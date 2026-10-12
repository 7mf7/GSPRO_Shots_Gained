#!/usr/bin/env python3
"""
ocr_reader.py — Reading GSPro's HUD regions. Split out of ocr_daemon.py (T-22, v3.8.5).

  * ocr_image: one region's image → text — the arrow, ticker and elevation
    detectors, character matching for GSPro's fixed fonts, Tesseract otherwise
    (tesseract_read, with per-field profiles), and fixes for known misreads
  * capture_and_ocr: grab one region and read it (the auto-capture pre-check)
  * read_end_screen: GSPro's end-of-round total (T-28)
  * suppress_subprocess_windows: no console window flashing per Tesseract call

State owned here: OCR_OVERRIDES (per-field profiles from config.json) and DIGITS
(the character-matching store), both set by the daemon at start-up, and
DEBUG_DIR for the --test debug images.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytesseract
from PIL import Image

import digit_reader
import screen_capture

try:
    import arrow_detector as _arrow
    _ARROW_OK = True
except ImportError:
    _ARROW_OK = False

try:
    import ocr_vocab as vocab
    _VOCAB_OK = True
except ImportError:
    _VOCAB_OK = False

log = logging.getLogger(__name__)
DEBUG_DIR = Path(__file__).resolve().parent

# Per-cell OCR profile overrides from config.json, set up in main(). The learned
# course list lives in parsing.COURSE_BOOK so the rebuild tool shares it.
OCR_OVERRIDES: dict = {}
# Character matching for the numeric HUD fields (digit_reader.py); set in main().
DIGITS: "digit_reader.TemplateStore | None" = None
# ── Known Tesseract misreads for GSPro's display font ─────────────────────────
# Add new entries here if further substitutions are discovered.
# These characters should never legitimately appear in GSPro game data.
_OCR_CHAR_FIXES: dict[str, str] = {
    "&": "8",   # curves of 8 misread as ampersand
    "$": "9",   # confirmed by user observation
}
def fix_ocr_chars(text: str) -> str:
    """Replace known Tesseract misreads with the correct characters."""
    for wrong, right in _OCR_CHAR_FIXES.items():
        text = text.replace(wrong, right)
    return text
def preprocess_image(img: Image.Image, mode: str | None) -> Image.Image:
    if not mode:
        return img
    img = img.convert("L")
    if mode in ("scale", "auto"):
        w, h = img.size
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
    if mode in ("threshold", "auto"):
        img = img.point(lambda p: 255 if p > 140 else 0)
    return img
def suppress_subprocess_windows() -> None:
    """
    Stop child processes from flashing a console window on Windows.

    pytesseract shells out to tesseract.exe for every region.  If that process
    is created without CREATE_NO_WINDOW, Windows briefly shows a console — and
    a new foreground window STEALS FOCUS.  An exclusive-fullscreen Direct3D
    application that loses focus minimises or drops to windowed mode, which is
    exactly the failure being fixed here: it fires the moment the first capture
    runs, and repeats on every capture afterwards.

    Whether pytesseract sets the flag itself varies by version, so this patches
    subprocess.Popen for this process to always add it.  The flag only
    suppresses the console window; stdout/stderr piping is unaffected, so
    pytesseract still reads results normally.  No-op off Windows.
    """
    if sys.platform != "win32":
        return
    import subprocess

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    original_popen   = subprocess.Popen

    if getattr(original_popen, "_ocr_daemon_patched", False):
        return

    class _NoWindowPopen(original_popen):          # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | create_no_window
            super().__init__(*args, **kwargs)

    _NoWindowPopen._ocr_daemon_patched = True      # type: ignore[attr-defined]
    subprocess.Popen = _NoWindowPopen              # type: ignore[assignment]
    log.debug("Subprocess console windows suppressed (CREATE_NO_WINDOW)")
def read_end_screen(config: dict, frame, left: int, top: int) -> list[tuple[str, str, str]]:
    """
    GSPro's end-of-round screen (T-28): [(cell, name, text)] for the total and
    its label when the screen is showing, else []. A cheap pixel check comes
    first — the screen's white header band above its dark score box — so the
    two OCR reads run only on that screen (tested on 150 screenshots: it fired
    on the 5 end screens and nothing else).
    """
    es = config.get("end_screen") or {}
    if not es or frame is None:
        return []
    import numpy as np
    from PIL import ImageOps

    def crop(key: str) -> Image.Image:
        x, y, w, h = es[key]
        return screen_capture.crop_region(frame, left, top, {"x": x, "y": y, "width": w, "height": h})
    try:
        band = np.asarray(crop("band").convert("RGB"))
        edge = np.asarray(crop("box_top").convert("RGB"))
        if band.size == 0 or (band.min(axis=2) > 230).mean() < 0.80 or edge.mean() > 45:
            return []
        out = []
        for key, cell, name, chars in (("score", "W1", "end-of-round score", "0123456789"),
                                       ("label", "W2", "end-of-round label",
                                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ")):
            c = crop(key)
            g = ImageOps.invert(c.convert("L")).resize((c.width * 2, c.height * 2))
            text = pytesseract.image_to_string(
                g, config=f"--psm 7 -c tessedit_char_whitelist={chars}").strip()
            out.append((cell, name, text))
        return out
    except Exception as exc:
        log.debug("End-of-round screen check failed: %s", exc)
        return []
def capture_and_ocr(region: dict, lang: str, save_debug: bool = False) -> tuple[str, Image.Image]:
    """
    Grab one region on its own and OCR it.

    Kept for callers that need a single field outside a full capture — the
    auto-capture pre-check reads only the hole number. A full capture uses
    _grab_frame() + ocr_image() instead, so every field comes from one instant.
    """
    return ocr_image(region, screen_capture.grab_region(region), lang, save_debug)
def on_navy_panel(img: Image.Image, min_fraction: float = 0.5) -> bool:
    """
    Is this crop on one of GSPro's navy HUD panels? Used for the LAST SHOT
    panel, which only appears after a shot from a penalising lie: its crops are
    ~80% navy when it shows and ~0% otherwise (T-08).
    """
    import numpy as np
    a = np.asarray(img.convert("RGB")).astype(int)
    navy = ((abs(a[..., 0] - 34) < 16) & (abs(a[..., 1] - 44) < 16) & (abs(a[..., 2] - 74) < 20))
    return float(navy.mean()) >= min_fraction


def last_shot_regions(config: dict) -> list[dict]:
    """The LAST SHOT panel's two percentages as regions X1 (speed) and Y1 (spin)."""
    panel = config.get("last_shot_panel") or {}
    out = []
    for cell, key, name in (("X1", "speed", "speed kept"), ("Y1", "spin", "spin kept")):
        box = panel.get(key)
        if box and len(box) == 4:
            x, y, w, h = (int(v) for v in box)
            out.append({"cell": cell, "name": name, "x": x, "y": y, "width": w, "height": h,
                        "preprocess": "auto", "detection_method": "panel_percent",
                        "tesseract_config": "--psm 7 -c tessedit_char_whitelist=0123456789%"})
    return out


def tess_config(region: dict) -> str:
    """Tesseract config for a region: per-field profile, else the region's own."""
    if _VOCAB_OK:
        return vocab.tesseract_config_for(region.get("cell", ""), region, OCR_OVERRIDES)
    return region.get("tesseract_config", "--psm 7")
def ocr_image(region: dict, img: Image.Image, lang: str,
              save_debug: bool = False, defer_tesseract: bool = False):
    """
    Extract text from an already-captured image of *region*.

    Dispatch is controlled by  region["detection_method"]:
      (absent / "tesseract")  → standard Tesseract OCR pipeline
      "arrow_direction"       → OpenCV pixel-mass arrow direction
                                returns compass label e.g. "N (Headwind)"
      "height_delta"          → OpenCV arrow direction + Tesseract digits
                                returns signed value e.g. "+15" or "-8"

    Falls back to Tesseract if arrow_detector is unavailable or the
    detection method string is unrecognised.
    """
    name   = region.get("name", "region")
    method = region.get("detection_method", "").lower().strip()

    debug_path = DEBUG_DIR / f"debug_{name}.png" if save_debug else None
    if save_debug:
        img.save(debug_path)
        log.info("  Debug image → %s", debug_path)

    # ── GSPro's LAST SHOT panel (T-08): read only while it is showing ──────────
    if method == "panel_percent":
        if not on_navy_panel(img):
            return "", img                  # not showing: nothing to read, no Tesseract
        method = ""                         # showing: an ordinary Tesseract field

    # ── OpenCV arrow-based methods ─────────────────────────────────────────────
    if method in ("arrow_direction", "height_delta"):
        if not _ARROW_OK:
            log.warning(
                "  [%s] arrow_detector not available — falling back to Tesseract", name
            )
        else:
            arrow_debug = (DEBUG_DIR / f"debug_arrow_{name}.png") if save_debug else None
            if method == "arrow_direction":
                text = _arrow.detect_wind_direction(img, debug_path=arrow_debug)
            else:  # height_delta
                text = _arrow.detect_height_delta(img, debug_path=arrow_debug)
            return text, img

    # ── Character matching (fixed HUD fonts) — ~1 ms instead of ~85 ms ────────
    cell = str(region.get("cell", "")).upper()
    reader = DIGITS if (DIGITS is not None and cell in digit_reader.FONT_OF_CELL) else None
    if reader is not None:
        matched = reader.read(cell, img)
        if matched is not None:
            if reader.needs_audit(cell):
                # A disagreement counts toward switching the font off (then
                # Tesseract reads it), but the matched read is kept: in every
                # disagreement checked against the screen Tesseract was the one
                # wrong — e.g. a carry of 27.4 read as 21.4 (v3.8.9).
                tess, _ = tesseract_read(region, img, lang, method)
                reader.record_audit(cell, matched, tess)
            return matched, img
    if defer_tesseract:
        # Read later in a shared Tesseract run (T-37); TessJob.finish does the rest.
        prepared, out_img = tesseract_prepare(region, img, method)
        return TessJob(region, img, prepared, out_img, lang, tess_config(region),
                       learn=reader is not None)
    text, out_img = tesseract_read(region, img, lang, method)
    if reader is not None and text:
        reader.learn(cell, img, text)        # fills in characters still missing
    return text, out_img
def tesseract_prepare(region: dict, img: Image.Image,
                      method: str) -> tuple[Image.Image, Image.Image]:
    """
    The image Tesseract will read for this field, and the image returned with
    the text — the first half of tesseract_read, split out so several fields
    can share one Tesseract run (T-37, v3.8.8).
    """
    name = region.get("name", "region")
    # ── Smart auto — handles both light-on-dark and dark-on-light text ─────────
    # Used for regions like the hole-score ticker (white text on black background).
    # Detects the background brightness and inverts if needed before Tesseract.
    if method == "auto":
        gray = img.convert("L")
        mean_brightness = sum(gray.getdata()) / (gray.width * gray.height)
        if mean_brightness < 100:                       # dark background → invert
            gray = gray.point(lambda p: 255 - p)
            log.debug("  [%s] auto: dark background detected — inverted", name)
        w, h = gray.size
        gray = gray.resize((w * 2, h * 2), Image.LANCZOS)
        gray = gray.point(lambda p: 255 if p > 140 else 0)
        return gray, img
    # ── Standard Tesseract path ────────────────────────────────────────────────
    prepared = preprocess_image(img, region.get("preprocess") or None)
    return prepared, prepared


def tesseract_text(raw: str) -> str:
    """Tesseract's raw output → the field's text."""
    return fix_ocr_chars(" ".join((raw or "").split()))


def tesseract_read(region: dict, img: Image.Image, lang: str,
                   method: str) -> tuple[str, Image.Image]:
    """The Tesseract paths of ocr_image (also used to cross-check matching)."""
    prepared, out_img = tesseract_prepare(region, img, method)
    raw = pytesseract.image_to_string(prepared, lang=lang, config=tess_config(region))
    return tesseract_text(raw), out_img


@dataclass
class TessJob:
    """A field waiting for Tesseract (T-37): read in a shared run, then finished."""
    region: dict
    img: Image.Image          # the crop as captured (for learning and the field cache)
    prepared: Image.Image     # what Tesseract reads
    out_img: Image.Image
    lang: str
    config: str
    learn: bool = False       # fill in missing characters from the result, as ocr_image does

    @property
    def batchable(self) -> bool:
        return self.prepared.mode in ("1", "L")

    def finish(self, raw: str) -> tuple[str, Image.Image]:
        text = tesseract_text(raw)
        if self.learn and text and DIGITS is not None:
            DIGITS.learn(str(self.region.get("cell", "")).upper(), self.img, text)
        return text, self.out_img


def _tesseract_pages(images: list, lang: str, config: str) -> list[str] | None:
    """
    One Tesseract run over several fields: each image is a page of one TIFF,
    read separately with the same settings, and the output is split on the page
    separator. None if the page count does not come back right.
    """
    with tempfile.TemporaryDirectory(prefix="gspro_ocr_") as td:
        path = Path(td) / "fields.tif"
        pages = [im.convert("L") for im in images]
        pages[0].save(path, save_all=True, append_images=pages[1:], compression="tiff_lzw")
        text = pytesseract.image_to_string(str(path), lang=lang, config=config)
    parts = text.split("\f")
    if len(parts) == len(images) + 1 and not parts[-1].strip():
        parts = parts[:-1]
    return parts if len(parts) == len(images) else None


def run_tesseract_jobs(jobs: list) -> list:
    """
    Read every waiting field with one Tesseract run per group of identical
    settings (T-37): Tesseract's start-up, ~75 ms, is most of a single read.
    Groups run in parallel. A group whose batch fails, or returns the wrong
    number of pages, is read one field at a time, so batching never costs a
    reading. Returns, per job, its text or the exception that stopped it.
    """
    groups: dict[tuple, list[int]] = defaultdict(list)
    for k, job in enumerate(jobs):
        groups[(job.lang, job.config, job.batchable)].append(k)
    out: list = [None] * len(jobs)

    def run_group(idxs: list[int]) -> None:
        group = [jobs[k] for k in idxs]
        raws = None
        if len(group) > 1 and group[0].batchable:
            try:
                raws = _tesseract_pages([j.prepared for j in group], group[0].lang, group[0].config)
                if raws is None:
                    log.warning("  Batched Tesseract run returned the wrong number of fields "
                                "— reading those %d one at a time", len(group))
            except Exception as exc:
                log.warning("  Batched Tesseract run failed (%s) — reading those %d fields "
                            "one at a time", exc, len(group))
        if raws is None:
            raws = []
            for j in group:
                try:
                    raws.append(pytesseract.image_to_string(j.prepared, lang=j.lang, config=j.config))
                except Exception as exc:
                    raws.append(exc)
        for k, raw in zip(idxs, raws):
            out[k] = raw if isinstance(raw, Exception) else jobs[k].finish(raw)[0]

    with ThreadPoolExecutor(max_workers=max(1, len(groups)), thread_name_prefix="tess") as pool:
        list(pool.map(run_group, groups.values()))
    return out


# ── one whole frame (the daemon's pipeline and rebuild_from_screenshots.py) ───

def read_frame(config: dict, frame, left: int, top: int, hole_par: int,
               lang: str = "eng", cache=None, debug: bool = False):
    """
    Read every region of one frame, in parallel — the daemon's capture
    pipeline and rebuild_from_screenshots.py both use this, so a replay of
    saved screenshots reads exactly as the live daemon does (v3.8.7).
    Returns (reads, end_reads, errors, seconds, workers); reads are
    (name, cell, text, note) in config order, end_reads those of GSPro's
    end-of-round screen when it is showing.
    """
    regions = config.get("regions", []) + last_shot_regions(config)
    unchanged: set[str] = set()

    def _read(region: dict) -> tuple[str, str, str]:
        name = region.get("name", "unnamed")
        cell = region.get("cell", "").upper()
        img = screen_capture.crop_region(frame, left, top, region)
        if cell == "V1" and _ARROW_OK:
            dbg = (DEBUG_DIR / f"debug_arrow_{name}.png") if debug else None
            stroke = _arrow.detect_stroke_ticker(img, hole_par=hole_par, debug_path=dbg)
            return name, cell, (str(stroke) if stroke is not None else "")
        if cache is not None:
            cached = cache.lookup(cell, img)
            if cached is not None:                 # same pixels as last time
                unchanged.add(cell)
                return name, cell, cached
        res = (ocr_image(region, img, lang, save_debug=debug, defer_tesseract=True) if batch
               else ocr_image(region, img, lang, save_debug=debug))
        if isinstance(res, TessJob):               # read in the shared run below
            return name, cell, res
        text, _ = res
        if cache is not None:
            cache.store(cell, img, text)
        return name, cell, text

    # Phase 1: everything that needs no Tesseract (detectors, character
    # matching, cache hits), collecting the fields that do. Phase 2: those, in
    # one Tesseract run per group of settings instead of one each (T-37).
    batch = bool(config.get("ocr_batch", True))
    workers = max(1, int(config.get("ocr_workers", 6)))
    errors, results = 0, []
    t1 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool:
        futures = [pool.submit(_read, rg) for rg in regions]
        for rg, fut in zip(regions, futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                log.error("  [%s %s]  FAILED: %s", rg.get("name", "?"), rg.get("cell", "?"), exc)
                errors += 1
    waiting = [k for k, (_, _, t) in enumerate(results) if isinstance(t, TessJob)]
    if waiting:
        texts = run_tesseract_jobs([results[k][2] for k in waiting])
        for k, text in zip(waiting, texts):
            name, cell, job = results[k]
            if isinstance(text, Exception):
                log.error("  [%s %s]  FAILED: %s", name, cell, text)
                errors += 1
                results[k] = None
                continue
            results[k] = (name, cell, text)
            if cache is not None:
                cache.store(cell, job.img, text)
        results = [r for r in results if r is not None]
    seconds = time.perf_counter() - t1
    reads = [(name, cell, text,
              f"(stroke ticker, par={hole_par})" if cell == "V1" and _ARROW_OK
              else "(unchanged)" if cell in unchanged else "")
             for name, cell, text in results]
    ends = [(name, cell, text, "") for cell, name, text in read_end_screen(config, frame, left, top)]
    return reads, ends, errors, seconds, workers


def log_reads(reads) -> None:
    """Log reads in the daemon's format — the one rebuild_from_log.py parses."""
    for name, cell, text, note in reads:
        log.info("  [%-22s %s]  %r%s", name, cell, text, f"  {note}" if note else "")


def precheck_says_capture(last: dict | None, hole: int,
                          read_ticker: Callable[[], "int | None"]) -> bool:
    """
    The auto-capture pre-check's decision (v3.8.7: shared with
    rebuild_from_screenshots.py). True = run the full capture: nothing to
    compare against, an unreadable or different hole, or a stroke counter that
    is unreadable or changed (a new hole resets it). The ticker is read only
    when the hole is unchanged.
    """
    if not last:
        return True
    if not hole or hole != last.get("hole", 0):
        return True
    ticker = read_ticker()
    if ticker is None:
        return True
    return ticker != (last.get("ticker", 0) or 0)

