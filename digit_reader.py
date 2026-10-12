#!/usr/bin/env python3
"""
digit_reader.py — Read GSPro's numeric HUD fields by matching character shapes.

GSPro draws its HUD in a few fixed fonts. Instead of running Tesseract (a new
process per field, ~90 ms), each character is cut out and compared with stored
examples of that font's characters (well under a millisecond per field).

How a field is read
-------------------
1. The white text is isolated by colour, so coloured shapes (the elevation
   triangle, the red score bar, sky) drop out.
2. It is split into characters (connected shapes, left to right).
3. Dots, quote marks and minus signs are recognised by SIZE AND POSITION: a
   tiny mark low on the line is ".", high on the line is "'", wide and centred
   is "-". Everything else is compared with the stored examples, scaled to a
   common height, by overlap; the relative height and vertical position must
   also agree, so "o" is not taken for "0".
4. If any character is not recognised confidently, the field is NOT guessed:
   read() returns None and the caller uses Tesseract, as before. The result
   must also pass the field's format check.

Learning
--------
The shipped starter set (digit_templates.seed.json) comes from real frames,
but no handful of frames contains every digit in every font. When Tesseract
reads a field and the text passes the format check, learn() stores the shapes
of characters the set is short of (digit_templates.json, beside the daemon). A
learned character is used only once two examples agree, so a single misread
cannot teach it something wrong.

Cross-check
-----------
Every AUDIT_EVERY-th matched read of a font should also be read by Tesseract
(the caller does this, see needs_audit / record_audit). If the two disagree
too often, matching is switched off for that font for the rest of the session.
A single disagreement keeps the matched read: every one checked against the
screen was Tesseract misreading (27.4 → 21.4, 7.7 → 71, Hole9 → Hole).
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV_OK = True
except ImportError:                       # pragma: no cover
    _CV_OK = False

log = logging.getLogger(__name__)

# Which font each region is drawn in.
FONT_OF_CELL: dict[str, str] = {
    **{c: "panel" for c in ("L1", "M1", "N1", "O1", "P1", "Q1", "R1", "S1")},
    **{c: "hud" for c in ("B1", "C1", "D1", "K1", "F1")},
    "U1": "score",
}

# What a correct read of each field looks like.
_PANEL = r"^(-?\d+(\.\d+)?|\d+' ?\d{1,2}\"?|-{2,})$"
PATTERNS: dict[str, str] = {
    **{c: _PANEL for c in ("L1", "M1", "N1", "O1", "P1", "Q1", "R1", "S1")},
    "K1": r"^\d+(\.\d+)? ?yd$|^\d+ ?ft ?\d{1,2}$",
    "D1": r"^\d+ ?yds?$",
    "B1": r"^Hole ?\d{1,2}$",
    "C1": r"^Par ?\d$",
    "F1": r"^\d* ?ft ?\d{1,2}$",
    "U1": r"^(E|[+-]\d{1,2})$",
}

# How GSPro actually displays these fields — stricter than PATTERNS, which
# also decide what may be learned. The cross-check uses these to tell whether
# Tesseract's read is a possible value at all: GSPro shows VLA, HLA, ball speed
# and spin axis with exactly one decimal, so "48" for 4.8 or "71" for 7.7 is
# Tesseract failing, not a disagreement (v3.8.7; such reads switched the panel
# font — and with it the round score — off while re-reading 4 Oct).
_ONE_DP = r"^(-?\d+\.\d|-{2,})$"
AUDIT_PATTERNS: dict[str, str] = {
    **{c: _ONE_DP for c in ("L1", "M1", "Q1", "R1")},
    **{c: r"^(-?\d+\.\d|\d+' ?\d{1,2}\"?|-{2,})$" for c in ("N1", "O1", "P1")},
    "S1": r"^(\d+|-{2,})$",
    "F1": r"^\d+ ?ft ?\d{1,2}$",
}

SIZE = 32                # characters are compared at 32×32
SCALE = 3                # crops are enlarged before thresholding
MATCH_MIN = 0.70         # minimum score to accept a character
MATCH_MARGIN = 0.05      # it must beat the best OTHER character by this much
MAX_SAMPLES = 6          # examples kept per character
LEARNED_MIN = 2          # learned examples needed before a character is used
AUDIT_EVERY = 25         # matched reads per font between Tesseract cross-checks
_PUNCT = {".", "'", "-"}

# A font's digits are matched only once ALL TEN are on file. With a digit
# missing, a character never seen is matched to the nearest one that is — in
# testing, with "9" held out, "398' 4\"" read as "308' 4\"", a wrong value that
# still passes every format check. Letters need no such gate: a wrong letter
# fails the format check ("Ho1e").
DIGITS = set("0123456789")

# The round score is drawn in the launch panel's typeface: the panel's digits
# read every round-score frame tested (92 of 92, v3.8.1), while the score's own
# set lacked 0, 1, 6 and 8 for weeks of play — so its "-6" was left to Tesseract,
# which read it as "6". A font listed here also uses the other font's digits.
FONT_BORROW = {"score": "panel"}

# Letter pairs that may touch and come out as ONE shape (e.g. "ft" in the
# elevation text). Learning tries these merges when a read has fewer shapes
# than characters.
LIGATURES = ("ft", "yd", "ds")


# ── image → characters ────────────────────────────────────────────────────────

def white_mask(img) -> np.ndarray:
    """White text only: high brightness, low saturation, enlarged SCALE×."""
    rgb = np.asarray(img.convert("RGB"))
    big = cv2.resize(rgb, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(big, cv2.COLOR_RGB2HSV)
    return cv2.inRange(hsv, np.array([0, 0, 130]), np.array([180, 70, 255]))


def _normalise(crop: np.ndarray) -> np.ndarray:
    """Scale a character to SIZE high, keep its proportions, centre it."""
    h, w = crop.shape
    nw = max(1, min(SIZE, round(SIZE * w / h)))
    r = cv2.resize(crop.astype(np.uint8) * 255, (nw, SIZE), interpolation=cv2.INTER_AREA) > 100
    out = np.zeros((SIZE, SIZE), bool)
    x0 = (SIZE - nw) // 2
    out[:, x0:x0 + nw] = r
    return out


def segment(mask: np.ndarray) -> list[dict]:
    """
    Split a white-text mask into characters, left to right. Each entry has its
    box, normalised bitmap, relative height/top (vs the line), and a "punct"
    guess for marks too small to compare by shape.
    """
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    parts = [(int(st[i][0]), int(st[i][1]), int(st[i][2]), int(st[i][3]), i)
             for i in range(1, n) if st[i][4] >= 6 * SCALE]
    parts.sort()
    # Pieces whose columns overlap belong to one character.
    groups: list[list] = []
    for p in parts:
        if groups:
            gx0 = min(q[0] for q in groups[-1]); gx1 = max(q[0] + q[2] for q in groups[-1])
            overlap = min(gx1, p[0] + p[2]) - max(gx0, p[0])
            if overlap > 0.5 * min(p[2], gx1 - gx0):
                groups[-1].append(p)
                continue
        groups.append([p])
    if not groups:
        return []
    boxes = []
    for g in groups:
        x0 = min(q[0] for q in g); y0 = min(q[1] for q in g)
        x1 = max(q[0] + q[2] for q in g); y1 = max(q[1] + q[3] for q in g)
        crop = np.isin(lab[y0:y1, x0:x1], [q[4] for q in g])
        boxes.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "crop": crop})
    maxh = max(b["h"] for b in boxes)
    tall = [b for b in boxes if b["h"] >= 0.6 * maxh]
    ref_top = min(b["y"] for b in tall)
    ref_h = float(np.median([b["h"] for b in tall]))
    for b in boxes:
        b["rh"] = b["h"] / ref_h
        b["rt"] = (b["y"] - ref_top) / ref_h
        b["bits"] = _normalise(b["crop"])
        b["punct"] = None
        if b["h"] < 0.45 * ref_h:
            # Width decides first: the launch panel's minus sign sits low on the
            # line, so position alone read "-4.5" as ".4.5" (v3.6.1).
            cy = (b["y"] + b["h"] / 2 - ref_top) / ref_h
            if b["w"] >= 1.5 * b["h"]:
                b["punct"] = "-"
            elif cy > 0.55:
                b["punct"] = "."
            elif cy < 0.45:
                b["punct"] = "'"
    med_w = float(np.median([b["w"] for b in tall]))
    for prev, cur in zip(boxes, boxes[1:]):
        cur["space_before"] = (cur["x"] - (prev["x"] + prev["w"])) > 0.5 * med_w
    boxes[0]["space_before"] = False
    return boxes


def _tokens(text: str, n_shapes: int) -> list[str] | None:
    """
    The characters of *text* lined up with *n_shapes* shapes, or None. A '"'
    is two marks. If there are fewer shapes than characters, touching letter
    pairs from LIGATURES are merged until the counts agree.
    """
    chars = list(text.replace(" ", "").replace('"', "''"))
    if len(chars) == n_shapes:
        return chars
    tokens = chars[:]
    for pair in LIGATURES:
        i = 0
        while len(tokens) > n_shapes and i < len(tokens) - 1:
            if tokens[i] + tokens[i + 1] == pair:
                tokens[i:i + 2] = [pair]
            i += 1
    return tokens if len(tokens) == n_shapes else None


def _placeholder(boxes: list[dict]) -> bool:
    """GSPro's '---' (no data): only wide, short marks."""
    return len(boxes) >= 2 and all(b["w"] >= 1.5 * b["h"] for b in boxes)


# ── stored examples ───────────────────────────────────────────────────────────

def _pack(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8).ravel()).tobytes().hex()


def _unpack(hexstr: str) -> np.ndarray:
    return np.unpackbits(np.frombuffer(bytes.fromhex(hexstr), np.uint8))[:SIZE * SIZE] \
             .reshape(SIZE, SIZE).astype(bool)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    u = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / u if u else 0.0


class TemplateStore:
    """
    Character examples per font: the shipped starter set (read-only) plus
    what has been learned on this PC (saved separately, so an upgrade never
    overwrites it).
    """

    def __init__(self, seed_path: Path | None = None, user_path: Path | None = None,
                 require_all_digits: bool = True) -> None:
        self.seed_path, self.user_path = seed_path, user_path
        self.require_all_digits = require_all_digits
        self._lock = threading.RLock()
        self.samples: dict[str, dict[str, list[dict]]] = {}
        self.stats = {"read": 0, "matched": 0, "fallback": 0, "learned": 0, "waiting": 0}
        self._audits: dict[str, list[int]] = {}       # font → [checked, disagreed]
        self._disabled: set[str] = set()
        self._since_audit: dict[str, int] = {}
        for path, source in ((seed_path, "seed"), (user_path, "learned")):
            self._load(path, source)

    # persistence
    def _load(self, path: Path | None, source: str) -> None:
        if not path or not Path(path).exists():
            return
        try:
            import storage
            data = storage.load_json(Path(path), expect=dict, backups=2,
                                     what="Character templates") or {}
        except Exception as exc:
            log.warning("Character templates %s unreadable (%s) — ignored", path, exc)
            return
        for font, labels in data.items():
            for label, items in labels.items():
                for it in items:
                    self.samples.setdefault(font, {}).setdefault(label, []).append(
                        {"bits": _unpack(it["bits"]), "rh": it["rh"], "rt": it["rt"],
                         "source": source, "seen": int(it.get("seen", 1))})

    def save(self) -> None:
        """Write the learned examples (never the starter set)."""
        if not self.user_path:
            return
        with self._lock:
            out = {font: {lab: [{"bits": _pack(s["bits"]), "rh": round(s["rh"], 3),
                                 "rt": round(s["rt"], 3), "seen": s.get("seen", 1)}
                                for s in items if s["source"] == "learned"]
                          for lab, items in labels.items()}
                   for font, labels in self.samples.items()}
            out = {f: {lab: v for lab, v in labs.items() if v} for f, labs in out.items()}
            out = {f: labs for f, labs in out.items() if labs}
        try:
            import storage
            storage.save_json(Path(self.user_path), out, backups=2)
        except Exception as exc:
            log.warning("Character templates not saved: %s", exc)

    def to_dict(self, source: str | None = None) -> dict:
        """Serialisable form (used to build the starter set)."""
        with self._lock:
            return {font: {lab: [{"bits": _pack(s["bits"]), "rh": round(s["rh"], 3),
                                  "rt": round(s["rt"], 3)}
                                 for s in items if source is None or s["source"] == source]
                           for lab, items in labels.items()}
                    for font, labels in self.samples.items()}

    def _candidates(self, font: str) -> dict[str, list[dict]]:
        """The font's own examples, plus the digits it borrows (FONT_BORROW)."""
        out = {lab: list(items) for lab, items in self.samples.get(font, {}).items()}
        src = FONT_BORROW.get(font)
        if src:
            for lab, items in self.samples.get(src, {}).items():
                if lab.isdigit():
                    out.setdefault(lab, []).extend(items)
        return out

    def missing_digits(self, font: str) -> str:
        """Digits this font still needs before its fields are matched."""
        with self._lock:
            have = {lab for lab, items in self._candidates(font).items()
                    if self._usable(items)}
        return "".join(sorted(DIGITS - have))

    def _usable(self, items: list[dict]) -> list[dict]:
        """Starter-set characters are usable at once; learned ones once seen
        LEARNED_MIN times in agreement (identical sightings count — GSPro
        draws a digit pixel-identically, so a second sighting is often exact)."""
        if any(s["source"] == "seed" for s in items):
            return items
        return items if sum(s.get("seen", 1) for s in items) >= LEARNED_MIN else []

    # matching
    def classify(self, font: str, glyph: dict) -> tuple[str | None, float]:
        best: dict[str, float] = {}
        with self._lock:
            for label, items in self._candidates(font).items():
                for s in self._usable(items):
                    score = (_iou(glyph["bits"], s["bits"])
                             - 0.5 * abs(glyph["rh"] - s["rh"])
                             - 0.5 * abs(glyph["rt"] - s["rt"]))
                    if score > best.get(label, -1.0):
                        best[label] = score
        if not best:
            return None, 0.0
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        label, score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else -1.0
        if score >= MATCH_MIN and score - runner >= MATCH_MARGIN:
            return label, score
        return None, score

    def read(self, cell: str, img) -> str | None:
        """The field's text, or None if any character is uncertain."""
        font = FONT_OF_CELL.get(cell)
        if not _CV_OK or font is None or font in self._disabled:
            return None
        with self._lock:
            self.stats["read"] += 1
        if self.require_all_digits and self.missing_digits(font):
            with self._lock:
                self.stats["waiting"] += 1
            return None                              # still learning this font
        boxes = segment(white_mask(img))
        if not boxes:
            return None
        if _placeholder(boxes):
            text = "-" * len(boxes)
        else:
            out = []
            for b in boxes:
                ch = b["punct"]
                if ch is None:
                    ch, _ = self.classify(font, b)
                    if ch is None:
                        with self._lock:
                            self.stats["fallback"] += 1
                        return None
                out.append((" " if b.get("space_before") else "") + ch)
            text = "".join(out).replace("''", '"')
        if not re.fullmatch(PATTERNS[cell], text):
            # The panel font spaces its decimal point widely, which reads as a
            # gap; no numeric field needs a space, so try without them.
            text = text.replace(" ", "")
        if not re.fullmatch(PATTERNS[cell], text):
            with self._lock:
                self.stats["fallback"] += 1
            return None
        with self._lock:
            self.stats["matched"] += 1
            self._since_audit[font] = self._since_audit.get(font, 0) + 1
        return text

    # learning
    def learn(self, cell: str, img, text: str) -> int:
        """
        Store character shapes from a read that passed the format check.
        Returns how many examples were added.
        """
        font = FONT_OF_CELL.get(cell)
        text = (text or "").strip()
        if not _CV_OK or font is None or not text or set(text) <= {"-", " "}:
            return 0
        if not re.fullmatch(PATTERNS[cell], text):
            return 0
        boxes = segment(white_mask(img))
        expected = _tokens(text, len(boxes))
        if expected is None:
            return 0                                 # cannot line characters up
        added = 0
        with self._lock:
            for b, ch in zip(boxes, expected):
                if ch in _PUNCT:
                    continue
                items = self.samples.setdefault(font, {}).setdefault(ch, [])
                ious = [_iou(b["bits"], s["bits"]) for s in items]
                if ious and max(ious) >= 0.97:
                    twin = items[ious.index(max(ious))]
                    if twin["source"] == "learned" and twin.get("seen", 1) < LEARNED_MIN:
                        twin["seen"] = twin.get("seen", 1) + 1   # a confirmation
                        added += 1
                    continue
                if ious and max(ious) < 0.55:
                    continue                         # disagrees with what we know
                if len(items) >= MAX_SAMPLES:
                    continue
                items.append({"bits": b["bits"], "rh": b["rh"], "rt": b["rt"],
                              "source": "learned", "seen": 1})
                added += 1
            self.stats["learned"] += added
        if added:
            self.save()
        return added

    def learn_seed(self, cell: str, img, text: str) -> int:
        """Like learn(), but marks the examples as verified starter-set ones."""
        n = self.learn(cell, img, text)
        if n:
            with self._lock:
                for labels in self.samples.values():
                    for items in labels.values():
                        for s in items:
                            s["source"] = "seed"
        return n

    # cross-check
    def needs_audit(self, cell: str) -> bool:
        font = FONT_OF_CELL.get(cell)
        with self._lock:
            if font and self._since_audit.get(font, 0) >= AUDIT_EVERY:
                self._since_audit[font] = 0
                return True
        return False

    def record_audit(self, cell: str, matched: str, tesseract: str) -> bool:
        """Compare a matched read with Tesseract. Returns True if they agree."""
        font = FONT_OF_CELL.get(cell, "?")
        norm = lambda s: re.sub(r"\s+", "", s or "")
        pat = AUDIT_PATTERNS.get(cell) or PATTERNS.get(cell, ".*")
        if not re.fullmatch(pat, (tesseract or "").strip()) \
                and not re.fullmatch(pat, norm(tesseract)):
            # Tesseract's read is not even a valid value for the field ("Hole"
            # with no number, seen in play): it failed, not the matcher.
            log.debug("Cross-check on %s: Tesseract read %r is not a valid value — "
                      "kept %r", cell, tesseract, matched)
            return True
        agree = norm(matched) == norm(tesseract)
        with self._lock:
            a = self._audits.setdefault(font, [0, 0])
            a[0] += 1
            if not agree:
                a[1] += 1
                log.warning("Character matching and Tesseract disagree on %s: %r vs %r",
                            cell, matched, tesseract)
                if a[1] >= 3 and a[1] / a[0] > 0.10:
                    self._disabled.add(font)
                    log.error("Character matching switched OFF for the %s font this "
                              "session (%d of %d cross-checks disagreed) — using "
                              "Tesseract", font, a[1], a[0])
        return agree

    def summary(self) -> str:
        with self._lock:
            s = dict(self.stats)
            audits = {f: f"{a[0] - a[1]}/{a[0]}" for f, a in self._audits.items()}
        return (f"{s['matched']} matched, {s['fallback']} fell back to Tesseract, "
                f"{s['waiting']} waited for a font's digits to be learned, "
                f"{s['learned']} character example(s) learned"
                + (f", cross-checks agreed {audits}" if audits else ""))
