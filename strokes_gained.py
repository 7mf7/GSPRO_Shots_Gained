#!/usr/bin/env python3
"""
strokes_gained.py — Baseline tables and SG calculations for GSPro.

All distances are in yards.
Baseline values approximate PGA Tour ShotLink averages (Broadie, 2014),
suitable for benchmarking against a scratch-golfer standard.

Public API
----------
expected_strokes(distance_yards, lie)              -> float
normalize_lie(raw_text)                            -> str
shot_category(shot_number, pre_lie, pre_distance)  -> str
sg_shot(pre_dist, pre_lie, post_dist, post_lie)    -> float
parse_distance(text)                               -> float
parse_number(text)                                 -> float | None
parse_int(text, default)                           -> int
"""
from __future__ import annotations

import math
import re

# ── Lie type constants ─────────────────────────────────────────────────────────

LIE_TEE         = "Tee"
LIE_FAIRWAY     = "Fairway"
LIE_ROUGH       = "Rough"
LIE_HEAVY_ROUGH = "Heavy Rough"   # backward-compat alias → same table as LIE_RECOVERY
LIE_RECOVERY    = "Recovery"      # trees, deep rough, penalty areas
LIE_BUNKER      = "Bunker"
LIE_FRINGE      = "Fringe"
LIE_GREEN       = "Green"

# ── SG category constants ──────────────────────────────────────────────────────

CAT_OTT  = "Off Tee"
CAT_APP  = "Approach"
CAT_ATG  = "Around Green"
CAT_PUTT = "Putting"
ALL_CATS = [CAT_OTT, CAT_APP, CAT_ATG, CAT_PUTT]     # measured shots

# Strokes the capture missed but GSPro's round score proves happened. Their
# hole and round totals are exact, but their start positions are unknown, so
# they cannot honestly be filed under any of the four categories above. They
# used to land in Putting — on the log tested, 89% of putting SG came from them.
CAT_UNATTRIBUTED = "Unattributed"
SG_CATS = ALL_CATS + [CAT_UNATTRIBUTED]              # every stroke

# Shots within this distance from the hole (and not on the green) are ATG
ATG_THRESHOLD_YD = 30.0

# ── Baseline tables ────────────────────────────────────────────────────────────
# Format: list of (distance_yards, expected_strokes_to_hole), sorted ascending.
# Distance 0 = ball is holed = 0 strokes remaining.
# Linear interpolation is used between knots; linear extrapolation beyond last.
#
# SOURCE DATA (100-300 yd for Tee/Fairway; 20-300 yd for Rough/Sand/Recovery):
#   Dist  Tee    Fairway  Rough   Sand    Recovery
#    20            —       2.40   2.59    2.53
#    40            —       2.60   2.78    2.82
#    60            —       2.70   2.91    3.15
#    80            —       2.75   2.96    3.24
#   100   2.92    2.80    3.02   3.23    3.80
#   120   2.99    2.85    3.08   3.21*   3.78*
#   140   2.97*   2.91    3.15   3.22    3.80
#   160   2.99    2.98    3.23   3.28    3.81
#   180   3.05    3.08    3.31   3.40    3.82
#   200   3.12    3.19    3.42   3.55    3.87
#   220   3.17    3.32    3.53   3.70    3.92
#   240   3.25    3.45    3.64   3.84    3.97
#   260   3.45    3.58    3.74   3.93    4.03
#   280   3.65    3.69    3.83   4.00    4.10
#   300   3.71    3.78    3.90   4.04    4.20
# (*) Values marked with * are non-monotone in the raw source data and are
#     reproduced exactly as provided.

# ── Green (putting) ───────────────────────────────────────────────────────────
# Source: Broadie / PGA Tour ShotLink (feet converted to yards).
# Sub-3 ft values extrapolated for smooth interpolation.
# Stored NATIVELY IN FEET. The ShotLink putting data is tabulated in feet, so
# holding it in yards forced every knot through a rounding step (5 ft became
# 1.667 yd, i.e. 5.001 ft) — harmless to SG at ~0.0001 strokes, but it is what
# put exact-foot putts into the wrong distance band. Lookups convert yards →
# feet once, at the boundary, and interpolate on the true source values.
_T_GREEN_FT: list[tuple[float, float]] = [
    (0.0,  0.00),   # holed
    (1.0,  1.00),   # extrapolated — tap-in
    (2.0,  1.02),   # extrapolated
    (3.0,  1.04),   # ← PGA Tour ShotLink data begins
    (4.0,  1.13),
    (5.0,  1.23),
    (6.0,  1.34),
    (7.0,  1.42),
    (8.0,  1.50),
    (9.0,  1.56),
    (10.0, 1.61),
    (15.0, 1.78),
    (20.0, 1.87),
    (30.0, 1.98),
    (40.0, 2.06),
    (50.0, 2.14),
    (60.0, 2.21),
    (90.0, 2.40),
]

# Yard view of the same table, exact (no rounding), for code that works in the
# canonical internal unit.
_T_GREEN: list[tuple[float, float]] = [(ft / 3.0, v) for ft, v in _T_GREEN_FT]

# ── Tee ───────────────────────────────────────────────────────────────────────
# Source table: 100-300 yd every 20 yd.
# Below 100 yd: smoothed estimates to connect (0,0) → (100, 2.92).
# Short par-3 tee shots < 100 yd are rare; estimates follow fairway shape.
_T_TEE: list[tuple[float, float]] = [
    (0.,   0.00),
    (5.,   1.90),   # ← sub-100 yd estimates
    (10.,  2.15),
    (20.,  2.35),
    (30.,  2.52),
    (50.,  2.68),
    (75.,  2.82),
    (100., 2.92),   # ← source table begins
    (120., 2.99),
    (140., 2.97),   # non-monotone in source; reproduced exactly
    (160., 2.99),
    (180., 3.05),
    (200., 3.12),
    (220., 3.17),
    (240., 3.25),
    (260., 3.45),
    (280., 3.65),
    (300., 3.71),
]

# ── Fairway ───────────────────────────────────────────────────────────────────
# Source table: 100-300 yd every 20 yd.
# Below 100 yd: smoothed estimates to connect (0,0) → (100, 2.80).
_T_FAIRWAY: list[tuple[float, float]] = [
    (0.,   0.00),
    (5.,   1.90),   # ← sub-100 yd estimates
    (10.,  2.15),
    (20.,  2.35),
    (30.,  2.50),
    (50.,  2.62),
    (75.,  2.72),
    (100., 2.80),   # ← source table begins
    (120., 2.85),
    (140., 2.91),
    (160., 2.98),
    (180., 3.08),
    (200., 3.19),
    (220., 3.32),
    (240., 3.45),
    (260., 3.58),
    (280., 3.69),
    (300., 3.78),
]

# ── Rough ─────────────────────────────────────────────────────────────────────
# Source table: 20-300 yd every 20 yd.
# Below 20 yd: smoothed estimates to connect (0,0) → (20, 2.40).
_T_ROUGH: list[tuple[float, float]] = [
    (0.,   0.00),
    (5.,   1.60),   # ← sub-20 yd estimates
    (10.,  1.90),
    (15.,  2.15),
    (20.,  2.40),   # ← source table begins
    (40.,  2.60),
    (60.,  2.70),
    (80.,  2.75),
    (100., 3.02),
    (120., 3.08),
    (140., 3.15),
    (160., 3.23),
    (180., 3.31),
    (200., 3.42),
    (220., 3.53),
    (240., 3.64),
    (260., 3.74),
    (280., 3.83),
    (300., 3.90),
]

# ── Sand / Bunker ─────────────────────────────────────────────────────────────
# Source table: 20-300 yd every 20 yd.
# Below 20 yd: smoothed estimates to connect (0,0) → (20, 2.59).
# Note: 120 yd (3.21) < 100 yd (3.23) in source — reproduced exactly.
_T_BUNKER: list[tuple[float, float]] = [
    (0.,   0.00),
    (5.,   1.80),   # ← sub-20 yd estimates
    (10.,  2.10),
    (15.,  2.35),
    (20.,  2.59),   # ← source table begins
    (40.,  2.78),
    (60.,  2.91),
    (80.,  2.96),
    (100., 3.23),
    (120., 3.21),   # non-monotone in source; reproduced exactly
    (140., 3.22),
    (160., 3.28),
    (180., 3.40),
    (200., 3.55),
    (220., 3.70),
    (240., 3.84),
    (260., 3.93),
    (280., 4.00),
    (300., 4.04),
]

# ── Recovery (trees, deep rough, penalty areas) ───────────────────────────────
# Source table: 20-300 yd every 20 yd.
# Below 20 yd: smoothed estimates to connect (0,0) → (20, 2.53).
# Note: 120 yd (3.78) < 100 yd (3.80) in source — reproduced exactly.
_T_RECOVERY: list[tuple[float, float]] = [
    (0.,   0.00),
    (5.,   1.70),   # ← sub-20 yd estimates
    (10.,  2.00),
    (15.,  2.28),
    (20.,  2.53),   # ← source table begins
    (40.,  2.82),
    (60.,  3.15),
    (80.,  3.24),
    (100., 3.80),
    (120., 3.78),   # non-monotone in source; reproduced exactly
    (140., 3.80),
    (160., 3.81),
    (180., 3.82),
    (200., 3.87),
    (220., 3.92),
    (240., 3.97),
    (260., 4.03),
    (280., 4.10),
    (300., 4.20),
]

# ── Fringe / collar ───────────────────────────────────────────────────────────
# Not provided in the source table; retained from prior estimates.
_T_FRINGE: list[tuple[float, float]] = [
    (0.00, 0.00),
    (1.00, 1.15),
    (2.00, 1.35),
    (3.00, 1.55),
    (5.00, 1.78),
    (7.00, 1.95),
    (10.0, 2.15),
    (15.0, 2.35),
    (20.0, 2.48),
    (30.0, 2.62),
    (40.0, 2.73),
    (50.0, 2.81),
]

# ── Lookup (case-insensitive key) ─────────────────────────────────────────────
_TABLES: dict[str, list[tuple[float, float]]] = {
    "tee":          _T_TEE,
    "fairway":      _T_FAIRWAY,
    "rough":        _T_ROUGH,
    "recovery":     _T_RECOVERY,
    "heavy rough":  _T_RECOVERY,   # alias
    "heavy_rough":  _T_RECOVERY,   # alias
    "bunker":       _T_BUNKER,
    "sand":         _T_BUNKER,
    "fringe":       _T_FRINGE,
    "collar":       _T_FRINGE,
    "apron":        _T_FRINGE,
    "green":        _T_GREEN,
    "putting":      _T_GREEN,
}

# ── Interpolation ──────────────────────────────────────────────────────────────

def _interp(table: list[tuple[float, float]], dist: float) -> float:
    """Linear interpolation (and extrapolation) in a (dist, strokes) table."""
    if dist <= 0:
        return 0.0
    if dist <= table[0][0]:
        return table[0][1]
    if dist >= table[-1][0]:
        x0, y0 = table[-2]
        x1, y1 = table[-1]
        return y1 + (y1 - y0) / (x1 - x0) * (dist - x1)
    lo, hi = 0, len(table) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        (lo, hi) = (mid, hi) if table[mid][0] <= dist else (lo, mid)
    x0, y0 = table[lo]
    x1, y1 = table[hi]
    return y0 + (y1 - y0) / (x1 - x0) * (dist - x0)


# ── Public API ─────────────────────────────────────────────────────────────────

def expected_strokes(distance_yards: float, lie: str) -> float:
    """
    Expected strokes to hole from *distance_yards* with *lie*.
    Returns 0.0 when distance <= 0 (ball holed).
    """
    if distance_yards <= 0:
        return 0.0
    table = _TABLES.get(lie.lower(), _T_FAIRWAY)
    if table is _T_GREEN:
        # Putting is evaluated in FEET against the native ShotLink table.
        result = round(_interp(_T_GREEN_FT, distance_yards * 3.0), 4)
    else:
        result = round(_interp(table, distance_yards), 4)

    # A ball that is NOT in the hole always needs at least one more stroke.
    # Interpolating the green table between (0 yd, 0.00) and (1 ft, 1.00) gives
    # values below 1.0 for very short putts — arithmetically smooth, physically
    # impossible. This only became reachable once balls inside the gimmie circle
    # stopped being short-circuited as holed; without the floor it silently
    # over-credits the approach that left the ball there.
    if table is _T_GREEN and result < 1.0:
        return 1.0
    return result


def normalize_lie(raw: str) -> str:
    """
    Map GSPro OCR lie text to a canonical lie constant.

    GSPro may display things like 'Fairway', 'Rough', 'Bunker',
    'Green', 'Fringe', 'Heavy Rough', 'Tee Box', etc.
    Unknown text defaults to Fairway.
    """
    t = raw.lower().strip()
    if not t or t in ("n/a", "na", "none", "-", ""):
        return LIE_FAIRWAY
    if "tee"    in t:                               return LIE_TEE
    if "green"  in t and "fringe" not in t \
                      and "collar" not in t:        return LIE_GREEN
    if "fringe" in t or "collar" in t \
                      or "apron"  in t:             return LIE_FRINGE
    if "bunker" in t or "sand"   in t \
                      or "trap"   in t:             return LIE_BUNKER
    if "heavy"  in t or "deep"   in t \
                      or "thick"  in t \
                      or "trees"  in t \
                      or "recovery" in t \
                      or "penalty"  in t \
                      or " ob"      in t:           return LIE_RECOVERY
    if "rough"  in t:                               return LIE_ROUGH
    if "fairway" in t or " fw" in t:                return LIE_FAIRWAY
    return LIE_FAIRWAY


def shot_category(shot_number: int, pre_lie: str, pre_distance: float,
                  par: int | None = None) -> str:
    """
    Return the SG category for a shot.

    shot_number : 1-based count of shots on this hole
    pre_lie     : canonical lie before the shot
    pre_distance: yards to hole before the shot
    par         : the hole's par. The PGA Tour definition counts tee shots on
                  par 3s as APPROACH shots, not Off the Tee; without the par a
                  par-3 tee shot cannot be told apart.
    """
    pl = pre_lie.lower()
    if shot_number == 1 or pl == "tee":
        if par == 3:
            return CAT_ATG if pre_distance <= ATG_THRESHOLD_YD else CAT_APP
        return CAT_OTT
    if pl == "green":
        return CAT_PUTT
    if pre_distance <= ATG_THRESHOLD_YD:
        return CAT_ATG
    return CAT_APP


def sg_shot(
    pre_dist:  float, pre_lie:  str,
    post_dist: float, post_lie: str,
) -> float:
    """
    Strokes Gained for a single shot.
      > 0 : better than the scratch-golfer baseline
      < 0 : worse than the scratch-golfer baseline

    Formula: SG = E(start) - 1 - E(finish)
    where E(x) = expected_strokes from position x.
    """
    return round(
        expected_strokes(pre_dist, pre_lie) - 1.0 - expected_strokes(post_dist, post_lie),
        3,
    )


# ── Parsing helpers ────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"[+-]?[\d]+(?:\.[\d]+)?")


# Characters Tesseract commonly emits for digits in the small numeric HUD
# fields. Applied ONLY inside distance parsing, where the value must be numeric
# — a global substitution would corrupt course names and lie descriptions.
_DIGIT_REPAIR = str.maketrans({
    "o": "0", "O": "0", "Q": "0", "D": "0",
    "l": "1", "I": "1", "|": "1", "!": "1",
    "S": "5", "s": "5", "B": "8", "&": "8", "$": "9",
})
_JUNK_RE = re.compile(r"[^0-9.'\"\s-]")


def _clean_numeric(token: str) -> str | None:
    """Repair known digit misreads; return None if anything unresolvable remains."""
    t = token.translate(_DIGIT_REPAIR)
    t = re.sub(r"[¢°ºo]+$", "", t)          # trailing OCR speckle
    return t if re.fullmatch(r"-?\d+(?:\.\d+)?", t) else None


def parse_distance(text: str) -> float:
    """
    Parse a distance string and return YARDS. Returns 0.0 on failure.

    GSPro switches units and notation by magnitude, so one field can arrive in
    several shapes:

        '150'        bare number      → yards
        '25.0yd'     explicit yards   → yards
        '29ft 4'     feet + inches    → 29 ft 4 in
        '15 ft 4'    feet + inches    → 15 ft 4 in
        "18' 2\""     feet + inches    → 18 ft 2 in
        '4ft'        feet only        → 4 ft
        '12.5 m'     metres           → converted

    The feet-plus-inches forms are the important ones: they appear on short
    shots and putts, and reading only the leading number (the previous
    behaviour) turned 29 ft 4 in into 29 YARDS — a threefold overstatement that
    silently corrupted every short-shot length and any geometry derived from it.
    """
    if not text:
        return 0.0
    t = text.strip().lower().replace("\\", "")
    if not t:
        return 0.0

    is_ft = ("ft" in t or "feet" in t or "foot" in t or "'" in t)
    is_m  = ("m" in t and "yd" not in t and "mile" not in t and not is_ft)

    if is_ft:
        # Split on the FEET separator first, then strip spaces inside each part.
        # OCR frequently breaks a two-digit number across a space ("1 8'" for
        # 18'), so tokenising on whitespace before splitting on the unit would
        # read 18 ft 2 in as 1 ft 8 in.
        sep = "'" if "'" in t else ("ft" if "ft" in t else
                                    ("feet" if "feet" in t else "foot"))
        head, _, tail = t.partition(sep)
        feet   = _clean_numeric(re.sub(r"\s+", "", head))
        inch_s = _clean_numeric(re.sub(r"[^0-9.]", "", tail))
        if feet is None:
            return 0.0
        inches = float(inch_s) if (inch_s is not None and 0 <= float(inch_s) < 12) else 0.0
        return round((float(feet) + inches / 12.0) / 3.0, 3)

    # Strip unit words FIRST, then require what remains to be fully numeric.
    # Repairing before stripping would let a unit letter survive into the number
    # ('100m' → token '100m' → unparseable), while skipping the strict check
    # entirely would turn OCR noise like 'Bit' into 8.
    body = re.sub(r"(yards|yds|yd|meters|metres|m)\b", " ", t)
    body = re.sub(r"\s+", "", body)
    c = _clean_numeric(body)
    if c is None:
        tok = re.search(r"-?\d+(?:\.\d+)?", body)
        if not tok:
            return 0.0
        c = tok.group()
    val = float(c)
    if is_m:
        return round(val * 1.0936, 2)
    return round(val, 2)


def parse_number(text: str) -> float | None:
    """Extract the first numeric value from text, or None."""
    if not text:
        return None
    m = _NUM_RE.search(text.strip())
    return float(m.group()) if m else None


def parse_int(text: str, default: int = 0) -> int:
    """Parse first integer from text, returning *default* on failure."""
    v = parse_number(text)
    return int(round(v)) if v is not None else default


# ── Player vs PGA Tour comparison ─────────────────────────────────────────────

# ── Distance bands (derived from the baseline tables) ─────────────────────────
# Bands are BUILT FROM the table knots above rather than written out separately,
# so they can never drift out of step with the baselines. Change a table and the
# reporting bands follow automatically.
#
# Putting uses the green table's native resolution, which is in FEET (the
# ShotLink source is tabulated in feet: 3, 4, 5 … 10, 15, 20, 30 … 90 ft).
# Everything else uses the 20-yard source-table steps.

FT_PER_YD = 3.0


def _bands_from_knots(knots: list[float]) -> list[tuple[float, float]]:
    """Turn an ascending knot list into contiguous [lo, hi) bands, plus a tail."""
    edges = [k for k in knots if k > 0]
    bands = [(0.0, edges[0])]
    bands += [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    bands.append((edges[-1], float("inf")))
    return bands


# Putting bands in FEET, straight off the green table's knots.
# Taken directly from the feet-native table, so the edges are exact.
PUTT_BAND_EDGES_FT: list[float] = [ft for ft, _ in _T_GREEN_FT if ft > 0]
PUTT_BANDS: list[tuple[float, float]] = _bands_from_knots(PUTT_BAND_EDGES_FT)

# Yardage bands from the 20-yd source tables (rough/sand/recovery share the
# widest span: 20 → 300 in 20 yd steps). Sub-20 detail is left to the ATG/putting
# categories, which report at finer resolution where it matters.
YARD_BAND_EDGES: list[float] = [d for d, _ in _T_ROUGH if d >= 20]
YARD_BANDS: list[tuple[float, float]] = _bands_from_knots(YARD_BAND_EDGES)

# Around-the-green work happens inside ATG_THRESHOLD_YD, where the yard bands are
# far too coarse. Use the fringe table's knots, which are tabulated for exactly
# this range.
ATG_BAND_EDGES: list[float] = [d for d, _ in _T_FRINGE if 0 < d <= ATG_THRESHOLD_YD]
ATG_BANDS: list[tuple[float, float]] = _bands_from_knots(ATG_BAND_EDGES)


def _fmt_band(lo: float, hi: float, unit: str) -> str:
    """Human label for a band, e.g. '4-5 ft', '100-120 yd', '300+ yd'."""
    def n(v: float) -> str:
        return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:g}"
    if hi == float("inf"):
        return f"{n(lo)}+ {unit}"
    if lo == 0:
        return f"0-{n(hi)} {unit}"
    return f"{n(lo)}-{n(hi)} {unit}"


_BAND_EPS = 1e-6


def _band_index(value: float, bands: list[tuple[float, float]]) -> int:
    """
    Locate the [lo, hi) band containing *value*.

    A tolerance is applied at the lower edge so a value that is a hair under an
    edge through floating-point arithmetic (119.99999999 for 120) still lands in
    the band that edge opens, matching the [lo, hi) convention the labels imply.
    """
    for i, (lo, hi) in enumerate(bands):
        if (lo - _BAND_EPS) <= value < (hi - _BAND_EPS):
            return i
    return len(bands) - 1


def distance_band(distance_yards: float, category: str) -> str:
    """
    Label the distance band a shot was played FROM, at the resolution the
    baseline table for that category actually supports.

    Putting  → feet, green-table knots (3, 4, 5 … 10, 15, 20, 30 … 90 ft)
    Around   → yards, fringe-table knots (1, 2, 3, 5, 7, 10, 15, 20, 30 yd)
    Off Tee  → yards, 20-yd source-table steps
    Approach → yards, 20-yd source-table steps
    """
    d = max(0.0, float(distance_yards or 0.0))
    if category == CAT_PUTT:
        ft = d * FT_PER_YD
        lo, hi = PUTT_BANDS[_band_index(ft, PUTT_BANDS)]
        return _fmt_band(lo, hi, "ft")
    if category == CAT_ATG:
        lo, hi = ATG_BANDS[_band_index(d, ATG_BANDS)]
        return _fmt_band(lo, hi, "yd")
    lo, hi = YARD_BANDS[_band_index(d, YARD_BANDS)]
    return _fmt_band(lo, hi, "yd")


def band_order(category: str) -> list[str]:
    """All band labels for a category, near-to-far, for stable report ordering."""
    if category == CAT_PUTT:
        return [_fmt_band(lo, hi, "ft") for lo, hi in PUTT_BANDS]
    if category == CAT_ATG:
        return [_fmt_band(lo, hi, "yd") for lo, hi in ATG_BANDS]
    return [_fmt_band(lo, hi, "yd") for lo, hi in YARD_BANDS]


# ── Unit presentation ─────────────────────────────────────────────────────────
# Everything is COMPUTED in yards (one canonical unit avoids mixed-unit bugs),
# but anything on the green is PRESENTED in feet, because that is how putting
# distances are read, discussed and benchmarked. Route every display of a
# distance through these two functions so the rule lives in exactly one place.

def display_unit(lie: str) -> str:
    """'ft' for a ball on the green, 'yd' everywhere else."""
    return "ft" if normalize_lie(lie or "") == LIE_GREEN else "yd"


def to_display(distance_yards: float, lie: str) -> float:
    """Convert a canonical yard distance into its display unit for *lie*."""
    d = float(distance_yards or 0.0)
    return d * FT_PER_YD if display_unit(lie) == "ft" else d


def format_distance(distance_yards: float, lie: str, decimals: int = 1) -> str:
    """'12.0 ft' on the green, '150.0 yd' elsewhere."""
    return f"{to_display(distance_yards, lie):.{decimals}f} {display_unit(lie)}"


# ── Shot geometry: short / long / offline ─────────────────────────────────────

# Below this pre-shot distance the 1/(2D) term amplifies any length error so
# badly that the result is noise.
#
# Putts get their own floor, set in FEET. The original single 10 yd floor
# excluded every putt under 30 ft — i.e. nearly all of them — so putting
# short/long, the most useful putting statistic there is ("never up, never in"),
# was never evaluated at all. With the feet-and-inches length parsing fixed,
# putts down to a few feet now carry enough signal.
MIN_GEOMETRY_DISTANCE_YD     = 10.0
MIN_GEOMETRY_DISTANCE_PUTT_FT = 6.0

# "Pin high" tolerance: inside this the shot is neither short nor long. Putts
# use a much tighter band — a putt finishing 1 ft past is long in lag terms.
PIN_HIGH_TOLERANCE_YD     = 0.5
PIN_HIGH_TOLERANCE_PUTT_FT = 0.25


def shot_geometry(pre_dist: float, length: float, remaining: float,
                  pre_lie: str = "") -> dict | None:
    """
    Resolve a shot into distance control (short/long) and direction (offline).

    Every shot forms a triangle:
        start→pin      = pre_dist   (D)
        start→ball end = length     (L)
        ball end→pin   = remaining  (R)

    The law of cosines gives the shot's component ALONG the pin line:

        along = (D² + L² − R²) / (2D)

    which is the only way to separate a distance-control miss from a directional
    one. Comparing L to D alone cannot: a 150 yd shot flying 155 yd and finishing
    20 yd away looks 5 yd long, but is really 3.8 yd past pin-high with 19.6 yd
    of lateral miss — the distance was fine, the aim was not.

    Returns None when the inputs cannot support a trustworthy answer:
      * pre_dist below MIN_GEOMETRY_DISTANCE_YD (error amplification)
      * any input missing or non-positive length
      * ball did not move (remaining unchanged — capture preceded the shot)
      * geometry impossible (along > length), meaning one field is misread

    Returning None rather than a number is deliberate: a wrong short/long
    reading is worse than none, because it looks authoritative.
    """
    if not pre_dist or not length or remaining is None:
        return None
    D, L, R = float(pre_dist), float(length), float(remaining)
    putt = normalize_lie(pre_lie or "") == LIE_GREEN
    floor_yd = (MIN_GEOMETRY_DISTANCE_PUTT_FT / FT_PER_YD) if putt else MIN_GEOMETRY_DISTANCE_YD
    tol_yd   = (PIN_HIGH_TOLERANCE_PUTT_FT / FT_PER_YD)    if putt else PIN_HIGH_TOLERANCE_YD
    if D < floor_yd or L <= 0:
        return None
    if abs(R - D) < (0.1 if putt else 0.5) and L > (0.3 if putt else 1.0):
        return None                      # ball did not move: pre-shot capture

    along   = (D * D + L * L - R * R) / (2.0 * D)
    if along > L * 1.02 + 0.5 or along < -L:
        return None                      # inconsistent inputs

    lateral = math.sqrt(max(0.0, L * L - along * along))
    delta   = along - D
    if   delta >  tol_yd: verdict = "Long"
    elif delta < -tol_yd: verdict = "Short"
    else:                 verdict = "Pin high"

    # Values stay in canonical yards; "unit" tells the caller how to present
    # them. Use to_display() / format_distance() rather than converting here.
    return {
        "along_line": round(along, 3),
        "lateral":    round(lateral, 3),
        "delta":      round(delta, 3),      # +long / −short, yards
        "verdict":    verdict,
        "unit":       "ft" if putt else "yd",
    }


def explain_sg(sg_val: float, category: str = "") -> str:
    """
    Return a concise human-readable description of a strokes gained value.

    Positive SG = player performed better than a PGA Tour average.
    Negative SG = player performed worse than a PGA Tour average.

    Examples
    --------
    >>> explain_sg(+0.61)
    '+0.6100 SG — 0.61 strokes BETTER than PGA Tour average'
    >>> explain_sg(-0.72, 'Approach')
    '-0.7200 SG [Approach] — 0.72 strokes WORSE than PGA Tour average'
    """
    direction = "BETTER" if sg_val >= 0 else "WORSE"
    cat_str   = f" [{category}]" if category else ""
    return (
        f"{sg_val:+.4f} SG{cat_str} — "
        f"{abs(sg_val):.4f} strokes {direction} than PGA Tour average"
    )


def shot_analysis(
    pre_dist:  float, pre_lie:  str,
    post_dist: float, post_lie: str,
) -> dict:
    """
    Full breakdown of one shot's strokes gained components.

    All three numbers satisfy the accounting identity:
        sg == pre_expected - player_strokes - post_expected

    Returns
    -------
    dict with keys:
      pre_expected   : expected strokes to hole from start (PGA Tour baseline)
      player_strokes : 1  (one shot was played)
      post_expected  : expected strokes to hole from end (PGA Tour baseline)
      sg             : strokes gained (pre_expected − 1 − post_expected)
      interpretation : human-readable string comparing player to PGA Tour
    """
    pre_exp  = expected_strokes(pre_dist,  pre_lie)
    post_exp = expected_strokes(post_dist, post_lie)
    sg       = sg_shot(pre_dist, pre_lie, post_dist, post_lie)
    return {
        "pre_expected":   round(pre_exp,  4),
        "player_strokes": 1,
        "post_expected":  round(post_exp, 4),
        "sg":             sg,
        "interpretation": explain_sg(sg),
    }
