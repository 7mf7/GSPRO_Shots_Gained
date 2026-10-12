#!/usr/bin/env python3
"""
ocr_vocab.py — Per-field OCR configuration and closed-vocabulary snapping.

Two accuracy improvements live here, kept out of ocr_daemon.py so they can be
tested in isolation.

1. FIELD PROFILES
   Every region used to be read with Tesseract's --psm 6 ("assume a block of
   text"), which is the wrong mode for a single number on one line, and with no
   character whitelist, so the panel border was read as ']' or ')' — e.g.
   '-2.]', '4]', '32)', and the round score '-1' as '-|' or '-]'. Each field now
   gets a single-line mode (--psm 7) and, where safe, a whitelist of the only
   characters that field can legitimately contain.

2. VOCABULARY SNAPPING
   Clubs and lies come from small fixed sets, and courses from a set that grows
   slowly. OCR damage like 'L Weda', 'S Wedg', '8 kon' is snapped to the nearest
   known value. Against the real log this raised exact club matches from 41% to
   99%. A snap only happens when the match is clear; an ambiguous read is left
   alone rather than forced to a plausible but possibly wrong value.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ── Field profiles ─────────────────────────────────────────────────────────────
#
# Whitelists deliberately contain ONLY letters, digits, '.', '-' and '+'.
# pytesseract splits the config string with shlex, and on Windows a quote or a
# space inside the whitelist breaks that split. This is why P1 (total length),
# the one field that shows feet-and-inches as  12' 4"  , gets single-line mode
# but no whitelist: it needs the quote characters, and they cannot be passed.
#
# Whitelists include the full digit set even where the valid range is narrow
# (par is 3-5). Restricting to 345 would force a smudged digit INTO the valid
# range — a wrong value that then passes validation. With all ten digits, a
# misread lands outside the range and validation rejects it instead.

_NUMERIC = "0123456789.-"

FIELD_PROFILES: dict[str, dict] = {
    "A1": {"psm": 7, "whitelist": None,              "kind": "course"},
    "B1": {"psm": 7, "whitelist": "Hole0123456789",  "kind": "hole"},
    "C1": {"psm": 7, "whitelist": "Par0123456789",   "kind": "par"},
    "D1": {"psm": 7, "whitelist": "0123456789.yds",  "kind": "distance"},
    "G1": {"psm": 7, "whitelist": None,              "kind": "lie"},
    "H1": {"psm": 7, "whitelist": None,              "kind": "text"},
    "I1": {"psm": 7, "whitelist": None,              "kind": "club"},
    "J1": {"psm": 7, "whitelist": None,              "kind": "lie"},
    "K1": {"psm": 7, "whitelist": "0123456789.ftyd", "kind": "distance"},
    "L1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "M1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "N1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "O1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "P1": {"psm": 7, "whitelist": None,              "kind": "distance"},
    "Q1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "R1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "S1": {"psm": 7, "whitelist": _NUMERIC,          "kind": "number"},
    "U1": {"psm": 7, "whitelist": "E+-0123456789",   "kind": "score"},
    # E1 sits over GSPro's wind SPEED text ("0 MPH"). It was configured as an
    # arrow detector and returned a meaningless direction for every frame.
    "E1": {"psm": 7, "whitelist": "0123456789MPHKmphk", "kind": "wind speed"},
}

_SAFE_WHITELIST = re.compile(r"^[A-Za-z0-9.+\-]+$")


def tesseract_config_for(cell: str, region: dict,
                         overrides: dict | None = None) -> str:
    """
    Build the Tesseract config string for one region.

    Precedence, highest first:
      region "use_profile": false  → the region's own tesseract_config, as before
      region "psm" / "whitelist"   → override individual profile fields
      config "ocr_profiles"[cell]  → override the built-in profile
      FIELD_PROFILES[cell]         → built-in default
      no profile for this cell     → the region's own tesseract_config
    """
    cell = (cell or "").upper()
    if region.get("use_profile") is False:
        return region.get("tesseract_config", "--psm 7")

    prof = dict(FIELD_PROFILES.get(cell, {}))
    if overrides and cell in overrides:
        prof.update(overrides[cell] or {})
    if "psm" in region:
        prof["psm"] = region["psm"]
    if "whitelist" in region:
        prof["whitelist"] = region["whitelist"]

    if not prof:
        return region.get("tesseract_config", "--psm 7")

    cfg = f"--psm {int(prof.get('psm', 7))}"
    wl = prof.get("whitelist")
    if wl:
        if _SAFE_WHITELIST.match(wl):
            cfg += f" -c tessedit_char_whitelist={wl}"
        else:
            log.warning("OCR profile %s: whitelist %r contains characters that "
                        "cannot be passed to Tesseract safely — ignored", cell, wl)
    return cfg


def field_kind(cell: str) -> str:
    return FIELD_PROFILES.get((cell or "").upper(), {}).get("kind", "text")


# ── Round score repair ─────────────────────────────────────────────────────────

_SCORE_REPAIR = str.maketrans({"|": "1", "]": "1", "[": "1", "l": "1",
                               "I": "1", "!": "1", "O": "0", "o": "0"})


def repair_score_text(text: str) -> str:
    """
    Repair the round-score field before parsing.

    The log shows '-1' read as '-|' and '-]' sixteen times. The whitelist should
    stop that at source on Tesseract 4.1+, but this is a second line of defence
    for older builds that ignore whitelists. 'E' (even par) is left untouched.
    """
    t = (text or "").strip()
    if t.upper() in ("E", "EVEN"):
        return t
    return t.translate(_SCORE_REPAIR)


# ── Generic snapping ───────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return " ".join(s.split())


def snap(raw: str, vocabulary: list[str], cutoff: float,
         margin: float = 0.05) -> tuple[str, bool]:
    """
    Snap *raw* to the closest vocabulary entry.

    Returns (value, snapped). A snap happens only when the best match clears
    *cutoff* AND beats the runner-up by *margin*. An ambiguous read — two
    candidates about equally close — is returned unchanged with snapped=False,
    because forcing it would produce a plausible-looking wrong value that no
    later check can catch.
    """
    key = _norm(raw)
    if not key:
        return "", False
    scored = sorted(((difflib.SequenceMatcher(None, key, _norm(v)).ratio(), v)
                     for v in vocabulary), reverse=True)
    if not scored:
        return (raw or "").strip(), False
    best_score, best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= cutoff and (best_score - runner) >= margin:
        return best, True
    return (raw or "").strip(), False


# ── Clubs ──────────────────────────────────────────────────────────────────────
# As GSPro's HUD displays them. Extend if your bag shows something else.

CLUBS: list[str] = [
    "Driver",
    "2 Wood", "3 Wood", "4 Wood", "5 Wood", "7 Wood",
    "2 Hybrid", "3 Hybrid", "4 Hybrid", "5 Hybrid", "6 Hybrid",
    "1 Iron", "2 Iron", "3 Iron", "4 Iron", "5 Iron",
    "6 Iron", "7 Iron", "8 Iron", "9 Iron",
    "P Wedge", "G Wedge", "S Wedge", "L Wedge",
    "Putter",
]


def snap_club(raw: str) -> tuple[str, bool]:
    return snap(raw, CLUBS, cutoff=0.5)


# ── Lies ───────────────────────────────────────────────────────────────────────
# GSPro's lie labels. The snapped label still goes through
# strokes_gained.normalize_lie to pick the baseline table.

LIES: list[str] = [
    "Tee", "Fairway", "Semi", "Rough", "Deep", "Deep Rough",
    "Sand", "Bunker", "Fringe", "Green", "Recovery",
]


def snap_lie(raw: str) -> tuple[str, bool]:
    """
    Snap a lie label.

    This matters more than it looks: normalize_lie falls back to Fairway for
    anything it does not recognise, so an OCR'd 'Grean' would silently be scored
    as a fairway lie — a large SG error on what was really a putt.
    """
    return snap(raw, LIES, cutoff=0.6)


# ── Courses (learned) ──────────────────────────────────────────────────────────

def clean_course(raw: str) -> str:
    """Strip OCR trailing junk: 'Rock Bay Golf:' / 'Rock Bay Golf |' → 'Rock Bay Golf'."""
    t = re.sub(r"[^A-Za-z0-9 '&\-]+", " ", raw or "")
    return " ".join(t.split()).strip(" -'")


class CourseBook:
    """
    Remembers course names seen on VALID captures and snaps new reads to them.

    Only observe() captures that passed validation, so menu-screen garbage never
    becomes a known course. A course becomes a snap target once seen
    `establish_after` times; until then reads pass through as cleaned text.
    """

    def __init__(self, path: Path, establish_after: int = 3) -> None:
        self.path = Path(path)
        self.establish_after = establish_after
        self._lock = threading.Lock()
        self._data: dict[str, int] = {}
        try:
            import storage
            raw = storage.load_json(self.path, expect=dict, backups=2,
                                    what="Course list") or {}
            self._data = {str(k): int(v) for k, v in raw.items()}
        except Exception as exc:          # a lost course list is relearned
            log.warning("Course list unavailable (%s) — relearning", exc)

    def established(self) -> list[str]:
        with self._lock:
            return [k for k, n in self._data.items() if n >= self.establish_after]

    def snap(self, raw: str) -> tuple[str, bool]:
        cleaned = clean_course(raw)
        if not cleaned:
            return "", False
        known = self.established()
        if not known:
            return cleaned, False
        value, snapped = snap(cleaned, known, cutoff=0.8)
        return (value, snapped) if snapped else (cleaned, False)

    def observe(self, name: str) -> None:
        """Record a course read from a capture that passed validation."""
        name = clean_course(name)
        if len(name) < 3:
            return
        with self._lock:
            # Fold into an existing entry when it is clearly the same course.
            if self._data:
                value, snapped = snap(name, list(self._data), cutoff=0.8)
                if snapped:
                    # Prefer the longer spelling: a truncated read
                    # ('The Hills Cours') is upgraded if a full one appears.
                    if len(name) > len(value) and _norm(name).startswith(_norm(value)):
                        self._data[name] = self._data.pop(value)
                        value = name
                    self._data[value] += 1
                    self._save()
                    return
            self._data[name] = self._data.get(name, 0) + 1
            self._save()

    def _save(self) -> None:
        try:
            import storage
            storage.save_json(self.path, self._data, backups=2)
        except Exception as exc:
            log.debug("CourseBook: could not save %s — %s", self.path, exc)
