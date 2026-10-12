#!/usr/bin/env python3
"""
test_strokes_gained.py — Verification suite for strokes_gained.py

Run from the project folder:
    python test_strokes_gained.py
    python test_strokes_gained.py -v      # verbose: print every case even if passing

All tests are self-contained; no external libraries required.
Exit code 0 = all passed, 1 = one or more failures.
"""

from __future__ import annotations
import sys, math, argparse
from pathlib import Path

# ── locate strokes_gained.py ───────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import strokes_gained as sg
except ModuleNotFoundError:
    sys.exit("Cannot find strokes_gained.py — run this script from the project folder.")

# ── tiny test harness ──────────────────────────────────────────────────────────

_passes = _fails = 0
_verbose = False

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _section(title: str) -> None:
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")


def check(label: str, got, expected, tol: float = 0.002) -> bool:
    """Assert |got - expected| <= tol and print a coloured result line."""
    global _passes, _fails
    if isinstance(got, float) and isinstance(expected, float):
        ok = math.isfinite(got) and abs(got - expected) <= tol
        detail = f"{got:.4f}  (expected {expected:.4f})"
    else:
        ok = (got == expected)
        detail = f"{got!r}  (expected {expected!r})"
    if ok:
        _passes += 1
        if _verbose:
            print(f"  {GREEN}✓{RESET}  {label:<52}  {detail}")
    else:
        _fails += 1
        print(f"  {RED}✗{RESET}  {label:<52}  {detail}")
    return ok


def note(msg: str) -> None:
    if _verbose:
        print(f"  {YELLOW}·{RESET}  {msg}")

# ══════════════════════════════════════════════════════════════════════════════
# 1. PUTTING BASELINE — all 15 data points provided by user
# ══════════════════════════════════════════════════════════════════════════════
_section("1 · Putting baseline — exact PGA Tour data points (feet → yards)")

# Table is keyed in yards; distances are feet / 3
PUTT_DATA = [
    ( 3, 1.04), ( 4, 1.13), ( 5, 1.23), ( 6, 1.34),
    ( 7, 1.42), ( 8, 1.50), ( 9, 1.56), (10, 1.61),
    (15, 1.78), (20, 1.87), (30, 1.98), (40, 2.06),
    (50, 2.14), (60, 2.21), (90, 2.40),
]

note("Each distance is a knot point in the table → result must be exact.")
for ft, exp_strokes in PUTT_DATA:
    yd = ft / 3.0
    check(f"Green {ft:2d} ft ({yd:.3f} yd)",
          sg.expected_strokes(yd, "green"), exp_strokes)

# boundary: holed = 0
check("Green 0 yd — holed", sg.expected_strokes(0.0, "green"), 0.0)

# sub-3ft extrapolation: must be between 1.00 and 1.04
v1 = sg.expected_strokes(1/3, "green")  # 1 ft
v2 = sg.expected_strokes(2/3, "green")  # 2 ft
check("Green 1 ft — between 1.00 and 1.04",
      float(1.00 <= v1 <= 1.04), 1.0, tol=0)
check("Green 2 ft — between 1.00 and 1.04",
      float(1.00 <= v2 <= 1.04), 1.0, tol=0)
check("Green 1 ft < 2 ft < 3 ft (monotone)", float(v1 < v2 < 1.04), 1.0, tol=0)

# beyond 90 ft: must continue rising
v90  = sg.expected_strokes(30.0,  "green")   # 90 ft
v100 = sg.expected_strokes(33.33, "green")   # 100 ft
check("Green 100 ft > 90 ft (extrapolation rises)", float(v100 > v90), 1.0, tol=0)


# ══════════════════════════════════════════════════════════════════════════════
# 2. FAIRWAY / ROUGH / BUNKER / RECOVERY BASELINE — spot checks
#    All distances at exact table knot points → results must be exact.
# ══════════════════════════════════════════════════════════════════════════════
_section("2 · Fairway / Rough / Bunker / Recovery baseline — spot checks")

note("Source table 100-300 yd (Tee/Fairway), 20-300 yd (Rough/Sand/Recovery).")
note("Tee and Fairway are now SEPARATE tables — different values above 100 yd.")

# ── Fairway ───────────────────────────────────────────────────────────────────
FW_KNOTS = [
    (100., 2.80), (120., 2.85), (140., 2.91),
    (200., 3.19), (240., 3.45), (300., 3.78),
]
for dist, exp in FW_KNOTS:
    check(f"Fairway {dist:.0f} yd", sg.expected_strokes(dist, "fairway"), exp)

# ── Tee (separate from Fairway above 100 yd) ──────────────────────────────────
TEE_KNOTS = [(100., 2.92), (200., 3.12), (260., 3.45), (300., 3.71)]
for dist, exp in TEE_KNOTS:
    check(f"Tee     {dist:.0f} yd", sg.expected_strokes(dist, "tee"), exp)

check("Tee 300 yd ≠ Fairway 300 yd (separate tables)",
      float(sg.expected_strokes(300, "tee") != sg.expected_strokes(300, "fairway")),
      1.0, tol=0)
check("Tee 100 yd > Fairway 100 yd (tee harder at same dist)",
      float(sg.expected_strokes(100, "tee") > sg.expected_strokes(100, "fairway")),
      1.0, tol=0)

# ── Rough ─────────────────────────────────────────────────────────────────────
ROUGH_KNOTS = [
    (20., 2.40), (40., 2.60), (80., 2.75),
    (100., 3.02), (200., 3.42), (300., 3.90),
]
for dist, exp in ROUGH_KNOTS:
    check(f"Rough   {dist:.0f} yd", sg.expected_strokes(dist, "rough"), exp)

# ── Sand / Bunker ─────────────────────────────────────────────────────────────
BUNKER_KNOTS = [
    (20., 2.59), (40., 2.78), (80., 2.96),
    (100., 3.23), (200., 3.55), (300., 4.04),
]
for dist, exp in BUNKER_KNOTS:
    check(f"Bunker  {dist:.0f} yd", sg.expected_strokes(dist, "bunker"), exp)

note("Source data has a non-monotone dip at 120 yd Sand (3.21 < 100 yd 3.23); reproduced exactly.")
check("Bunker 120 yd (non-monotone) = 3.21 as per source",
      sg.expected_strokes(120, "bunker"), 3.21)
check("Bunker 120 yd < 100 yd (source data non-monotone, not a bug)",
      float(sg.expected_strokes(120, "bunker") < sg.expected_strokes(100, "bunker")),
      1.0, tol=0)

# ── Recovery ─────────────────────────────────────────────────────────────────
RECOVERY_KNOTS = [
    (20., 2.53), (40., 2.82), (80., 3.24),
    (100., 3.80), (200., 3.87), (300., 4.20),
]
for dist, exp in RECOVERY_KNOTS:
    check(f"Recovery {dist:.0f} yd", sg.expected_strokes(dist, "recovery"), exp)

check("Recovery > Rough > Fairway at 200 yd (harder lies = more strokes)",
      float(
          sg.expected_strokes(200, "recovery") >
          sg.expected_strokes(200, "rough")    >
          sg.expected_strokes(200, "fairway")
      ), 1.0, tol=0)

# ── Monotonicity (over broad distance range) ──────────────────────────────────
note("Monotonicity over broad range; Tee/Sand/Recovery have small non-monotone")
note("dips in the source data and are tested for overall increasing trend only.")

# Strictly monotone lies (source data has no dips)
for lie in ("fairway", "rough", "fringe", "green"):
    dists = [10, 25, 50, 75, 100, 150, 200, 300]
    vals  = [sg.expected_strokes(d, lie) for d in dists]
    mono  = all(vals[i] < vals[i+1] for i in range(len(vals)-1))
    check(f"{lie.capitalize():<12} — strictly monotone over 10-300 yd",
          float(mono), 1.0, tol=0)

# Overall trend for lies with source non-monotonicity
for lie in ("tee", "bunker", "recovery"):
    first = sg.expected_strokes(100, lie)
    last  = sg.expected_strokes(300, lie)
    check(f"{lie.capitalize():<12} — overall increasing trend 100-300 yd",
          float(last > first), 1.0, tol=0)

# Holed always = 0 regardless of lie
for lie in ("tee", "fairway", "rough", "recovery", "bunker", "green"):
    check(f"0 yd holed ({lie}) = 0.00", sg.expected_strokes(0.0, lie), 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# 3. LIE NORMALISATION — GSPro text strings
# ══════════════════════════════════════════════════════════════════════════════
_section("3 · normalize_lie — GSPro OCR text mapping")

LIE_CASES = [
    ("Fairway",       sg.LIE_FAIRWAY),
    ("fairway",       sg.LIE_FAIRWAY),
    ("FAIRWAY",       sg.LIE_FAIRWAY),
    ("Rough",         sg.LIE_ROUGH),
    # Recovery / Heavy Rough / trees → all map to LIE_RECOVERY (source table column)
    ("Heavy Rough",   sg.LIE_RECOVERY),
    ("deep rough",    sg.LIE_RECOVERY),
    ("trees",         sg.LIE_RECOVERY),
    ("recovery",      sg.LIE_RECOVERY),
    ("penalty",       sg.LIE_RECOVERY),
    ("Bunker",        sg.LIE_BUNKER),
    ("Sand",          sg.LIE_BUNKER),
    ("trap",          sg.LIE_BUNKER),
    ("Green",         sg.LIE_GREEN),
    ("On Green",      sg.LIE_GREEN),
    ("Fringe",        sg.LIE_FRINGE),
    ("Collar",        sg.LIE_FRINGE),
    ("Tee",           sg.LIE_TEE),
    ("Tee Box",       sg.LIE_TEE),
    ("",              sg.LIE_FAIRWAY),
    ("n/a",           sg.LIE_FAIRWAY),
    ("Unknown Lie",   sg.LIE_FAIRWAY),
]

note("'Heavy Rough', 'deep rough', 'trees', 'recovery', 'penalty' all → LIE_RECOVERY.")
for raw, expected_lie in LIE_CASES:
    check(f"normalize_lie({raw!r:<22})", sg.normalize_lie(raw), expected_lie, tol=0)


# ══════════════════════════════════════════════════════════════════════════════
# 4. SHOT CATEGORY
# ══════════════════════════════════════════════════════════════════════════════
_section("4 · shot_category — Off Tee / Approach / Around Green / Putting")

CAT_CASES = [
    # (shot_num, pre_lie, pre_dist_yd, expected_cat)
    (1,  sg.LIE_TEE,     400., sg.CAT_OTT),    # shot 1 = always OTT
    (1,  sg.LIE_FAIRWAY, 400., sg.CAT_OTT),    # shot 1, any lie = OTT
    (2,  sg.LIE_TEE,     150., sg.CAT_OTT),    # lie=tee overrides shot num
    (2,  sg.LIE_FAIRWAY, 150., sg.CAT_APP),    # 150 yd > ATG threshold
    (2,  sg.LIE_ROUGH,   150., sg.CAT_APP),
    (3,  sg.LIE_FAIRWAY,  31., sg.CAT_APP),    # just above ATG threshold
    (3,  sg.LIE_FAIRWAY,  30., sg.CAT_ATG),    # exactly at ATG threshold
    (3,  sg.LIE_FAIRWAY,  10., sg.CAT_ATG),    # well within ATG
    (3,  sg.LIE_ROUGH,    20., sg.CAT_ATG),
    (3,  sg.LIE_BUNKER,   15., sg.CAT_ATG),
    (4,  sg.LIE_GREEN,    5.0, sg.CAT_PUTT),   # on green = putting
    (2,  sg.LIE_GREEN,    0.5, sg.CAT_PUTT),
]

for shot_num, pre_lie, pre_dist, exp_cat in CAT_CASES:
    label = f"shot={shot_num}  lie={pre_lie:<12}  dist={pre_dist:.0f} yd"
    check(label, sg.shot_category(shot_num, pre_lie, pre_dist), exp_cat, tol=0)


# ══════════════════════════════════════════════════════════════════════════════
# 5. PARSE DISTANCE
# ══════════════════════════════════════════════════════════════════════════════
_section("5 · parse_distance — unit conversion")

PARSE_CASES = [
    ("154",       154.0),   # bare number → yards
    ("154 yd",    154.0),
    ("154 yds",   154.0),
    ("45 ft",      15.0),   # feet → yards
    ("45 feet",    15.0),
    ("30 ft",      10.0),
    ("9 ft",        3.0),
    ("9.6 ft",      3.2),
    ("0",           0.0),
    ("",            0.0),   # blank → 0
    ("N/A",         0.0),
    ("100m",      109.36),  # metres → yards (approx)
]

for text, exp in PARSE_CASES:
    tol = 0.5 if "m" in text.lower() and "yd" not in text else 0.01
    check(f"parse_distance({text!r:<12})", sg.parse_distance(text), exp, tol=tol)


# ══════════════════════════════════════════════════════════════════════════════
# 6. SG SINGLE-SHOT SCENARIOS
# ══════════════════════════════════════════════════════════════════════════════
_section("6 · sg_shot — named golf scenarios")

note("Formula: SG = expected(pre) − 1 − expected(post)")

def scenario(label, pre_dist, pre_lie, post_dist, post_lie, exp_sg, tol=0.01):
    got = sg.sg_shot(pre_dist, pre_lie, post_dist, post_lie)
    check(label, got, exp_sg, tol)
    return got

# ── Putting ───────────────────────────────────────────────────────────────────
note("Putting: SG = expected(pre) − 1 − 0  when holed in one putt")

# Make a 10ft putt: expected(3.333yd, green)=1.61, SG = 1.61 - 1 - 0 = +0.61
scenario("Sink 10ft putt",
         3.333, sg.LIE_GREEN, 0.0, sg.LIE_GREEN, +0.61)

# Make a 20ft birdie putt: expected=1.87, SG = 1.87 - 1 - 0 = +0.87
scenario("Sink 20ft birdie putt",
         6.667, sg.LIE_GREEN, 0.0, sg.LIE_GREEN, +0.87)

# Make a 3ft par putt: expected=1.04, SG = 1.04 - 1 - 0 = +0.04
scenario("Sink 3ft par putt",
         1.000, sg.LIE_GREEN, 0.0, sg.LIE_GREEN, +0.04)

# Lag a 30ft putt to 3ft: expected(10yd)=1.98, expected(1yd)=1.04
# SG = 1.98 - 1 - 1.04 = -0.06  (below baseline — had to take a second putt)
scenario("Lag 30ft → 3ft (two-putt lag)",
         10.00, sg.LIE_GREEN, 1.000, sg.LIE_GREEN, -0.06)

# Three-putt: miss a 5ft putt, leave 2ft
# SG = 1.23 - 1 - 1.02 = -0.79  (heavy penalty for missing a makeable putt)
scenario("Miss 5ft putt, leave 2ft",
         1.667, sg.LIE_GREEN, 0.667, sg.LIE_GREEN, -0.79)

# ── Tee shots ─────────────────────────────────────────────────────────────────
note("Tee shots now use a SEPARATE table from Fairway (Tee column in source data).")

# Good tee shot: 400yd par 4, drive to 125yd fairway
# e(400,tee) extrapolated from (280=3.65,300=3.71): slope=0.003/yd → 4.01
# e(125,fw) between 120(2.85) and 140(2.91) → 2.865
# SG = 4.01 − 1 − 2.865 = +0.145
scenario("Good tee shot: 400→125 yd fairway",
         400., sg.LIE_TEE, 125., sg.LIE_FAIRWAY, +0.145)

# Great tee shot: drive to 75yd
# e(75,fw) = sub-100 estimate = 2.72
# SG = 4.01 − 1 − 2.72 = +0.29
scenario("Great tee shot: 400→75 yd fairway",
         400., sg.LIE_TEE, 75., sg.LIE_FAIRWAY, +0.29)

# OB: same position as start → always −1.00 (one stroke wasted)
scenario("OB: 400 yd re-tee (shot wasted)",
         400., sg.LIE_TEE, 400., sg.LIE_TEE, -1.00)

# ── Approach shots ────────────────────────────────────────────────────────────
note("Approach shots — baseline uses new Fairway/Rough/Bunker source tables.")

# 150yd fairway → 10ft: e(150,fw)=2.945 (interp 140-160), e(10ft=3.333yd,g)=1.61
# SG = 2.945 − 1 − 1.61 = +0.335
scenario("Flush 150 yd FW → 10ft on green",
         150., sg.LIE_FAIRWAY, 10/3, sg.LIE_GREEN, +0.335)

# 150yd fairway → 20ft: SG = 2.945 − 1 − 1.87 = +0.075
scenario("Decent 150 yd FW → 20ft on green",
         150., sg.LIE_FAIRWAY, 20/3, sg.LIE_GREEN, +0.075)

# Chunk from rough: 150→125yd (only 25 yd gained)
# e(150,rough)=3.19, e(125,fw)=2.865 → SG = 3.19 − 1 − 2.865 = −0.675
scenario("Chunk 150 yd rough → 125 yd FW  (only 25 yd gained)",
         150., sg.LIE_ROUGH, 125., sg.LIE_FAIRWAY, -0.675)

# FW bunker 150yd → greenside bunker 10yd:
# e(150,bunker)=3.25 (interp 140-160), e(10,bunker)=2.10 (sub-20 estimate)
# SG = 3.25 − 1 − 2.10 = +0.15  (advanced 140yd, escaped the bunker zone)
scenario("FW bunker 150 yd → green-side bunker 10 yd",
         150., sg.LIE_BUNKER, 10., sg.LIE_BUNKER, +0.15)

# ── Around green ──────────────────────────────────────────────────────────────
note("Around the green shots")

# Chip from 20yd fringe to 4ft: fringe table unchanged
# e(20,fringe)=2.48 − 1 − e(4ft=1.333yd,g)=1.13 = +0.35
scenario("Chip 20 yd fringe → 4ft on green",
         20., sg.LIE_FRINGE, 4/3, sg.LIE_GREEN, +0.35)

# Greenside bunker 10yd → 5ft (1.667yd):
# e(10,bunker)=2.10 (sub-20 estimate), e(5ft=1.667yd,g)=1.23
# SG = 2.10 − 1 − 1.23 = −0.13
# Slightly negative: baseline expects getting within ~4ft from a 30ft bunker
scenario("Bunker 10 yd → 5ft on green",
         10., sg.LIE_BUNKER, 5/3, sg.LIE_GREEN, -0.13)

# Poor rough shot: 150→125yd
scenario("Poor rough shot: 150→125 yd (only 25 yd gained)",
         150., sg.LIE_ROUGH, 125., sg.LIE_FAIRWAY, -0.675)

# Good rough shot: 150yd rough → 15ft (5yd on green)
# SG = 3.19 − 1 − 1.78 = +0.41
scenario("Good rough shot: 150 yd rough → 15ft on green",
         150., sg.LIE_ROUGH, 5.0, sg.LIE_GREEN, +0.41)


# ══════════════════════════════════════════════════════════════════════════════
# 7. FULL-HOLE IDENTITY
#    Sum of SG for all shots on a hole = expected_strokes(tee, dist) − score
#    This is the fundamental SG accounting identity.
# ══════════════════════════════════════════════════════════════════════════════
_section("7 · Full-hole identity: Σ SG = expected(tee) − actual score")

note("Identity: no matter how a hole is played, the sum of per-shot SG")
note("must equal baseline_from_tee − actual_strokes_taken.")

def simulate_hole(label, tee_yardage, shots, expected_score_diff):
    """
    shots: list of (pre_dist, pre_lie, post_dist, post_lie)
    Returns total SG and checks against the identity.
    """
    baseline = sg.expected_strokes(tee_yardage, sg.LIE_TEE)
    total_sg  = sum(sg.sg_shot(pd, pl, od, ol) for pd, pl, od, ol in shots)
    score     = len(shots)
    identity  = round(baseline - score, 3)
    check(f"{label} — Σ SG = {total_sg:+.3f}, identity = {identity:+.3f}",
          total_sg, identity, tol=0.005)

# ── Par 4, 400 yd — birdie (3 shots) ──────────────────────────────────────────
# Drive 400→125 yd FW, approach 125→10ft green, sink putt
simulate_hole("Par 4 (400 yd) — 3-shot birdie", 400., [
    (400., sg.LIE_TEE,     125.,    sg.LIE_FAIRWAY),  # drive
    (125., sg.LIE_FAIRWAY, 10/3,    sg.LIE_GREEN),    # approach → 10ft
    (10/3, sg.LIE_GREEN,   0.,      sg.LIE_GREEN),    # make putt
], expected_score_diff=-1)  # birdie

# ── Par 4, 400 yd — bogey (5 shots) ───────────────────────────────────────────
# Drive to rough 150yd, lay up to 75yd FW, approach to 20ft, two-putt
simulate_hole("Par 4 (400 yd) — 5-shot bogey", 400., [
    (400., sg.LIE_TEE,     150., sg.LIE_ROUGH),     # drive → rough
    (150., sg.LIE_ROUGH,   75.,  sg.LIE_FAIRWAY),   # punch out
    (75.,  sg.LIE_FAIRWAY, 20/3, sg.LIE_GREEN),     # approach → 20ft
    (20/3, sg.LIE_GREEN,   1.0,  sg.LIE_GREEN),     # lag → 3ft
    (1.0,  sg.LIE_GREEN,   0.,   sg.LIE_GREEN),     # make 3ft
], expected_score_diff=+1)  # bogey

# ── Par 3, 150 yd — par (2 shots) ─────────────────────────────────────────────
# Tee shot to 15ft, make birdie putt
simulate_hole("Par 3 (150 yd) — 2-shot birdie", 150., [
    (150., sg.LIE_TEE,   5.0, sg.LIE_GREEN),   # tee shot → 15ft
    (5.0,  sg.LIE_GREEN, 0.,  sg.LIE_GREEN),   # make 15ft birdie putt
], expected_score_diff=-1)

# ── Par 5, 550 yd — eagle (3 shots) ───────────────────────────────────────────
simulate_hole("Par 5 (550 yd) — 3-shot eagle", 550., [
    (550., sg.LIE_TEE,     250., sg.LIE_FAIRWAY),  # drive 300yd
    (250., sg.LIE_FAIRWAY, 10/3, sg.LIE_GREEN),    # 2nd shot → 10ft
    (10/3, sg.LIE_GREEN,   0.,   sg.LIE_GREEN),    # make eagle putt
], expected_score_diff=-2)


# ══════════════════════════════════════════════════════════════════════════════
# 8. GIMMIE / RECONCILIATION MATHS
# ══════════════════════════════════════════════════════════════════════════════
_section("8 · Gimmie / auto-holed reconciliation")

note("Gimmie SG = expected(pre_dist, green) − 1 − 0")
note("Must be positive (ball is inside expected stroke count from that distance).")

for ft, exp_strokes in [(3, 1.04), (5, 1.23), (7, 1.42), (9, 1.56)]:
    yd = ft / 3.0
    sg_val = sg.sg_shot(yd, sg.LIE_GREEN, 0., sg.LIE_GREEN)
    exp    = round(exp_strokes - 1.0, 3)
    check(f"Gimmie from {ft} ft — SG = {exp:+.3f}", sg_val, exp)
    check(f"Gimmie from {ft} ft — SG is positive",
          float(sg_val > 0), 1.0, tol=0)

# A gimmie from beyond typical range should still be computable (no crash)
sg_long = sg.sg_shot(10.0, sg.LIE_GREEN, 0., sg.LIE_GREEN)
check("Gimmie from 30ft (10 yd) — no crash, SG > 0",
      float(sg_long > 0), 1.0, tol=0)


# ══════════════════════════════════════════════════════════════════════════════
# 9. INTERPOLATION — values strictly BETWEEN table knots
#    Linear interpolation formula: result = y0 + (y1-y0)/(x1-x0) * (x-x0)
#    Tests confirm the interpolator hits the correct value at arbitrary
#    mid-knot distances, not just at the exact data points.
# ══════════════════════════════════════════════════════════════════════════════
_section("9 · Interpolation between table knots")

note("Each value below falls strictly between two adjacent knot points.")
note("Expected values computed analytically from the linear formula.")

# ── Green ─────────────────────────────────────────────────────────────────────
# Between  9 ft (3.000 yd, 1.56) and 10 ft (3.333 yd, 1.61)
# At 9.5 ft = 3.167 yd:
#   t = (3.167 - 3.000) / (3.333 - 3.000) = 0.502
#   result = 1.56 + 0.502 * 0.05 ≈ 1.585
_d = 9.5 / 3.0
_e = 1.56 + ((1.61 - 1.56) / (10/3 - 3.0)) * (_d - 3.0)
check(f"Green  9.5 ft ({_d:.4f} yd) — midpoint 9–10 ft",
      sg.expected_strokes(_d, "green"), round(_e, 4))

# Between 15 ft (5.000 yd, 1.78) and 20 ft (6.667 yd, 1.87)
# At 17.5 ft = 5.833 yd:
#   t ≈ 0.500  →  result = 1.78 + 0.5 * 0.09 = 1.825
_d = 17.5 / 3.0
_e = 1.78 + ((1.87 - 1.78) / (20/3 - 5.0)) * (_d - 5.0)
check(f"Green 17.5 ft ({_d:.4f} yd) — midpoint 15–20 ft",
      sg.expected_strokes(_d, "green"), round(_e, 4))

# Between 30 ft (10.00 yd, 1.98) and 40 ft (13.33 yd, 2.06)
# At 35 ft = 11.667 yd:
#   t ≈ 0.500  →  result = 1.98 + 0.5 * 0.08 = 2.020
_d = 35.0 / 3.0
_e = 1.98 + ((2.06 - 1.98) / (40/3 - 10.0)) * (_d - 10.0)
check(f"Green 35 ft ({_d:.4f} yd) — midpoint 30–40 ft",
      sg.expected_strokes(_d, "green"), round(_e, 4))

# Between 60 ft (20.00 yd, 2.21) and 90 ft (30.00 yd, 2.40)
# At 75 ft = 25.00 yd:
#   t = 0.500  →  result = 2.21 + 0.5 * 0.19 = 2.305
_d = 75.0 / 3.0
_e = 2.21 + ((2.40 - 2.21) / (30.0 - 20.0)) * (_d - 20.0)
check(f"Green 75 ft ({_d:.4f} yd) — midpoint 60–90 ft",
      sg.expected_strokes(_d, "green"), round(_e, 4))

# Non-midpoint: at 7.2 ft = 2.400 yd (between 7ft=2.333yd/1.42 and 8ft=2.667yd/1.50)
#   t = (2.400 - 2.333) / (2.667 - 2.333) = 0.067/0.334 = 0.2006
#   result = 1.42 + 0.2006 * 0.08 ≈ 1.436
_d = 7.2 / 3.0
_e = 1.42 + ((1.50 - 1.42) / (8/3 - 7/3)) * (_d - 7/3)
check(f"Green  7.2 ft ({_d:.4f} yd) — non-midpoint 7–8 ft",
      sg.expected_strokes(_d, "green"), round(_e, 4))

# ── Fairway ── (source table: 100-300 yd in 20 yd steps) ─────────────────────
# Between 140 yd (2.91) and 160 yd (2.98) → midpoint 150 yd = 2.9450
_d, _e = 150.0, 2.91 + ((2.98 - 2.91) / (160 - 140)) * (150 - 140)
check("Fairway 150 yd — midpoint 140–160 yd",
      sg.expected_strokes(_d, "fairway"), round(_e, 4))

# Between 100 yd (2.80) and 120 yd (2.85) → midpoint 110 yd = 2.8250
_d, _e = 110.0, 2.80 + ((2.85 - 2.80) / (120 - 100)) * (110 - 100)
check("Fairway 110 yd — midpoint 100–120 yd",
      sg.expected_strokes(_d, "fairway"), round(_e, 4))

# Between 200 yd (3.19) and 240 yd (3.45) → midpoint 220 yd = 3.3200
_d, _e = 220.0, 3.19 + ((3.45 - 3.19) / (240 - 200)) * (220 - 200)
check("Fairway 220 yd — midpoint 200–240 yd",
      sg.expected_strokes(_d, "fairway"), round(_e, 4))

# Between 120 yd (2.85) and 140 yd (2.91) → midpoint 130 yd = 2.8800
_d, _e = 130.0, 2.85 + ((2.91 - 2.85) / (140 - 120)) * (130 - 120)
check("Fairway 130 yd — midpoint 120–140 yd",
      sg.expected_strokes(_d, "fairway"), round(_e, 4))

# ── Rough ── (source table: 20-300 yd in 20 yd steps) ────────────────────────
# Between 40 yd (2.60) and 60 yd (2.70) → midpoint 50 yd = 2.6500
_d, _e = 50.0, 2.60 + ((2.70 - 2.60) / (60 - 40)) * (50 - 40)
check("Rough  50 yd — midpoint 40–60 yd",
      sg.expected_strokes(_d, "rough"), round(_e, 4))

# Between 160 yd (3.23) and 180 yd (3.31) → midpoint 170 yd = 3.2700
_d, _e = 170.0, 3.23 + ((3.31 - 3.23) / (180 - 160)) * (170 - 160)
check("Rough 170 yd — midpoint 160–180 yd",
      sg.expected_strokes(_d, "rough"), round(_e, 4))

# ── Bunker ── (source table: 20-300 yd; sub-20 estimated) ────────────────────
# Between 15 yd (2.35) and 20 yd (2.59) → midpoint 17.5 yd = 2.4700
_d, _e = 17.5, 2.35 + ((2.59 - 2.35) / (20 - 15)) * (17.5 - 15)
check("Bunker 17.5 yd — midpoint 15–20 yd (sub-table estimates)",
      sg.expected_strokes(_d, "bunker"), round(_e, 4))

# ── Fringe ────────────────────────────────────────────────────────────────────
# Between 5 yd (1.78) and 7 yd (1.95)  →  midpoint 6 yd = 1.865
_d, _e = 6.0, 1.78 + ((1.95 - 1.78) / (7 - 5)) * (6 - 5)
check("Fringe 6 yd — midpoint 5–7 yd",
      sg.expected_strokes(_d, "fringe"), round(_e, 4))

# ── Extrapolation beyond table maximum (300 yd) ───────────────────────────────
note("Table ends at 300 yd; linear extrapolation uses slope of the last segment.")
# Fairway last two knots: 280 yd (3.69) and 300 yd (3.78)
# slope = (3.78-3.69)/20 = 0.0045 per yd → 400 yd = 3.78 + 0.0045*100 = 4.23
_slope_fw = (3.78 - 3.69) / (300 - 280)
_d = 400.0
_e = 3.78 + _slope_fw * (_d - 300)
check("Fairway 400 yd — linear extrapolation beyond 300 yd knot",
      sg.expected_strokes(_d, "fairway"), round(_e, 4))

# Green extrapolation (unchanged): last knots 20 yd (2.21) and 30 yd (2.40)
_slope = (2.40 - 2.21) / (30.0 - 20.0)
_d = 35.0
_e = 2.40 + _slope * (_d - 30.0)
check("Green 35 yd (beyond 30 yd knot) — linear extrapolation",
      sg.expected_strokes(_d, "green"), round(_e, 4))


note("Continuity: interpolated values lie strictly between their bracket knots.")
_interp_pairs = [
    ("green",   9.5/3,   1.56, 1.61),   # 9–10 ft
    ("green",   17.5/3,  1.78, 1.87),   # 15–20 ft
    ("fairway", 150.0,   2.91, 2.98),   # 140–160 yd  ← new table brackets
    ("fairway", 220.0,   3.19, 3.45),   # 200–240 yd
    ("bunker",  17.5,    2.35, 2.59),   # 15–20 yd    ← new table brackets
]
for lie, dist, lo_val, hi_val in _interp_pairs:
    got = sg.expected_strokes(dist, lie)
    check(f"{lie.capitalize():<9} {dist:6.2f} yd — result between bracket knots",
          float(lo_val < got < hi_val), 1.0, tol=0)


# ══════════════════════════════════════════════════════════════════════════════
# 10. PLAYER vs PGA TOUR — explain_sg and shot_analysis
# ══════════════════════════════════════════════════════════════════════════════
_section("10 · Player vs PGA Tour — explain_sg / shot_analysis")

note("explain_sg: positive SG = player better than PGA Tour average.")
note("shot_analysis: pre_expected − 1 − post_expected = sg (accounting identity).")

# ── explain_sg labels ─────────────────────────────────────────────────────────
pos_txt = sg.explain_sg(+0.61)
check("explain_sg(+0.61) contains 'BETTER'",   float("BETTER" in pos_txt), 1.0, tol=0)
check("explain_sg(+0.61) contains '+0.6100'",  float("+0.6100" in pos_txt), 1.0, tol=0)

neg_txt = sg.explain_sg(-0.72)
check("explain_sg(-0.72) contains 'WORSE'",    float("WORSE" in neg_txt), 1.0, tol=0)
check("explain_sg(-0.72) contains '-0.7200'",  float("-0.7200" in neg_txt), 1.0, tol=0)

cat_txt = sg.explain_sg(-0.25, "Approach")
check("explain_sg with category contains '[Approach]'",
      float("[Approach]" in cat_txt), 1.0, tol=0)

zero_txt = sg.explain_sg(0.0)
check("explain_sg(0.0) contains 'BETTER' (≥0 branch)",
      float("BETTER" in zero_txt), 1.0, tol=0)

# ── shot_analysis accounting identity ────────────────────────────────────────
note("Identity: pre_expected - 1 - post_expected == sg  (must hold for every shot).")

ANALYSIS_CASES = [
    # (pre_dist, pre_lie, post_dist, post_lie, label)
    (400.,  sg.LIE_TEE,     125.,    sg.LIE_FAIRWAY, "Drive 400→125 yd FW"),
    (150.,  sg.LIE_FAIRWAY, 10/3,    sg.LIE_GREEN,   "Approach 150yd→10ft"),
    (10/3,  sg.LIE_GREEN,   0.,      sg.LIE_GREEN,   "Make 10ft putt"),
    (10.0,  sg.LIE_GREEN,   1.0,     sg.LIE_GREEN,   "Lag 30ft → 3ft"),
    (20.,   sg.LIE_FRINGE,  4/3,     sg.LIE_GREEN,   "Chip 20yd fringe→4ft"),
    (10.,   sg.LIE_BUNKER,  5/3,     sg.LIE_GREEN,   "Blast bunker 10yd→5ft"),
]

for pre_d, pre_l, post_d, post_l, label in ANALYSIS_CASES:
    a = sg.shot_analysis(pre_d, pre_l, post_d, post_l)

    # Accounting identity
    identity = round(a["pre_expected"] - a["player_strokes"] - a["post_expected"], 3)
    check(f"{label:<35} — accounting identity holds",
          a["sg"], identity)

    # player_strokes always = 1
    check(f"{label:<35} — player_strokes == 1",
          a["player_strokes"], 1, tol=0)

    # pre_expected > post_expected for any shot that gains ground
    if post_d < pre_d:
        check(f"{label:<35} — pre_expected > post_expected (gained ground)",
              float(a["pre_expected"] > a["post_expected"]), 1.0, tol=0)

    # interpretation string exists and is non-empty
    check(f"{label:<35} — interpretation is non-empty string",
          float(bool(a["interpretation"])), 1.0, tol=0)

# ── SG magnitude sanity ───────────────────────────────────────────────────────
note("SG magnitude: realistic pre/post pairs must lie within physically plausible bounds.")
note("Impossible combinations (e.g. 300 yd putt on the green) are excluded.")

# Realistic pre-shot positions
PRE_REALISTIC = [
    (500., sg.LIE_TEE),     # long par 5 tee shot
    (300., sg.LIE_TEE),     # mid-length tee shot
    (150., sg.LIE_FAIRWAY),
    (100., sg.LIE_ROUGH),
    (20.,  sg.LIE_BUNKER),
    (15.,  sg.LIE_FRINGE),
    (10.,  sg.LIE_GREEN),   # 30 ft putt
    (3.,   sg.LIE_GREEN),   # 9 ft putt
    (1.,   sg.LIE_GREEN),   # 3 ft tap-in
]
# Realistic post-shot positions (ball can't end up further than it started)
POST_REALISTIC = [
    (0.,   sg.LIE_GREEN),   # holed
    (1.,   sg.LIE_GREEN),   # tap-in range
    (5.,   sg.LIE_GREEN),   # 15 ft remaining
    (10.,  sg.LIE_FAIRWAY),
    (50.,  sg.LIE_FAIRWAY),
    (100., sg.LIE_ROUGH),
]

realistic_sg = [
    sg.sg_shot(pd, pl, od, ol)
    for pd, pl in PRE_REALISTIC
    for od, ol in POST_REALISTIC
    if od <= pd + 1      # post distance can't wildly exceed pre distance
]
all_finite = all(math.isfinite(v) for v in realistic_sg)
all_bounded = all(-5.0 <= v <= 5.0 for v in realistic_sg)
check("All realistic pre/post pairs — SG is finite (no NaN / Inf)",
      float(all_finite), 1.0, tol=0)
check("All realistic pre/post pairs — SG within ±5.0",
      float(all_bounded), 1.0, tol=0)

# Separately verify the model doesn't crash on any input (including odd ones)
note("Crash-proofing: expected_strokes must return a finite number for any input.")
crash_inputs = [
    (0.0, "green"), (0.0, "fairway"), (0.001, "green"),
    (650.0, "tee"), (700.0, "fairway"), (1000.0, "rough"),
]
all_no_crash = all(math.isfinite(sg.expected_strokes(d, l)) for d, l in crash_inputs)
check("expected_strokes — finite for all edge-case inputs (0, 650+, 1000 yd)",
      float(all_no_crash), 1.0, tol=0)


total = _passes + _fails
print(f"\n{'═'*60}")
if _fails == 0:
    print(f"{GREEN}{BOLD}  ALL {total} TESTS PASSED{RESET}")
else:
    print(f"{RED}{BOLD}  {_fails} FAILED / {_passes} PASSED  ({total} total){RESET}")
print(f"{'═'*60}\n")

sys.exit(0 if _fails == 0 else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strokes Gained test suite")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print every test case, not just failures")
    args = parser.parse_args()
    _verbose = args.verbose
