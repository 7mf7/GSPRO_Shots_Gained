#!/usr/bin/env python3
"""
parsing.py — Turn one capture's raw OCR text into typed, validated fields.

Pure functions: no screen access, no files, no clock. Used identically by the
live daemon and by rebuild_from_log.py, so a capture replayed from the log is
interpreted exactly as it was live.

  parse_ocr_fields   raw text by cell → typed fields, with vocabulary snapping
  _capture_is_valid  plausibility gate (menus, loading screens)
  parse_cell         "K1" → (row, col)
"""

from __future__ import annotations

import logging
import re

import strokes_gained as sg_mod

try:
    import ocr_vocab as vocab
    _VOCAB_OK = True
except ImportError:                 # snapping disabled, raw text passes through
    vocab = None
    _VOCAB_OK = False

log = logging.getLogger(__name__)


_DIGITS_ONLY = re.compile(r"^(-?)(\d{2,})$")


def _one_decimal(text: str) -> str:
    """
    GSPro shows VLA, HLA, carries, total length (in yards), ball speed and spin
    axis with exactly one decimal place. Tesseract sometimes drops the point
    ("-4.5" read as "-45", seen in real play), so a reading that is digits only
    gets its point back before the last digit. Anything else is left alone.
    """
    m = _DIGITS_ONLY.match((text or "").strip())
    return f"{m.group(1)}{m.group(2)[:-1]}.{m.group(2)[-1]}" if m else text

def _wind_speed(text: str) -> str:
    """'12 MPH' → '12'; '' when no number was read."""
    m = re.search(r"\d+", text or "")
    return str(int(m.group())) if m else ""


# The learned course list. Set by the daemon (or rebuild tool) at startup;
# None disables course snapping.
COURSE_BOOK = None


def parse_cell(address: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", address.strip())
    if not m:
        raise ValueError(f"Invalid cell address: {address!r}")
    col_str, row_str = m.groups()
    col = 0
    for ch in col_str.upper():
        col = col * 26 + (ord(ch) - 64)
    return int(row_str) - 1, col - 1

def _parse_round_score(text: str) -> int | None:
    """
    Parse a GSPro round-score string into a signed integer.

    GSPro displays the cumulative score versus par for the round:
        "E" or "EVEN"  →   0  (even par)
        "-3"           →  -3  (3 under)
        "+2"           →  +2  (2 over)

    A number WITHOUT a sign ("6") returns None: GSPro always shows "+" over par,
    so a bare number means the minus sign was lost (seen in play: "-6" read as
    "6", which was taken as +6). See _round_score_unsigned for its magnitude.

    Returns None if the text is blank, a plain dash, or unparseable
    (e.g. OCR noise), so callers can distinguish "not captured" from 0.
    """
    t = text.strip().upper().replace(" ", "")
    if not t or t in ("-", "—", "N/A"):
        return None
    if t in ("E", "EVEN"):
        return 0
    t = t.replace("−", "-").replace("–", "-")
    return int(t) if re.fullmatch(r"[+-]\d{1,2}", t) else None   # "+3" → 3, "-2" → -2


def _end_of_round_strokes(number: str, label: str) -> int | None:
    """
    GSPro's end-of-round total (T-28, v3.8.2): the number shown above "SCORE"
    on the end-of-round screen, read only when that screen was detected (see
    ocr_daemon._read_end_screen). None unless the label really says SCORE.
    """
    n = (number or "").strip()
    if "SCORE" not in (label or "").upper().replace(" ", "") or not re.fullmatch(r"\d{1,3}", n):
        return None
    v = int(n)
    return v if 1 <= v <= 300 else None


def _round_score_unsigned(text: str) -> int | None:
    """The size of a round score read without its sign ("6"), else None."""
    t = (text or "").strip().replace(" ", "")
    return int(t) if re.fullmatch(r"\d{1,2}", t) else None

def _percent(text: str) -> int | None:
    """'80%' → 80; None unless a whole percentage from 1 to 100."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*", text or "")
    v = int(m.group(1)) if m else None
    return v if v is not None and 1 <= v <= 100 else None


def parse_ocr_fields(by_cell: dict[str, str], by_name: dict[str, str]) -> dict:
    """
    Extract typed values from OCR results, keyed two ways:

    by_cell  — keyed by cell address ("A1", "B1", …).  Used for all fixed
                GSPro fields.  Cell addresses are unique, so this is robust
                even when two regions share the same name (e.g. both wind
                captures are named "wind direction" in config.json but live
                in cells E1 and T1 respectively).

    by_name  — keyed by region name.  Used only for user-added fields whose
                cell address may vary (currently: "hole score").

    Adding a new captured field later is a two-step process:
      1. Add the region to config.json via region_selector.py.
      2. Add a line here referencing its cell address.
    """
    # ── Lie: prefer Shot Loc 1 (G1), fall back to Shot Loc 2 (J1) ──────────────
    loc1_raw = by_cell.get("G1", "").strip()
    loc2_raw = by_cell.get("J1", "").strip()
    lie_raw  = loc1_raw or loc2_raw

    # ── Snap closed-vocabulary fields to known values ─────────────────────────
    # Raw OCR is kept on the *_raw keys and in Sheet1 (the audit trail); the
    # snapped value is what feeds strokes gained and the SG sheets.
    club_raw   = by_cell.get("I1", "").strip()
    course_raw = by_cell.get("A1", "").strip()
    score_raw  = by_cell.get("U1", "")
    club, course, lie_text = club_raw, course_raw, lie_raw
    if _VOCAB_OK:
        club = vocab.snap_club(club_raw)[0] or club_raw
        if lie_raw:
            snapped_lie, ok = vocab.snap_lie(lie_raw)
            lie_text = snapped_lie if ok else lie_raw
        if COURSE_BOOK is not None:
            course = COURSE_BOOK.snap(course_raw)[0]
        else:
            course = vocab.clean_course(course_raw)
        score_raw = vocab.repair_score_text(score_raw)
        for label, raw, val in (("club", club_raw, club),
                                ("lie", lie_raw, lie_text),
                                ("course", course_raw, course)):
            if raw and val and raw != val:
                log.info("  snap %-6s %r → %r", label, raw, val)

    return {
        # ── Identifiers ──────────────────────────────────────────────────────────
        "course":       course,
        "course_raw":   course_raw,
        "hole":         sg_mod.parse_int(by_cell.get("B1", ""), 0),
        "par":          sg_mod.parse_int(by_cell.get("C1", ""), 4),
        "hole_yardage": sg_mod.parse_distance(by_cell.get("D1", "")),

        # ── SG inputs ─────────────────────────────────────────────────────────────
        "remaining": sg_mod.parse_distance(by_cell.get("K1", "")),
        "lie":       sg_mod.normalize_lie(lie_text),
        "lie_raw":   lie_raw,

        # ── Launch monitor ────────────────────────────────────────────────────────
        "carry_game":   sg_mod.parse_distance(_one_decimal(by_cell.get("N1", ""))),
        "carry_raw":    sg_mod.parse_distance(_one_decimal(by_cell.get("O1", ""))),
        "total_length": sg_mod.parse_distance(_one_decimal(by_cell.get("P1", ""))),
        "ball_speed":   sg_mod.parse_number(_one_decimal(by_cell.get("Q1", ""))),
        "spin_axis":    sg_mod.parse_number(_one_decimal(by_cell.get("R1", ""))),
        "total_spin":   sg_mod.parse_number(by_cell.get("S1", "")),
        "vla":          sg_mod.parse_number(_one_decimal(by_cell.get("L1", ""))),
        "hla":          sg_mod.parse_number(_one_decimal(by_cell.get("M1", ""))),

        # ── Environment / context ─────────────────────────────────────────────────
        # E1 and T1 are two separate wind captures. Storing both lets future
        # analysis compare outcomes under headwind vs crosswind conditions.
        "wind_1":       by_cell.get("E1", "").strip(),
        "wind_speed":   _wind_speed(by_cell.get("E1", "")),
        # A direction is meaningless in still air; the arrow still points somewhere.
        "wind_2":       ("calm" if _wind_speed(by_cell.get("E1", "")) == "0"
                         else by_cell.get("T1", "").strip()),
        "height_delta": by_cell.get("F1", "").strip(),   # elevation Δ to green
        "lie_angles":   by_cell.get("H1", "").strip(),   # hardware lie-angle reading
        # GSPro's LAST SHOT panel (T-08): speed and spin kept after a shot from
        # a penalising lie; None when the panel is not showing.
        "speed_kept":   _percent(by_cell.get("X1", "")),
        "spin_kept":    _percent(by_cell.get("Y1", "")),
        "shot_loc_1":   loc1_raw,                        # raw text before normalise
        "shot_loc_2":   loc2_raw,
        "region9":      club,          # I1 is the club field; snapped to a known club
        "club":         club,
        "club_raw":     club_raw,

        # ── Scoring fields ────────────────────────────────────────────────────────
        # U1 "current score": cumulative round score vs par (e.g. -2, E, +1).
        # GSPro updates this after each hole completes.  The DIFFERENCE between
        # the value at hole N start and the stored value from hole N-1 start
        # gives the exact stroke count for hole N-1.
        "round_score":       _parse_round_score(score_raw),
        "round_score_unsigned": _round_score_unsigned(score_raw),   # sign lost by the OCR
        # W1/W2 are read only while GSPro's end-of-round screen is showing.
        "end_of_round_strokes": _end_of_round_strokes(by_cell.get("W1", ""), by_cell.get("W2", "")),

        # V1 "current hole score ticker": real-time stroke count on this hole,
        # white-on-black display.  Used as fallback when round_score is unavailable.
        "hole_score_ticker": sg_mod.parse_int(by_cell.get("V1", ""), 0),
    }

def _normalize_course(name: str) -> str:
    """
    Reduce an OCR'd course name to a comparable key.

    The same course reads as 'Rock Bay Golf:', 'Rock Bay Golf |' and
    'Rock Bay Golf' across captures, so a raw string comparison would fire a
    false session boundary on nearly every hole. Strip trailing punctuation and
    case, and compare on that.
    """
    t = (name or "").strip().lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())

# Validation bounds, overridable from config.json.
#
# The lower yardage bound exists only to reject blank/menu screens, NOT to
# police course design. Practice and distance-challenge modes legitimately use
# 25, 50, 100 and 150 yd stations, and a short par 3 can sit under 100 yd, so
# the floor is deliberately permissive — the hole-number and par checks are what
# actually catch a menu screen. An earlier 30 yd floor silently discarded 70
# perfectly good captures from a 25 yd station.
_VALIDATION: dict = {
    "max_hole_number":  18,
    "min_hole_yardage": 5,
    "max_hole_yardage": 750,
}

def _capture_is_valid(parsed: dict) -> tuple[bool, str]:
    """
    Sanity-check a capture before any of it is written or fed to the state
    machine.  Returns (ok, reason_if_not_ok).

    This gate exists because a bad capture does not just produce one bad row:
    a misread `remaining` becomes `pre_distance` for the NEXT shot, so a single
    garbage frame corrupts every SG value for the rest of the hole.  Rejecting
    early costs one skipped capture; accepting silently costs the hole.

    It matters most in auto-capture mode, where no human is watching each frame.
    """
    hole = parsed.get("hole", 0)
    par  = parsed.get("par", 0)
    yard = parsed.get("hole_yardage", 0.0)
    rem  = parsed.get("remaining", 0.0)

    max_hole = int(_VALIDATION.get("max_hole_number", 18))
    min_yd   = float(_VALIDATION.get("min_hole_yardage", 5))
    max_yd   = float(_VALIDATION.get("max_hole_yardage", 750))

    if not 1 <= hole <= max_hole:
        return False, f"hole number {hole!r} outside 1-{max_hole}"
    if not 3 <= par <= 5:
        return False, f"par {par!r} outside 3-5"
    if yard <= 0:
        return False, "hole yardage missing or zero"
    if not min_yd <= yard <= max_yd:
        return False, (f"hole yardage {yard:.0f} outside "
                       f"{min_yd:.0f}-{max_yd:.0f} yd")
    if rem < 0 or rem > max_yd:
        return False, f"remaining distance {rem:.0f} implausible"
    # Tolerance is relative on short holes: a 25 yd station overshot to 40 yd is
    # a real, recordable result, not a misread. An absolute margin alone would
    # either reject those or wave through nonsense on a 600 yd hole.
    if rem > max(yard * 2.0, yard + 50.0):
        return False, f"remaining ({rem:.0f}) far exceeds hole length ({yard:.0f})"
    return True, ""
