#!/usr/bin/env python3
"""
rebuild_from_screenshots.py — Re-read saved screenshots with the current code
(T-23, v3.8.7).

With screenshot mode on, the daemon saves the exact frame of every capture
(…_auto_h2_s1_accepted.png) and of every screen auto-capture skipped
(…_settle_skipped.png). This tool replays those frames, in time order, through
the CURRENT pre-check, reader and engine — the very code the live daemon runs
(ocr_reader.read_frame, ocr_reader.precheck_says_capture, scoring) — and writes
a report, and optionally a database and dashboard, like rebuild_from_log.py.
A sitting recorded badly by an older version comes out as the current version
would have recorded it.

    python rebuild_from_screenshots.py screenshots\\2026-10-05 -o recovered.ods

To bring a recovered sitting into your whole history, give your log as well:
its captures outside the screenshots' time are kept, those inside are replaced
by the re-read frames.

    python rebuild_from_screenshots.py screenshots\\2026-10-05 --log ocr_daemon.log ^
        --with-archives -o ocr_output_new.ods --db-out gspro_stats_new.db ^
        --log-out ocr_daemon_corrected.log

Left out, as in the live run: frames auto-capture skipped for its cooldown
(…_settle_cooldown) and Ctrl+Shift+F12 screenshots (…_manual), which were not
captures. Nothing in your folders is changed; outputs go only where you name
them. Keep --log-out: a later rebuild from that log reproduces this result.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pytesseract                       # noqa: E402
from PIL import Image                    # noqa: E402

import digit_reader                      # noqa: E402
import field_cache                       # noqa: E402
import logrotate                         # noqa: E402
import ocr_reader                        # noqa: E402
import rebuild_from_log as rb            # noqa: E402
import screen_capture                    # noqa: E402
import strokes_gained as sg_mod          # noqa: E402
from version import VERSION_STR          # noqa: E402

log = logging.getLogger("rebuild_from_screenshots")

NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{3})_(.+)\.(png|jpe?g)$", re.I)
SPAN_GAP = timedelta(minutes=30)        # frames further apart belong to separate sittings
SPAN_PAD = timedelta(seconds=60)        # log captures this close to a sitting are replaced too


# ── finding the frames ────────────────────────────────────────────────────────

@dataclass
class FrameCapture(rb.Capture):
    """A saved screenshot, read when the replay reaches it (see prepare)."""
    path: Path | None = None
    kind: str = "auto"                  # auto (pre-checked live), manual (hotkey capture), skipped
    reads: list = field(default_factory=list)

    def prepare(self, state: dict) -> bool:
        return READER.prepare(self, state)


def classify(label: str) -> tuple[str, str] | None:
    """(kind, what the live run decided) for a screenshot label, or None to leave it out."""
    if label == "settle_skipped":
        return "skipped", "skipped"
    m = re.match(r"^(auto|manual)_h[^_]+_s[^_]+_(.+)$", label)
    if m:
        return m.group(1), m.group(2)
    return None                          # settle_cooldown, a hotkey screenshot, anything else


def collect_frames(paths: list[Path]) -> tuple[list[FrameCapture], Counter]:
    """Every capture frame under the given folders/files, oldest first."""
    files: list[Path] = []
    for p in paths:
        files += sorted(p.rglob("*")) if p.is_dir() else [p]
    frames, counts = [], Counter()
    for f in files:
        m = NAME.match(f.name)
        if not m or not f.is_file():
            continue
        what = classify(m.group(6))
        if what is None:
            counts["cooldown" if m.group(6) == "settle_cooldown" else "not a capture"] += 1
            continue
        ts = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                               "%Y-%m-%d %H:%M:%S").replace(microsecond=int(m.group(5)) * 1000)
        frames.append(FrameCapture(ts=ts, path=f, kind=what[0], live=what[1]))
        counts[what[0]] += 1
    frames.sort(key=lambda c: c.ts)
    return frames, counts


def spans(frames: list[FrameCapture]) -> list[tuple[datetime, datetime]]:
    """The sittings the frames cover, padded slightly."""
    out: list[list[datetime]] = []
    for f in frames:
        if out and f.ts - out[-1][1] <= SPAN_GAP:
            out[-1][1] = f.ts
        else:
            out.append([f.ts, f.ts])
    return [(a - SPAN_PAD, b + SPAN_PAD) for a, b in out]


def merge(log_caps: list[rb.Capture], frames: list[FrameCapture]) -> tuple[list, int]:
    """Log captures outside the frames' sittings, plus the frames, in time order."""
    covered = spans(frames)
    kept = [c for c in log_caps if not any(a <= c.ts <= b for a, b in covered)]
    return sorted(kept + frames, key=lambda c: c.ts), len(log_caps) - len(kept)


# ── reading a frame exactly as the live daemon would ──────────────────────────

class FrameReader:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.lang = config.get("tesseract_lang", "eng")
        # A saved screenshot is GSPro's monitor; regions are in window-2
        # coordinates, so this is the same arithmetic as the live crop.
        self.left, self.top = int(config.get("calibrated_monitor_left", 1920)), 0
        self.regions = {str(r.get("cell", "")).upper(): r for r in config.get("regions", [])}
        self.cache = (field_cache.FieldCache(config.get("ocr_cache_cells", ["A1", "B1", "C1", "D1"]))
                      if config.get("ocr_cache", True) else None)

    def _crop(self, frame, cell: str):
        return screen_capture.crop_region(frame, self.left, self.top, self.regions[cell])

    def prepare(self, cap: FrameCapture, state: dict) -> bool:
        frame = Image.open(cap.path).convert("RGB")
        par = state["round"].get("hole_par", 5)
        if cap.kind in ("auto", "skipped"):              # live: the auto-capture pre-check
            hole = 0
            if "B1" in self.regions:
                txt, _ = ocr_reader.ocr_image(self.regions["B1"], self._crop(frame, "B1"), self.lang)
                hole = sg_mod.parse_int(txt, 0)

            def ticker():
                if "V1" not in self.regions or not ocr_reader._ARROW_OK:
                    return None
                return ocr_reader._arrow.detect_stroke_ticker(self._crop(frame, "V1"), hole_par=par)

            if not ocr_reader.precheck_says_capture(state.get("last_capture"), hole, ticker):
                return False
        reads, ends, _, _, _ = ocr_reader.read_frame(self.config, frame, self.left, self.top,
                                                     par, self.lang, cache=self.cache)
        cap.reads = reads + ends
        cap.by_cell = {cell: text for _, cell, text, _ in cap.reads}
        cap.by_name = {}
        for name, cell, text, _ in reads:
            cap.by_name.setdefault(name.lower(), text)
        return True


READER: FrameReader | None = None


def setup_reader(config: dict, learned: Path | None, workdir: Path) -> FrameReader:
    """The live daemon's reading set-up, without touching any of your files."""
    global READER
    cmd = config.get("tesseract_cmd")
    if cmd and Path(cmd).exists():
        pytesseract.pytesseract.tesseract_cmd = cmd
    ocr_reader.suppress_subprocess_windows()
    ocr_reader.OCR_OVERRIDES = config.get("ocr_profiles", {}) or {}
    screen_capture.window_x_offset = 0
    if config.get("digit_templates", True):
        copy = workdir / "digit_templates.json"            # learn into a copy, never your file
        if learned and learned.exists():
            shutil.copy(learned, copy)
        ocr_reader.DIGITS = digit_reader.TemplateStore(HERE / "digit_templates.seed.json", copy)
    else:
        ocr_reader.DIGITS = None
    if ocr_reader._ARROW_OK:
        ocr_reader._arrow.DIGIT_READER = ocr_reader.DIGITS
        ocr_reader._arrow.WIND_ARROW_POINTS = config.get("wind_arrow_points", "to")
    READER = FrameReader(config)
    return READER


# ── a corrected log ───────────────────────────────────────────────────────────

def write_log(path: Path, caps: list, config: dict) -> int:
    """The replayed captures in the daemon's log format (rebuild_from_log reads it)."""
    names = {str(r.get("cell", "")).upper(): r.get("name", "") for r in config.get("regions", [])}
    lines, n = [], 0
    for c in caps:
        if not c.by_cell:
            continue                                          # skipped by the pre-check
        stamp = c.ts.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{stamp} INFO      ━━━  OCR pipeline  ({c.ts:%H:%M:%S})  ━━━")
        reads = getattr(c, "reads", None) or [(names.get(k, k.lower()), k, v, "") for k, v in c.by_cell.items()]
        for name, cell, text, note in reads:
            lines.append(f"{stamp} INFO        [{name:<22} {cell}]  {text!r}{'  ' + note if note else ''}")
        n += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


# ── command line ──────────────────────────────────────────────────────────────

class _RoundLines(logging.Filter):
    def filter(self, rec: logging.LogRecord) -> bool:
        msg = rec.getMessage()
        return "━━━  Round" in msg or "not recorded:" in msg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", type=Path, nargs="+",
                    help="screenshot folder(s) or files, searched recursively")
    ap.add_argument("--log", type=Path, nargs="*", default=[],
                    help="your daemon log(s): captures outside the screenshots' time are kept")
    ap.add_argument("--with-archives", action="store_true",
                    help="also use the archived logs in logs/ beside the first --log")
    ap.add_argument("-o", "--output", type=Path, default=Path("rebuilt_from_screenshots.ods"))
    ap.add_argument("--config", type=Path, default=None,
                    help="config.json (default: the one beside this script)")
    ap.add_argument("--learned", type=Path, default=HERE / "digit_templates.json",
                    help="your learned characters (read only; default beside this script)")
    ap.add_argument("--courses", type=Path, default=None,
                    help="seed course snapping from a known_courses.json (read-only)")
    ap.add_argument("--db-out", type=Path, default=None, help="also write a database")
    ap.add_argument("--dashboard", type=Path, default=None, metavar="HTML")
    ap.add_argument("--history-out", type=Path, default=None)
    ap.add_argument("--log-out", type=Path, default=None,
                    help="also write the replayed captures as a daemon log, for future rebuilds")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the engine's log")
    a = ap.parse_args()

    rounds = logging.StreamHandler(sys.stdout)
    rounds.addFilter(_RoundLines())
    rounds.setFormatter(logging.Formatter("%(message)s"))
    rest = logging.StreamHandler()
    rest.setLevel(logging.INFO if a.verbose else logging.ERROR)   # round checks say the rest
    rest.addFilter(lambda rec: not _RoundLines().filter(rec))
    rest.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[rounds, rest], force=True)

    config = rb._load_config(a.config)
    frames, counts = collect_frames(a.folders)
    if not frames:
        sys.exit("No capture screenshots found (…_auto_…, …_manual_h…, …_settle_skipped).")
    caps: list = frames
    replaced = 0
    if a.log:
        paths = list(a.log)
        if a.with_archives:
            paths = logrotate.archived_logs(paths[0]) + paths
        log_caps = [c for p in paths for c in rb.parse_log(p)]
        caps, replaced = merge(log_caps, frames)
    with tempfile.TemporaryDirectory() as td:
        setup_reader(config, a.learned, Path(td))
        print(f"Re-reading {len(frames)} screenshot(s) with the current code ({VERSION_STR})…")
        sheets, history, st = rb.replay(caps, config, a.courses)
    first, last = frames[0].ts, frames[-1].ts
    print(f"\nRebuilt from screenshots ({VERSION_STR})")
    print(f"  Screenshots              : {len(frames)}  ({first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M})")
    print(f"    auto / manual / skipped: {counts['auto']} / {counts['manual']} / {counts['skipped']}"
          f"   (left out: {counts['cooldown']} cooldown, {counts['not a capture']} not captures)")
    print(f"    skipped by the pre-check now: {st['prechecked']}")
    if a.log:
        print(f"  Log captures kept        : {sum(1 for c in caps if not isinstance(c, FrameCapture))}"
              f"  (replaced by screenshots: {replaced})")
    print(f"  Captures replayed        : {st['captures'] - st['prechecked']}")
    print(f"    accepted               : {st['accepted']}")
    print(f"    invalid (blank/menu)   : {st['invalid']}")
    print(f"    no progression (replay): {st['no_progression']}")
    print(f"    end-of-round screens   : {st['end_of_round']}")
    print(f"  Recovered vs live run    : {st['recovered']}  (rejected or skipped then, accepted now)")
    print(f"  Shots scored             : {st['shots']}")
    print(f"  Holes completed          : {st['holes']}")
    print(f"  Sessions                 : {st['sessions']}")
    if a.dry_run:
        print("\n--dry-run: nothing written")
        return
    rb.write_outputs(a, sheets, history, st, f"rebuild_from_screenshots {VERSION_STR}")
    if a.log_out:
        n = write_log(a.log_out, caps, config)
        print(f"Log: {a.log_out}  ({n} captures)")


if __name__ == "__main__":
    main()
