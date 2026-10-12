#!/usr/bin/env python3
"""
scoring.py — The round and session engine, shared by the daemon and the
rebuild tool.

Everything that DECIDES what a capture means lives here: whether it shows any
progression, whether it starts a new session, which shot it was, its strokes
gained, when a hole is finished, gimmie handling, and discarding an unfinished
hole. Because the live daemon and rebuild_from_log.py both call
handle_capture(), a log replayed later is scored exactly as it was live — they
cannot drift apart again.

The engine does no file I/O and does not read the system clock directly:

  * Writes go through a SINK supplied by the caller (see Sink below). The
    daemon's sink writes the ODS report and the history file; MemorySink
    collects everything in memory for the rebuild tool and for tests.
  * Time comes from now(). It is the real clock unless set_clock() installs
    another one — a replay installs each capture's original timestamp, so
    session gaps and row timestamps are reproduced exactly.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from typing import Any, Callable, Protocol

import parsing
import strokes_gained as sg_mod

log = logging.getLogger(__name__)


# ── Clock ──────────────────────────────────────────────────────────────────────

_clock: Callable[[], datetime] | None = None


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Install a clock (a replay passes each capture's timestamp); None = real time."""
    global _clock
    _clock = fn


def now() -> datetime:
    return _clock() if _clock is not None else datetime.now()


# ── Sink ───────────────────────────────────────────────────────────────────────

class Sink(Protocol):
    """Where the engine's output goes. The engine calls only these four."""

    def write_row(self, sheet: str, row: int, values: list) -> bool: ...
    def truncate(self, sheet: str, keep_rows: int) -> int: ...
    def summary_changed(self, round_state: dict) -> None: ...
    def archive_session(self, round_state: dict) -> list: ...


def _zero_cats() -> dict[str, float]:
    return {c: 0.0 for c in sg_mod.SG_CATS}

DEFAULT_ROUND: dict[str, Any] = {
    "hole_number":    0,          # last hole number seen (0 = none yet)
    "hole_par":       4,
    "hole_yardage":   0.0,
    "hole_started":   False,      # True after first (setup) capture of this hole
    "shots_completed": 0,         # shots completed THIS hole
    "pre_distance":   0.0,        # distance before next shot
    "pre_lie":        sg_mod.LIE_TEE,
    "sg_this_hole":   None,
    "sg_round":       None,
    "score_vs_par":   0,
    "holes_completed": 0,
    "shots_total":    0,
    "pro_baseline_round": 0.0,
    "completed_shots":    0,
    "completed_par":      0,
    "round_score_at_hole_start": None,
    "round_score_at_hole_start_unsigned": None,
    "score_carry": None,
    "round_pars": {},                 # hole -> par for this GSPro round (T-28)
    "end_of_round_strokes": None,     # GSPro's end-of-round total, once shown
    "round_log": {},                  # hole -> recorded/lost, for the round check (T-35)
    "round_reported": False,
    "score_bad_streak": 0,
    "sg_shots_row":   1,
    # pending_shots: one record per shot played on the CURRENT, unfinalised
    # hole.  Cleared by _finalize_hole once the hole is committed.  If the
    # daemon exits with entries still here, that hole never finished and its
    # contribution is rolled back out of the round totals — see
    # _discard_incomplete_hole.
    "pending_shots":  [],
    # Hole lifecycle. hole_over records HOW the current hole ended — "holed" or
    # "gimmie" (ball inside the conceded radius) — or None while it is in play.
    # hole_finalised is set once the hole has been counted. A hole is finalised
    # exactly once: when the next hole starts, or when the session or run ends.
    "hole_over":      None,
    # The club GSPro showed at the previous capture — the club for the NEXT shot.
    # After a shot settles, the HUD has already switched to the following
    # shot's club, so the club read at a capture belongs to the shot after it.
    "pre_club":       "",
    # Capture quality for this session — see _new_quality(). None here so every
    # round gets its own fresh dict.
    "quality":        None,
    "hole_finalised": False,
    # band_stats: {category: {band_label: {n, sg, holed, prox_sum, prox_n}}}
    # Bands come from strokes_gained.distance_band(), which derives them from
    # the baseline table knots — so they always match the input tables.
    "band_stats":     {},
    # geom_stats: {club: {n, short, long, pinhigh, delta_sum, abs_delta_sum,
    #                     lateral_sum}} — short/long tendency per club.
    "geom_stats":     {},
    # ── Session identity ────────────────────────────────────────────────────────
    # A session is ONE sitting on ONE course. It is NOT the daemon's lifetime:
    # round state survives restarts, so without explicit boundaries a week of
    # play on several courses merges into a single meaningless "round".
    "session_course":   "",     # course this session belongs to
    "session_started":  "",     # "YYYY-MM-DD HH:MM:SS"
    "last_capture_ts":  "",     # for idle-gap detection

    "sg_holes_row":   1,
    # ── Per-round statistics (written to "Round Stats" sheet on daemon exit) ────
    "shot_counts_by_cat": None,    # {cat: count} — initialised on first shot
    "shot_counts_by_lie": {},      # {lie: count}
    "sg_by_lie":          {},      # {lie: total_sg}
    "gimmies_count":      0,       # total reconciled shots
    "course_name":        "",      # last seen course name
    "round_start_time":   "",      # ISO timestamp of first hole
    "hole_history":       [],      # list of per-hole summary dicts
}

def _new_quality() -> dict:
    """
    Per-session capture quality.

    captures / accepted / invalid / no_progression
        what happened to each capture (invalid = menu or loading screen;
        no_progression = replay, camera move, result screen captured again)
    strokes_captured / strokes_conceded / strokes_uncaptured
        how each counted stroke is known: from a capture; a gimmie (certain);
        or inferred from the round score with an ESTIMATED strokes-gained value
    fields
        per-region read health on accepted captures: {cell: {name, n, blank, bad}}
    """
    return {"captures": 0, "accepted": 0, "invalid": 0, "no_progression": 0,
            "strokes_captured": 0, "strokes_conceded": 0, "strokes_uncaptured": 0,
            "strokes_recovered": 0, "end_screens": 0,
            "fields": {}}


# What each region must yield on a genuine golf screen. Launch-monitor fields
# are skipped on putts, where GSPro legitimately leaves them blank.
_LAUNCH_CELLS = {"L1", "M1", "N1", "O1", "P1", "Q1", "R1", "S1"}


def _field_ok(cell: str, raw: str, p: dict) -> bool:
    if cell == "A1": return bool(p.get("course"))
    if cell == "B1": return p.get("hole", 0) > 0
    if cell == "C1": return 3 <= p.get("par", 0) <= 5
    if cell == "D1": return p.get("hole_yardage", 0) > 0
    if cell in ("G1", "J1"):
        v = parsing.vocab
        return bool(v.snap_lie(raw)[1]) if v else bool(raw.strip())
    if cell == "I1":
        v = parsing.vocab
        return p.get("club") in v.CLUBS if v else bool(raw.strip())
    if cell == "K1":
        return sg_mod.parse_distance(raw) > 0 or bool(
            re.fullmatch(r"\s*0+(?:\.0+)?\s*(?:yds?|ft)?\s*", raw))
    if cell == "U1": return p.get("round_score") is not None
    if cell == "V1": return p.get("hole_score_ticker", 0) > 0
    if cell == "E1": return bool(re.search(r"\d", raw))
    if cell in ("N1", "O1", "P1"): return sg_mod.parse_distance(raw) > 0
    if cell in _LAUNCH_CELLS: return sg_mod.parse_number(raw) is not None
    return True


_PLACEHOLDER = re.compile(r"^[\s\-—–=_]{2,}$")


_FIELD_NAMES = {"E1": "wind speed", "A1": "course", "B1": "hole", "C1": "par", "D1": "hole yardage",
                "G1": "lie", "J1": "lie (2nd)", "I1": "club", "K1": "remaining",
                "L1": "VLA", "M1": "HLA", "N1": "carry", "O1": "carry raw",
                "P1": "total length", "Q1": "ball speed", "R1": "spin axis",
                "S1": "total spin", "U1": "round score", "V1": "stroke ticker"}


def _record_field_health(q: dict, parsed: dict, by_cell: dict[str, str]) -> None:
    putt = (parsed.get("club") or "").lower() == "putter"
    for cell, name in _FIELD_NAMES.items():
        if cell not in by_cell or (putt and cell in _LAUNCH_CELLS):
            continue
        raw = by_cell.get(cell, "") or ""
        e = q["fields"].setdefault(cell, {"name": name, "n": 0, "blank": 0, "bad": 0})
        e.setdefault("nodata", 0)
        e["n"] += 1
        if _PLACEHOLDER.match(raw):
            # GSPro shows "---" when it has no shot data (e.g. at a hole's
            # first tee shot). That is a correct read, not an OCR failure.
            e["nodata"] += 1
        elif not raw.strip():
            e["blank"] += 1
        elif not _field_ok(cell, raw, parsed):
            e["bad"] += 1


def _quality(state: dict) -> dict:
    r = state["round"]
    q = r.get("quality") or _new_quality()
    for k, v in _new_quality().items():
        q.setdefault(k, v)
    r["quality"] = q
    return q


def _fix_round(r: dict) -> dict:
    """
    Ensure all round keys exist (migration safety).

    DEFAULT_ROUND is DEEP-copied. A shallow copy left its dicts and lists —
    band_stats, geom_stats, hole_history, pending_shots, shot_counts_by_lie,
    sg_by_lie — as ONE shared object behind every fresh round, so recording a
    shot in one round wrote into the defaults and leaked into every later
    session started in the same run. Found in v2.9.0 when the rebuild tool,
    which never round-trips state through JSON, diverged from the live daemon.
    """
    merged = copy.deepcopy(DEFAULT_ROUND)
    merged.update(r)
    if merged["sg_this_hole"] is None:
        merged["sg_this_hole"] = _zero_cats()
    if merged["sg_round"] is None:
        merged["sg_round"] = _zero_cats()
    # States saved before v3.4 have no Unattributed key; add every category.
    for key in ("sg_round", "sg_this_hole"):
        if isinstance(merged.get(key), dict):
            for c in sg_mod.SG_CATS:
                merged[key].setdefault(c, 0.0)
    if isinstance(merged.get("shot_counts_by_cat"), dict):
        for c in sg_mod.SG_CATS:
            merged["shot_counts_by_cat"].setdefault(c, 0)
    q = merged.get("quality") or _new_quality()
    for k, v in _new_quality().items():
        q.setdefault(k, v)
    merged["quality"] = q
    if merged["shot_counts_by_cat"] is None:
        merged["shot_counts_by_cat"] = {c: 0 for c in sg_mod.SG_CATS}
    # Ensure list/dict fields are never None
    for key, default in [("shot_counts_by_lie", {}), ("sg_by_lie", {}),
                          ("hole_history", []), ("pending_shots", []),
                          ("band_stats", {}), ("geom_stats", {})]:
        if merged.get(key) is None:
            merged[key] = default
    return merged

DATE_FMT = "%Y-%m-%d"

TIME_FMT = "%H:%M:%S"

def _now_parts() -> tuple[str, str]:
    """Return (date, time) strings for the current moment."""
    n = now()
    return n.strftime(DATE_FMT), n.strftime(TIME_FMT)

def _iso_week_label(dt: datetime) -> str:
    """
    ISO-8601 week label, e.g. '2026-W36'.

    ISO weeks start on Monday and the ISO year can differ from the calendar
    year in late December / early January, so isocalendar() is used rather
    than strftime('%Y-%W') which would split those weeks incorrectly.
    """
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

def build_session_entry(r: dict) -> dict | None:
    """
    Append the finished round to round_history.json and return the full archive.

    A round is identified by its start timestamp.  Re-archiving the same round
    (e.g. daemon restarted and exited again without a new round) REPLACES the
    previous entry rather than duplicating it, so the history stays clean.
    """
    holes = r.get("holes_completed", 0)
    if holes == 0:
        return None

    started = r.get("round_start_time") or ""
    try:
        start_dt = datetime.strptime(started, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        start_dt = now()
        started  = start_dt.strftime("%Y-%m-%d %H:%M")

    ended    = now()
    duration = max(0, int((ended - start_dt).total_seconds() // 60))
    sg_r     = r.get("sg_round") or {}
    shots    = r.get("completed_shots", 0)
    baseline = r.get("pro_baseline_round", 0.0)

    entry = {
        # Session identity: start time AND course, so two sittings on different
        # courses can never collapse into one archive entry.
        "round_id":     f"{started} | {r.get('session_course') or r.get('course_name') or '?'}",
        "started_at":   started,
        "date":         start_dt.strftime(DATE_FMT),
        "start_time":   start_dt.strftime(TIME_FMT),
        "end_time":     ended.strftime(TIME_FMT),
        "duration_min": duration,
        "week":         _iso_week_label(start_dt),
        "course":       r.get("session_course") or r.get("course_name", ""),
        "holes":        holes,
        "shots":        shots,
        "par":          r.get("completed_par", 0),
        "vs_par":       r.get("score_vs_par",  0),
        "pro_baseline": round(baseline, 2),
        "vs_pro":       round(shots - baseline, 2),
        "gimmies":      r.get("gimmies_count", 0),
        "sg":           {c: round(sg_r.get(c, 0.0), 3) for c in sg_mod.SG_CATS},
        "sg_total":     round(sum(sg_r.values()), 3),
        # Rates stored alongside totals so any later comparison between
        # sessions of different length is normalised by construction.
        "holes_equiv":       round(holes / 18.0, 3),
        "vs_par_per_hole":   round(r.get("score_vs_par", 0) / holes, 4),
        "vs_pro_per_hole":   round((shots - baseline) / holes, 4),
        "sg_total_per_hole": round(sum(sg_r.values()) / holes, 4),
        "shot_counts":  dict(r.get("shot_counts_by_cat") or {}),
        "band_stats":   copy.deepcopy(r.get("band_stats") or {}),
        "geom_stats":   copy.deepcopy(r.get("geom_stats") or {}),
        "quality":      copy.deepcopy(r.get("quality") or {}),
    }

    return entry


def merge_session(history: list[dict], entry: dict) -> list[dict]:
    """
    Add *entry* to *history*. A session is identified by start time and course,
    so re-archiving the same session (daemon restarted, exited again) REPLACES
    the earlier entry instead of duplicating it.
    """
    rounds = [h for h in history if h.get("round_id") != entry["round_id"]]
    rounds.append(entry)
    rounds.sort(key=lambda h: h.get("round_id", ""))
    return rounds

_SG_SHOTS_HEADERS = [
    # ── When the shot was captured ──────────────────────────────────────────────
    "Date", "Time",
    # ── SG calculation context ──────────────────────────────────────────────────
    "Hole", "Par", "Shot#", "SG Category",
    # The club that hit this shot (the club GSPro showed BEFORE it). Region9,
    # further right, keeps the raw club read AFTER the shot — the next club.
    "Club",
    # Distances are shown in FEET when the ball is on the green and in yards
    # otherwise. The unit sits in its own column so the value column stays
    # numeric for sorting and filtering.
    "Pre-Dist", "Pre Unit", "Pre-Lie",
    "Post-Dist", "Post Unit", "Post-Lie",
    "SG", "SG Cumul.",
    # ── Player vs PGA Tour ──────────────────────────────────────────────────────
    # "Pro Baseline" = expected_strokes(pre_dist, pre_lie) — what a PGA Tour
    # average would need from this exact position and lie.
    # "Player vs PGA Tour" = human-readable interpretation of the SG value.
    "Pro Baseline (exp.strokes)", "Player vs PGA Tour",
    # ── Launch monitor data ─────────────────────────────────────────────────────
    # Shot lengths use the same unit as Pre Unit: a putt's roll in feet.
    "Carry Game", "Carry Raw", "Total Length",
    "Ball Speed", "Total Spin", "Spin Axis", "VLA", "HLA",
    # ── Environment / context (not in SG model — kept for future analysis) ──────
    "Wind Speed (E1)", "Wind Dir 2 (T1)",
    "Height Delta", "Lie Angles",
    "Shot Loc 1 (raw)", "Shot Loc 2 (raw)",
    "Region9",
    "Hole Yardage (yd)", "Course",
    # ── Shot geometry (short/long vs offline) ───────────────────────────────────
    # Putts are resolved in feet, everything else in yards — see Geom Unit.
    "Along Line", "Offline", "Short/Long", "Geom Unit", "Distance Verdict",
    # ── Shot outcome ────────────────────────────────────────────────────────────
    # "Holed" only when the ball genuinely went in. "Gimmie zone (N ft)" when it
    # finished inside the conceded radius — a good shot, but not a hole-out, and
    # scored on the distance actually left.
    "Outcome",
    # ── Data-quality flag ───────────────────────────────────────────────────────
    "Note",
    # ── Lie penalty (T-08, format 6) — GSPro's LAST SHOT panel ─────────────────
    # Ball speed and spin kept by a shot from a penalising lie, e.g. 80 / 53 from
    # deep rough; blank when GSPro showed no penalty. Last, so no column moves.
    "Speed Kept %", "Spin Kept %",
]

_SG_HOLES_HEADERS = [
    "Date", "Time",                  # when the hole was completed
    "Hole", "Par", "Score", "+/- Par",
    "Pro Baseline (exp.strokes)",   # expected_strokes(hole_yardage, tee)
    f"SG-{sg_mod.CAT_OTT}", f"SG-{sg_mod.CAT_APP}",
    f"SG-{sg_mod.CAT_ATG}", f"SG-{sg_mod.CAT_PUTT}",
    "SG-Total", "Player vs PGA Tour",
]

def _ensure_sg_headers(sink, r: dict) -> None:
    """Write column headers to SG_Shots and SG_Holes on first use."""
    if r["sg_shots_row"] == 1:
        sink.write_row("SG_Shots", 0, _SG_SHOTS_HEADERS)
    if r["sg_holes_row"] == 1:
        sink.write_row("SG_Holes", 0, _SG_HOLES_HEADERS)

def _fmt(v: float | None, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}" if v is not None else ""

def _finalize_hole(r: dict, sink) -> None:
    """Write hole summary row to SG_Holes and update round accumulators."""
    hole   = r["hole_number"]
    par    = r["hole_par"]
    shots  = r["shots_completed"]
    _note_hole(r, "recorded", strokes=shots)
    sg_h   = r["sg_this_hole"]
    total  = round(sum(sg_h.values()), 3)
    diff   = shots - par

    # PGA Tour expected score from this tee (the "pro baseline" for the hole)
    hole_baseline = round(sg_mod.expected_strokes(r["hole_yardage"], sg_mod.LIE_TEE), 4)

    # Player vs pro for this hole: positive = player worse than PGA Tour
    vs_pro_strokes = round(shots - hole_baseline, 2)
    vs_pro_label   = (
        f"{vs_pro_strokes:+.2f} strokes vs PGA Tour avg "
        f"({'WORSE' if vs_pro_strokes > 0 else 'BETTER'})"
    )

    _ensure_sg_headers(sink, r)
    row_idx  = r["sg_holes_row"]
    hdate, htime = _now_parts()
    sink.write_row("SG_Holes", row_idx, [
        hdate, htime,
        str(hole), str(par), str(shots), f"{diff:+d}",
        f"{hole_baseline:.4f}",
        f"{sg_h.get(sg_mod.CAT_OTT, 0.0):+.3f}",
        f"{sg_h.get(sg_mod.CAT_APP, 0.0):+.3f}",
        f"{sg_h.get(sg_mod.CAT_ATG, 0.0):+.3f}",
        f"{sg_h.get(sg_mod.CAT_PUTT,0.0):+.3f}",
        f"{total:+.3f}",
        vs_pro_label,
    ])

    r["sg_holes_row"]      += 1
    r["holes_completed"]   += 1
    r["score_vs_par"]       = r.get("score_vs_par", 0) + diff
    r["pro_baseline_round"] = round(r.get("pro_baseline_round", 0.0) + hole_baseline, 4)
    r["completed_shots"]    = r.get("completed_shots", 0) + shots
    r["completed_par"]      = r.get("completed_par", 0) + par
    # Count how each committed stroke is known, then commit. Discarded holes
    # never reach here, so their strokes are never counted.
    q = r.get("quality") or _new_quality()
    estimated = 0
    for shot in r.get("pending_shots", []):
        if shot.get("kind") == "recovered":
            q["strokes_recovered"] = q.get("strokes_recovered", 0) + 1
        elif not shot.get("gimmie"):
            q["strokes_captured"] = q.get("strokes_captured", 0) + 1
        elif shot.get("kind") == "conceded":
            q["strokes_conceded"] = q.get("strokes_conceded", 0) + 1
        else:
            q["strokes_uncaptured"] = q.get("strokes_uncaptured", 0) + 1
            estimated += 1
    r["quality"] = q
    # The hole is committed; its shots are no longer provisional.
    r["pending_shots"] = []
    r["hole_finalised"] = True

    # Append to per-hole history for the Round Stats sheet
    r.setdefault("hole_history", []).append({
        "hole":          hole,
        "par":           par,
        "shots":         shots,
        "diff":          diff,
        "hole_baseline": round(hole_baseline, 2),
        "sg_by_cat":     dict(sg_h),
        "sg_total":      round(total, 3),
        "date":          hdate,
        "time":          htime,
        "estimated":     estimated,     # strokes inferred from the round score (T-18)
    })
    log.info("  ⛳  Hole %d: %d strokes (%+d par)  pro-baseline=%.2f  SG=%+.3f",
             hole, shots, diff, hole_baseline, total)

def _fmtd(val, lie: str, decimals: int = 1) -> str:
    """Format a yard distance in the display unit for *lie* (ft on the green)."""
    if val is None or val == "":
        return ""
    try:
        return f"{sg_mod.to_display(float(val), lie):.{decimals}f}"
    except (TypeError, ValueError):
        return ""

def _sg_shot_row(
    r: dict, parsed: dict,
    hole: int, par: int, shot_num: int,
    cat: str, pre_dist: float, pre_lie: str,
    post_dist: float, post_lie: str,
    sg: float, note: str = "",
) -> list:
    """Build the full SG_Shots row list (matches _SG_SHOTS_HEADERS exactly)."""
    sg_cumul = round(sum(r["sg_round"].values()), 3)
    pro_baseline = sg_mod.expected_strokes(pre_dist, pre_lie)
    sdate, stime = _now_parts()

    geom = sg_mod.shot_geometry(pre_dist, parsed.get("total_length", 0.0),
                                post_dist, pre_lie)
    def _g(k: str) -> str:
        """Geometry field in its display unit (ft for putts, yd otherwise)."""
        if not geom:
            return ""
        v = geom[k]
        if isinstance(v, str):
            return v
        if geom.get("unit") == "ft":
            v = v * sg_mod.FT_PER_YD
        v = 0.0 if abs(v) < 0.005 else v           # no "-0.0"
        return f"{v:+.1f}" if k == "delta" else f"{v:.1f}"
    return [
        # When acquired
        sdate, stime,
        # SG context
        str(hole), str(par), str(shot_num), cat,
        parsed.get("shot_club", ""),
        f"{sg_mod.to_display(pre_dist, pre_lie):.1f}",
        sg_mod.display_unit(pre_lie), pre_lie,
        f"{sg_mod.to_display(post_dist, post_lie):.1f}",
        sg_mod.display_unit(post_lie), post_lie,
        f"{sg:+.3f}", f"{sg_cumul:+.3f}",
        # Player vs PGA Tour
        f"{pro_baseline:.4f}",
        sg_mod.explain_sg(sg, cat),
        # Launch monitor
        _fmtd(parsed.get("carry_game"),  pre_lie),
        _fmtd(parsed.get("carry_raw"),   pre_lie),
        _fmtd(parsed.get("total_length"), pre_lie),
        _fmt(parsed.get("ball_speed")),
        _fmt(parsed.get("total_spin"), 0), _fmt(parsed.get("spin_axis"), 1),
        _fmt(parsed.get("vla")),           _fmt(parsed.get("hla")),
        # Environment
        parsed.get("wind_speed", ""),      parsed.get("wind_2", ""),
        parsed.get("height_delta", ""),    parsed.get("lie_angles", ""),
        parsed.get("shot_loc_1", ""),      parsed.get("shot_loc_2", ""),
        parsed.get("region9", ""),
        f"{parsed.get('hole_yardage', 0):.0f}",
        parsed.get("course", ""),
        # Note
        _g("along_line"), _g("lateral"), _g("delta"),
        (geom or {}).get("unit", ""), _g("verdict"),
        _shot_outcome(post_dist, post_lie),
        note,
        *_lie_penalty(parsed, pre_lie),
    ]


def _lie_penalty(parsed: dict, pre_lie: str) -> tuple[str, str]:
    """
    Speed and spin kept for this shot, from GSPro's LAST SHOT panel. The panel
    stays on screen after the shot it describes — through putts and onto the
    next tee — so it is credited only to a shot that started off the green:
    putts are never penalised and do not clear it (T-08).
    """
    speed = parsed.get("speed_kept")
    if speed is None or pre_lie == sg_mod.LIE_GREEN:
        return "", ""
    spin = parsed.get("spin_kept")
    return str(speed), ("" if spin is None else str(spin))

def _add_reconciliation_shot(
    r: dict, parsed_next_hole: dict, sink,
    note: str, dry_run: bool, sg_override: float | None = None,
) -> None:
    """
    Synthesise one uncaptured shot (gimmie or auto-holed final putt) and
    add it to the current hole's stats.

    GSPro automatically advances past the final putt when the ball is inside
    the gimmie circle (or holes out in a flyby), giving no opportunity for an
    F9 screenshot.  When a new hole is detected and `hole_score` exceeds
    `shots_completed`, this function is called once per missing stroke.

    pre  = last saved position (where the ball was after the previous shot)
    post = 0 yards / Green (ball holed)
    SG   = baseline(pre) − 1 − 0  =  baseline(pre) − 1
    """
    shot_num  = r["shots_completed"] + 1
    pre_dist  = r["pre_distance"]
    pre_lie   = r["pre_lie"]
    post_dist = 0.0
    post_lie  = sg_mod.LIE_GREEN

    cat = sg_mod.shot_category(shot_num, pre_lie, pre_dist, par=r.get("hole_par"))
    # A conceded gimmie is a putt from a known spot. Any other inferred stroke
    # has an unknown start position, so it is not filed under a real category.
    if note != "gimmie":
        cat = sg_mod.CAT_UNATTRIBUTED
    sg  = (sg_override if sg_override is not None
           else sg_mod.sg_shot(pre_dist, pre_lie, post_dist, post_lie))

    r["shots_completed"] += 1
    r["shots_total"]      = r.get("shots_total", 0) + 1
    r["sg_this_hole"][cat] = round(r["sg_this_hole"].get(cat, 0.0) + sg, 3)
    r["sg_round"][cat]     = round(r["sg_round"].get(cat, 0.0) + sg, 3)

    # per-round stats accumulators (same as normal shots)
    r["gimmies_count"] = r.get("gimmies_count", 0) + 1
    _band = (_record_band(r, cat, pre_dist, sg, 0.0, sg_mod.LIE_GREEN)
             if cat != sg_mod.CAT_UNATTRIBUTED else "")
    r.setdefault("pending_shots", []).append(
        {"cat": cat, "lie": pre_lie, "sg": sg, "gimmie": True,
         "kind": "conceded" if note == "gimmie" else "uncaptured",
         "band": _band, "post_dist": 0.0, "holed": True}
    )
    if r.get("shot_counts_by_cat") is None:
        r["shot_counts_by_cat"] = {c: 0 for c in sg_mod.SG_CATS}
    r["shot_counts_by_cat"][cat] = r["shot_counts_by_cat"].get(cat, 0) + 1
    lie_cnt = r.setdefault("shot_counts_by_lie", {})
    lie_sg  = r.setdefault("sg_by_lie", {})
    lie_cnt[pre_lie] = lie_cnt.get(pre_lie, 0) + 1
    lie_sg[pre_lie]  = round(lie_sg.get(pre_lie, 0.0) + sg, 3)

    log.info(
        "  ↳ Reconciled %s  hole=%d  shot=%d  %s  "
        "pre=(%.1f yd, %s)  post=holed  sg=%+.3f",
        note, r["hole_number"], shot_num, cat, pre_dist, pre_lie, sg,
    )

    if not dry_run:
        _ensure_sg_headers(sink, r)
        # Build a partial parsed dict using previous-hole context
        phantom = dict(parsed_next_hole)   # copy carries course name etc.
        phantom["carry_game"] = phantom["carry_raw"] = None
        phantom["total_length"] = phantom["ball_speed"] = None
        phantom["total_spin"] = phantom["spin_axis"] = None
        phantom["vla"] = phantom["hla"] = None
        phantom["wind_1"] = phantom["wind_2"] = phantom["wind_speed"] = ""
        phantom["height_delta"] = phantom["lie_angles"] = ""
        phantom["shot_loc_1"] = phantom["shot_loc_2"] = ""
        phantom["region9"] = ""
        phantom["shot_club"] = ""              # an inferred stroke: club unknown
        phantom["hole_yardage"] = r["hole_yardage"]
        phantom["course"]       = r.get("last_course", parsed_next_hole.get("course", ""))

        sink.write_row("SG_Shots", r["sg_shots_row"],
                  _sg_shot_row(r, phantom, r["hole_number"], r["hole_par"],
                               shot_num, cat,
                               pre_dist, pre_lie, post_dist, post_lie,
                               sg, note=note))
        r["sg_shots_row"] += 1

    r["pre_distance"] = post_dist
    r["pre_lie"]      = post_lie

def process_shot(parsed: dict, state: dict, sink, dry_run: bool = False) -> dict:
    """
    Update round state and (if applicable) calculate + write SG for the
    shot just completed.  Returns the updated state dict.

    First capture of each hole   → sets up pre-shot tee state, no SG written.
    Subsequent captures same hole → SG written for the completed shot.

    New-hole transition  → reconcile any uncaptured shots (gimmie / auto-holed
    final putt) by comparing shots_completed to the 'hole score' OCR field
    before closing out the previous hole.
    """
    r = state["round"]
    if r["sg_this_hole"] is None:
        r["sg_this_hole"] = _zero_cats()
    if r["sg_round"] is None:
        r["sg_round"] = _zero_cats()

    # Cache course name so reconciliation shots can reference it even after
    # the hole has been reset.
    if parsed.get("course"):
        r["last_course"] = parsed["course"]

    hole      = parsed["hole"]
    par       = parsed["par"]
    hyd       = parsed["hole_yardage"]
    remaining = parsed["remaining"]
    lie       = parsed["lie"]

    # ── detect new hole ────────────────────────────────────────────────────────
    # A new hole starts when the number changes OR the previous hole is over —
    # distance-challenge stations repeat a hole number for every attempt, and
    # identifying holes by number alone carried one attempt's strokes into the next.
    new_hole = (hole > 0) and (hole != r["hole_number"] or bool(r.get("hole_over")))

    if new_hole:
        _reconcile_and_finalise(r, parsed, sink, dry_run, next_hole=hole)
        if 0 < hole < (r.get("hole_number") or 0):            # a new GSPro round
            _report_round(r, heading="Round ended")         # if not already (T-35)
            r["round_pars"] = {}
            r["end_of_round_strokes"] = None
            r["round_log"], r["round_reported"] = {}, False

        # ── initialise new hole ────────────────────────────────────────────────
        r["hole_number"]              = hole
        r["hole_par"]                 = par if par > 0 else 4
        r.setdefault("round_pars", {})[str(hole)] = r["hole_par"]     # for T-28
        r["hole_yardage"]             = hyd if hyd > 0 else r["hole_yardage"]
        r["hole_started"]             = True
        r["hole_over"]                = None
        r["hole_finalised"]           = False
        r["shots_completed"]          = 0
        r["pre_distance"]             = r["hole_yardage"]
        r["pre_lie"]                  = sg_mod.LIE_TEE
        r["pre_club"]                 = parsed.get("club", "")   # the tee shot's club
        r["sg_this_hole"]             = _zero_cats()
        # Store the round score NOW as the baseline for THIS hole's reconciliation.
        # If this frame's reading lost its sign, use the value the previous
        # hole's closing settled (score_carry), when it settled one.
        start   = parsed.get("round_score")
        carried = r.pop("score_carry", None)
        if start is None and carried is not None:
            start = carried
        r["round_score_at_hole_start"] = start
        r["round_score_at_hole_start_unsigned"] = (parsed.get("round_score_unsigned")
                                                   if start is None else None)

        rs_label = (f"{r['round_score_at_hole_start']:+d}"
                    if r["round_score_at_hole_start"] is not None else "—")
        log.info(
            "  ◉  Hole %d  par %d  %.0f yd  round score=%s — pre-state = tee",
            hole, r["hole_par"], r["hole_yardage"], rs_label,
        )
        return state   # no SG for the setup capture itself

    # ── same hole, shot completed ──────────────────────────────────────────────
    # The round score cannot change during a hole, so one read on its first
    # frame is not the only chance: any later capture on the hole can supply it.
    if r.get("round_score_at_hole_start") is None and parsed.get("round_score") is not None:
        r["round_score_at_hole_start"] = parsed["round_score"]
        r["round_score_at_hole_start_unsigned"] = None
        log.info("  Round score for hole %d: %+d (unreadable when the hole started)",
                 r.get("hole_number", 0), parsed["round_score"])
    if not r["hole_started"]:
        log.warning("  Shot captured before hole setup — press F9 at hole start first")
        return state

    pre_dist  = r["pre_distance"]
    pre_lie   = r["pre_lie"]
    post_dist = remaining
    post_lie  = lie

    r["shots_completed"] += 1
    r["shots_total"]      = r.get("shots_total", 0) + 1
    shot_num = r["shots_completed"]

    cat = sg_mod.shot_category(shot_num, pre_lie, pre_dist, par=r.get("hole_par"))
    sg  = sg_mod.sg_shot(pre_dist, pre_lie, post_dist, post_lie)

    r["sg_this_hole"][cat] = round(r["sg_this_hole"].get(cat, 0.0) + sg, 3)
    r["sg_round"][cat]     = round(r["sg_round"].get(cat, 0.0) + sg, 3)

    # ── per-round statistics accumulators ────────────────────────────────────
    if r.get("shot_counts_by_cat") is None:
        r["shot_counts_by_cat"] = {c: 0 for c in sg_mod.SG_CATS}
    r["shot_counts_by_cat"][cat] = r["shot_counts_by_cat"].get(cat, 0) + 1
    lie_cnt = r.setdefault("shot_counts_by_lie", {})
    lie_sg  = r.setdefault("sg_by_lie", {})
    lie_cnt[pre_lie] = lie_cnt.get(pre_lie, 0) + 1
    lie_sg[pre_lie]  = round(lie_sg.get(pre_lie, 0.0) + sg, 3)

    _band = _record_band(r, cat, pre_dist, sg, post_dist, post_lie)
    # The club that hit THIS shot is the one shown before it (see pre_club).
    shot_club = r.get("pre_club", "")
    parsed = {**parsed, "shot_club": shot_club}
    _record_geometry(r, shot_club,
                     sg_mod.shot_geometry(pre_dist,
                                          parsed.get("total_length", 0.0),
                                          post_dist, pre_lie))

    # Provisional until the hole is finalised (see _discard_incomplete_hole)
    r.setdefault("pending_shots", []).append(
        {"cat": cat, "lie": pre_lie, "sg": sg, "gimmie": False,
         "band": _band, "post_dist": post_dist,
         "holed": _is_holed(post_dist, post_lie)}
    )

    if parsed.get("course"):
        r["course_name"] = parsed["course"]
    if not r.get("round_start_time"):
        r["round_start_time"] = now().strftime("%Y-%m-%d %H:%M")

    log.info("  SG  hole=%-2d  shot=%-2d  %s  "
             "pre=(%s, %s)  post=(%s, %s)  sg=%+.3f  cum=%+.3f",
             hole, shot_num, cat,
             sg_mod.format_distance(pre_dist, pre_lie), pre_lie,
             sg_mod.format_distance(post_dist, post_lie), post_lie,
             sg, round(sum(r["sg_round"].values()), 3))

    if not dry_run:
        _ensure_sg_headers(sink, r)
        sink.write_row("SG_Shots", r["sg_shots_row"],
                  _sg_shot_row(r, parsed, hole, par, shot_num,
                               cat, pre_dist, pre_lie, post_dist, post_lie, sg))
        r["sg_shots_row"] += 1

    # Update pre-state for next shot
    r["pre_distance"] = post_dist
    r["pre_lie"]      = post_lie
    r["pre_club"]     = parsed.get("club", "")            # club for the next shot

    # Mark the hole as over; do NOT finalise yet. The round score that verifies
    # the stroke count only updates after the hole, so the hole is finalised
    # once, when the next hole starts or the session ends. Finalising here as
    # well counted every holed-out hole twice.
    if _is_holed(post_dist, post_lie):
        r["hole_over"] = "holed"
    elif _in_gimmie_zone(post_dist, post_lie):
        r["hole_over"] = "gimmie"
        log.info("  ⛳  In gimmie zone — %.1f ft left, NOT holed. Scored on the "
                 "distance remaining; the conceded putt is counted when the "
                 "hole closes.", post_dist * sg_mod.FT_PER_YD)
    if not dry_run:
        sink.summary_changed(r)

    return state

# ── Holed vs conceded ─────────────────────────────────────────────────────────
# These are two different events and conflating them corrupts both the stroke
# count and the strokes-gained attribution.
#
# HOLED_EPSILON_YD   the ball is actually IN the hole. Only at this distance is
#                    the hole complete and the shot a genuine make.
#
# GIMMIE_ZONE_YD     the ball finished inside GSPro's gimmie circle. It was a
#                    good shot, but it did NOT go in: a further stroke is
#                    conceded and still counts on the card. Such a shot is
#                    scored on its ACTUAL remaining distance, and the conceded
#                    putt is recorded as its own stroke with its own SG.
#                    Override in config.json under "gimmie_zone_yd" to match the
#                    gimmie radius set in GSPro (0.5 yd = 1.5 ft, 1.0 yd = 3 ft).
HOLED_EPSILON_YD = 0.05          # ~2 inches — effectively in the cup

GIMMIE_ZONE_YD   = 0.5           # replaced from config at startup

# ── Final strokes recovered from the next tee frame (T-27, v3.7.0) ────────────
# When a hole ends on a holed stroke or a ball in the gimmie circle, GSPro moves
# straight to the next hole, so the final stroke is never captured. The next
# hole's first frame still shows that stroke's launch data, and where it started
# is the last captured position. With the stroke count from the round score —
# one missing: holed; two missing: stopped in the gimmie circle, then conceded —
# the stroke is recorded as a real shot instead of "Unattributed".
#
# Recovered only when the evidence agrees: the frame shows the next hole
# untouched (on the tee, stroke 1, full yardage), its shot panel changed since
# the last capture (so it shows a new shot), and the shot's length fits.
# The stopping point of a stroke that ended in the gimmie circle is estimated
# as |start − total length|; the panel gives no direction to the hole. Neither
# choice changes the hole's score or its strokes-gained total, which both come
# from the round score — only how the strokes are classified.
RECOVER_FINAL_STROKES = True      # config: "recover_final_strokes"
RECOVER_HOLED_TOL_FT  = 2.0       # a holed stroke's length matches its start this closely
RECOVER_MAX_GIMMIE_FT = 10.0      # beyond GSPro's largest gimmie setting
_PANEL_KEYS = ("vla", "hla", "ball_speed", "total_length", "total_spin", "spin_axis")
_gimmie_hint_given = False


def _panel(parsed: dict | None) -> list:
    """The shot panel's values, to tell whether a frame shows a new shot."""
    return [round(float((parsed or {}).get(k) or 0.0), 2) for k in _PANEL_KEYS]


def _plan_final_stroke(r: dict, parsed: dict | None, missing: int) -> dict | None:
    """
    How a hole's uncaptured final stroke(s) went, read from the next hole's
    first frame — or None when the evidence does not support it.
    Returns {"end": stopping distance (yd), "holed": bool, "conceded": bool}.
    """
    global _gimmie_hint_given
    if not RECOVER_FINAL_STROKES or not parsed or missing not in (1, 2):
        return None
    # The frame must show the next hole untouched.
    hyd, rem = parsed.get("hole_yardage") or 0.0, parsed.get("remaining") or 0.0
    if (parsed.get("lie") != sg_mod.LIE_TEE
            or parsed.get("hole_score_ticker") not in (0, 1)
            or (hyd and abs(rem - hyd) > max(2.0, 0.1 * hyd))):
        return None
    length = parsed.get("total_length") or 0.0
    if length <= 0 or (parsed.get("ball_speed") or 0.0) <= 0:
        return None
    if r.get("last_panel") is not None and _panel(parsed) == list(r["last_panel"]):
        return None                       # no new shot on the panel (e.g. a penalty stroke)
    pre    = r.get("pre_distance") or 0.0
    end    = abs(pre - length)
    end_ft = end * sg_mod.FT_PER_YD
    if missing == 1:                      # the final stroke was holed
        tol = max(RECOVER_HOLED_TOL_FT, 0.2 * pre * sg_mod.FT_PER_YD)
        return {"end": 0.0, "holed": True, "conceded": False} if end_ft <= tol else None
    tol = max(2.0, 0.1 * length * sg_mod.FT_PER_YD)    # stopped in the gimmie circle
    if end_ft > RECOVER_MAX_GIMMIE_FT + tol:
        return None
    if end > GIMMIE_ZONE_YD * 1.5 and not _gimmie_hint_given:
        _gimmie_hint_given = True
        log.info("  ℹ  GSPro conceded a putt from about %.1f ft, but gimmie_zone_ft is "
                 "%.1f — consider setting it to match GSPro's gimmie distance",
                 end_ft, GIMMIE_ZONE_YD * sg_mod.FT_PER_YD)
    return {"end": max(end, 1 / 36), "holed": False, "conceded": True}


def _add_recovered_shot(r: dict, parsed_next: dict, sink, dry_run: bool,
                        post_dist: float, holed: bool) -> None:
    """
    Record a hole's final stroke from the next hole's first frame: the same
    bookkeeping as a captured shot (process_shot), with the shot panel's
    launch data and the club shown before the stroke.
    """
    shot_num  = r["shots_completed"] + 1
    pre_dist  = r["pre_distance"]
    pre_lie   = r["pre_lie"]
    post_dist = 0.0 if holed else post_dist
    post_lie  = sg_mod.LIE_GREEN

    r["shots_completed"] += 1
    r["shots_total"]      = r.get("shots_total", 0) + 1
    cat = sg_mod.shot_category(shot_num, pre_lie, pre_dist, par=r.get("hole_par"))
    sg  = sg_mod.sg_shot(pre_dist, pre_lie, post_dist, post_lie)
    r["sg_this_hole"][cat] = round(r["sg_this_hole"].get(cat, 0.0) + sg, 3)
    r["sg_round"][cat]     = round(r["sg_round"].get(cat, 0.0) + sg, 3)
    if r.get("shot_counts_by_cat") is None:
        r["shot_counts_by_cat"] = {c: 0 for c in sg_mod.SG_CATS}
    r["shot_counts_by_cat"][cat] = r["shot_counts_by_cat"].get(cat, 0) + 1
    lie_cnt = r.setdefault("shot_counts_by_lie", {})
    lie_sg  = r.setdefault("sg_by_lie", {})
    lie_cnt[pre_lie] = lie_cnt.get(pre_lie, 0) + 1
    lie_sg[pre_lie]  = round(lie_sg.get(pre_lie, 0.0) + sg, 3)
    _band = _record_band(r, cat, pre_dist, sg, post_dist, post_lie)
    shot_club = r.get("pre_club", "")
    _record_geometry(r, shot_club, sg_mod.shot_geometry(
        pre_dist, parsed_next.get("total_length", 0.0), post_dist, pre_lie))
    r.setdefault("pending_shots", []).append(
        {"cat": cat, "lie": pre_lie, "sg": sg, "gimmie": False, "kind": "recovered",
         "band": _band, "post_dist": post_dist, "holed": holed})
    note = "recovered: holed" if holed else "recovered: stopped in gimmie circle"
    log.info("  ↳ Recovered final stroke  hole=%d  shot=%d  %s  pre=(%s, %s)  post=(%s)  "
             "sg=%+.3f  [next tee frame: %s, %s mph]",
             r["hole_number"], shot_num, cat, sg_mod.format_distance(pre_dist, pre_lie),
             pre_lie, "holed" if holed else
             f"{sg_mod.format_distance(post_dist, post_lie)}, gimmie circle",
             sg, sg_mod.format_distance(parsed_next.get("total_length") or 0.0, pre_lie),
             parsed_next.get("ball_speed"))
    if not dry_run:
        _ensure_sg_headers(sink, r)
        row_parsed = dict(parsed_next)
        for k in ("wind_1", "wind_2", "wind_speed", "height_delta", "lie_angles",
                  "shot_loc_1", "shot_loc_2", "region9"):
            row_parsed[k] = ""                    # those belong to the next hole
        row_parsed["shot_club"]    = shot_club
        row_parsed["hole_yardage"] = r["hole_yardage"]
        row_parsed["course"]       = r.get("last_course", parsed_next.get("course", ""))
        sink.write_row("SG_Shots", r["sg_shots_row"],
                       _sg_shot_row(r, row_parsed, r["hole_number"], r["hole_par"], shot_num,
                                    cat, pre_dist, pre_lie, post_dist, post_lie, sg, note=note))
        r["sg_shots_row"] += 1
    r["pre_distance"] = post_dist
    r["pre_lie"]      = post_lie

def _record_geometry(r: dict, club: str, geom: dict | None) -> None:
    """
    Accumulate short/long tendency for the club used.

    Aggregating by CLUB is what makes this actionable: a persistent short bias
    with one club is a gapping or expectation problem you can fix, whereas the
    same figure pooled across the bag averages opposite biases into nothing.
    """
    if not geom:
        return
    # Every stroke from the green is a putt and is reported in FEET, whatever
    # the club field says. Keying putts on their own also isolates them from
    # off-green shots carrying a stale "Putter" club read, which would otherwise
    # mix yard-scale approach misses into the putting row.
    if geom.get("unit") == "ft":
        key = "Putting (on green)"
    else:
        key = (club or "").strip() or "(unknown club)"
    e = r.setdefault("geom_stats", {}).setdefault(
        key, {"n": 0, "short": 0, "long": 0, "pinhigh": 0,
              "delta_sum": 0.0, "abs_delta_sum": 0.0, "lateral_sum": 0.0,
              "unit": geom.get("unit", "yd")})
    e["n"] += 1
    v = geom["verdict"]
    e["short"   if v == "Short" else "long" if v == "Long" else "pinhigh"] += 1
    e["delta_sum"]     = round(e["delta_sum"] + geom["delta"], 2)
    e["abs_delta_sum"] = round(e["abs_delta_sum"] + abs(geom["delta"]), 2)
    e["lateral_sum"]   = round(e["lateral_sum"] + geom["lateral"], 2)

def _shot_outcome(post_dist: float, post_lie: str) -> str:
    """Label a shot's outcome for the SG_Shots sheet."""
    if _is_holed(post_dist, post_lie):
        return "Holed"
    if _in_gimmie_zone(post_dist, post_lie):
        return f"Gimmie zone ({post_dist * sg_mod.FT_PER_YD:.1f} ft left)"
    return ""

def _is_holed(post_dist: float, post_lie: str) -> bool:
    """True only when the ball is genuinely in the hole."""
    return post_dist <= HOLED_EPSILON_YD

def _in_gimmie_zone(post_dist: float, post_lie: str) -> bool:
    """Inside the conceded radius but not in the hole."""
    return (not _is_holed(post_dist, post_lie)
            and post_dist <= GIMMIE_ZONE_YD
            and post_lie == sg_mod.LIE_GREEN)

def _session_boundary(r: dict, parsed: dict, gap_minutes: float) -> str:
    """
    Decide whether this capture starts a NEW session. Returns a reason, or "".

    Two triggers, both conservative:

      course change — a different course is unambiguously a different session.
                      Blank OCR reads are ignored rather than treated as a
                      change, since the course field reads empty fairly often.

      idle gap      — more than gap_minutes since the last capture. Covers
                      quitting and coming back later, which leaves no other
                      trace because round state persists across daemon restarts.

    Hole-number regression is deliberately NOT a trigger: practice and
    distance-challenge modes legitimately revisit hole numbers, so it would
    split a single session repeatedly.
    """
    if not r.get("session_started"):
        return ""                       # nothing open yet

    cur = parsing._normalize_course(r.get("session_course", ""))
    new = parsing._normalize_course(parsed.get("course", ""))
    if cur and new and cur != new:
        return f"course changed ({r.get('session_course','?')} → {parsed.get('course','?')})"

    last = r.get("last_capture_ts") or ""
    if last:
        try:
            prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            idle = (now() - prev).total_seconds() / 60.0
            if idle > gap_minutes:
                return f"{idle:.0f} min idle (limit {gap_minutes:.0f})"
        except ValueError:
            pass
    return ""

def _record_band(r: dict, cat: str, pre_dist: float, sg_val: float,
                 post_dist: float, post_lie: str) -> str:
    """
    Bucket one shot into its distance band and update that band's tallies.

    Returns the band label so the caller can attach it to the provisional shot
    record, letting _discard_incomplete_hole reverse it exactly.

    Tracked per band:
      n        shots played from the band
      sg       total strokes gained from the band
      holed    shots that finished in the hole (post distance ~ 0)
      holed    genuine holes only (HOLED_EPSILON_YD). A ball conceded inside
               the gimmie circle is NOT a make and is excluded here.
      prox     sum/count of resulting distance to the hole, for proximity —
               the standard approach measure. Only accumulated for shots that
               did NOT hole out, since a holed shot has no meaningful proximity.
    """
    band = sg_mod.distance_band(pre_dist, cat)
    bs   = r.setdefault("band_stats", {}).setdefault(cat, {})
    e    = bs.setdefault(band, {"n": 0, "sg": 0.0, "holed": 0,
                                "prox_sum": 0.0, "prox_n": 0})
    e["n"]  += 1
    e["sg"]  = round(e["sg"] + sg_val, 3)
    if _is_holed(post_dist, post_lie):
        e["holed"] += 1
    else:
        e["prox_sum"] = round(e["prox_sum"] + max(0.0, post_dist), 2)
        e["prox_n"]  += 1
    return band

def _unrecord_band(r: dict, cat: str, band: str, sg_val: float,
                   post_dist: float, holed: bool) -> None:
    """Exact reversal of _record_band, for discarding an unfinished hole."""
    e = (r.get("band_stats", {}).get(cat, {}) or {}).get(band)
    if not e:
        return
    e["n"]  = max(0, e["n"] - 1)
    e["sg"] = round(e["sg"] - sg_val, 3)
    if holed:
        e["holed"] = max(0, e["holed"] - 1)
    else:
        e["prox_sum"] = round(max(0.0, e["prox_sum"] - max(0.0, post_dist)), 2)
        e["prox_n"]   = max(0, e["prox_n"] - 1)
    if e["n"] == 0:
        r["band_stats"][cat].pop(band, None)

# Most strokes a round-score reconciliation may add to one hole. Real misses —
# a gimmie, an auto-holed putt, a shot the capture missed — are a few strokes;
# a larger jump means the round score is unusable (OCR misread, or a reset).
MAX_RECONCILE_STROKES = 5


def _score_options(signed: int | None, unsigned: int | None) -> list[int]:
    if signed is not None:
        return [signed]
    if unsigned is not None:
        return sorted({unsigned, -unsigned})
    return []


def _resolve_scores(r: dict, parsed: dict | None) -> tuple[int | None, int | None, str, bool]:
    """
    The round score at this hole's start and now, restoring a sign the OCR lost
    (v3.8.1). A bare "6" can only be +6 or −6; of the pairs the readings allow,
    keep those that give a possible hole (at least one stroke, at most
    MAX_RECONCILE_STROKES more than were captured). If they all agree on the
    stroke count, that count is safe even if a sign is still open.
    Returns (now, before, note, now_sign_certain); (None, None, …) if unsettled.
    """
    p = parsed or {}
    now_o = _score_options(p.get("round_score"), p.get("round_score_unsigned"))
    bef_o = _score_options(r.get("round_score_at_hole_start"),
                           r.get("round_score_at_hole_start_unsigned"))
    if not now_o or not bef_o:
        return None, None, "", False
    if len(now_o) == 1 and len(bef_o) == 1:
        return now_o[0], bef_o[0], "", True
    par, done = r.get("hole_par", 4), r.get("shots_completed", 0)
    fits = [(b, n) for b in bef_o for n in now_o
            if par + n - b >= 1 and 0 <= par + n - b - done <= MAX_RECONCILE_STROKES]
    if not fits or len({n - b for b, n in fits}) != 1:
        return None, None, "a lost minus sign could not be settled", False
    b, n = fits[0]
    certain = len({x for _, x in fits}) == 1
    note = ("round score read without its sign — only " + (f"{n:+d}" if certain else
            "one stroke count") + " fits the hole")
    return n, b, note, certain


# ── Each GSPro round checked as it ends (T-35, v3.8.6) ─────────────────────────
SCORE_WARN_AFTER = 3        # captures in a row with no valid round score before warning


def _note_hole(r: dict, status: str, strokes: int | None = None, reason: str = "") -> None:
    """Record how the current hole ended up, for the end-of-round check."""
    hole = r.get("hole_number", 0)
    if hole > 0:
        r.setdefault("round_log", {})[str(hole)] = {"status": status, "strokes": strokes,
                                                     "reason": reason}


def _round_summary(r: dict, end_total: int | None = None,
                   last: tuple[int, int | None] | None = None,
                   heading: str = "Round complete") -> tuple[list[str], bool]:
    """
    Lines describing this GSPro round — holes recorded, strokes, GSPro's total
    when shown — and whether anything was lost or does not add up. `last` is
    the last hole and its strokes as settled by the end-of-round total (None
    strokes: it could not be settled).
    """
    log_ = {int(k): v for k, v in (r.get("round_log") or {}).items()}
    if last is not None:
        hole, strokes = last
        log_[hole] = ({"status": "recorded", "strokes": strokes, "reason": ""} if strokes
                      else {"status": "lost", "strokes": None,
                            "reason": "GSPro's end-of-round score could not settle it"})
    seen = {int(k) for k in (r.get("round_pars") or {})} | set(log_)
    if not seen:
        return [], False
    first = 10 if min(seen) == 10 else 1
    holes = range(first, max(seen) + 1)
    rec = [h for h in holes if log_.get(h, {}).get("status") == "recorded"]
    strokes = sum(log_[h].get("strokes") or 0 for h in rec)
    lost = [(h, log_.get(h, {}).get("reason") or
             ("never seen" if h not in seen else "not finished")) for h in holes if h not in rec]
    n = len(holes)
    head = f"{heading} — {len(rec)} of {n} holes recorded, {strokes} strokes"
    bad = bool(lost)
    if end_total is not None:
        if not lost and strokes == end_total:
            head += f", matching GSPro's {end_total}  ✓"
        elif not lost:
            head += f" — but GSPro shows {end_total}"
            bad = True
        else:
            head += f" (GSPro's total: {end_total})"
    elif heading == "Round complete":
        head += " (no end-of-round screen to check against)"
    lines = [f"━━━  {head}  ━━━"]
    if lost:
        lines.append("       not recorded: " + "; ".join(f"hole {h} — {why}" for h, why in lost))
    return lines, bad


def _report_round(r: dict, end_total: int | None = None,
                  last: tuple[int, int | None] | None = None,
                  heading: str = "Round complete", final: bool = True) -> None:
    """Say how the round went, once per round (final=False: an interim report)."""
    if r.get("round_reported"):
        return
    lines, bad = _round_summary(r, end_total, last, heading)
    for line in lines:
        (log.warning if bad else log.info)(line)
    if lines and final:
        r["round_reported"] = True


def _strokes_from_end_total(r: dict, total: int, quiet: bool = False) -> int | None:
    """
    The last hole's strokes from GSPro's end-of-round total (T-28): the total
    minus the strokes before this hole — the pars of the round's earlier holes
    plus the round score when this hole started. Used only when every earlier
    hole of the round was seen (so their pars are known) and exactly one
    possible hole results; otherwise None, and nothing is guessed.
    """
    n, done = r.get("hole_number", 0), r.get("shots_completed", 0)
    pars = {int(k): v for k, v in (r.get("round_pars") or {}).items()}
    first = 10 if pars and min(pars) == 10 else 1               # a back-nine round
    unseen = [h for h in range(first, n) if h not in pars]
    if unseen:
        if quiet:
            return None
        log.warning("  ⚠️  End-of-round score %d not used: hole(s) %s of this round were "
                    "not seen, so the strokes before hole %d are unknown",
                    total, ", ".join(map(str, unseen)), n)
        return None
    before_par = sum(pars[h] for h in range(first, n))
    options = _score_options(r.get("round_score_at_hole_start"),
                             r.get("round_score_at_hole_start_unsigned"))
    fits = {total - before_par - b for b in options}
    fits = {a for a in fits if a >= 1 and 0 <= a - done <= MAX_RECONCILE_STROKES}
    if len(fits) != 1:
        if quiet:
            return None
        log.warning("  ⚠️  End-of-round score %d does not settle hole %d (round score at "
                    "its start: %s)", total, n, r.get("round_score_at_hole_start"))
        return None
    return fits.pop()


def _record_end_of_round(state: dict, parsed: dict) -> dict:
    """GSPro's end-of-round screen is showing: remember its total for the open hole."""
    r, total = state["round"], parsed["end_of_round_strokes"]
    q = _quality(state)
    q["captures"] += 1
    q["end_screens"] = q.get("end_screens", 0) + 1
    if r.get("hole_started") and not r.get("hole_finalised"):
        if r.get("end_of_round_strokes") != total:
            log.info("  🏁  End of round: GSPro shows %d strokes — hole %d will be closed "
                     "from it", total, r.get("hole_number", 0))
        r["end_of_round_strokes"] = total
        # Check the round now, while the end screen is up (T-35): the last
        # hole's strokes already follow from the total.
        _report_round(r, total, (r.get("hole_number", 0),
                                 _strokes_from_end_total(r, total, quiet=True)))
    else:
        log.info("  🏁  End-of-round screen (%d strokes) — no hole open", total)
    return state


def _reconcile_and_finalise(r: dict, parsed: dict | None, sink,
                            dry_run: bool = False, next_hole: int | None = None) -> bool:
    """
    Close the current hole EXACTLY ONCE: add the strokes known to be missing,
    then finalise. Returns True if a hole was finalised.

    parsed     the first capture of the next hole (whose round score has
               updated), or None at the end of a session or run
    next_hole  that capture's hole number, used to detect a new GSPro round

    Missing strokes come from the round score when it can be trusted:
        actual = par + (round score now − round score at hole start)
    It cannot be trusted across a new GSPro round — the hole number wraps (9→1)
    and the round score resets to E, which would read as strokes — or when the
    jump is implausible. Then only what is certain is added: nothing for a
    holed ball, the one conceded putt for a ball in the gimmie zone.

    A hole with NO captured shot is still closed when the round score says how
    it went (v3.6.1): on short holes a tee shot that finishes in the gimmie
    circle sends GSPro straight to the next hole, leaving nothing to capture.

    A hole that was NOT seen finished, and that the round score cannot account
    for (a new round, an unreadable or implausible score), is discarded rather
    than recorded with only its captured strokes (v3.6.1): that recorded e.g.
    a 1 on a par 4 for a hole whose final putt was never seen.
    """
    if r.get("hole_finalised") or not r.get("hole_started"):
        return False

    over    = r.get("hole_over")
    certain = 1 if over == "gimmie" else 0
    missing = certain
    source  = {"gimmie": "ball in gimmie zone — conceded putt",
               "holed":  "holed on a captured shot"}.get(over, "hole left unfinished")

    reconciled   = False
    score_now, score_before, sign_note, sign_certain = _resolve_scores(r, parsed)
    r["score_carry"] = score_now if (score_now is not None and sign_certain) else None
    if sign_note:
        log.info("  ↺  Hole %d: %s", r.get("hole_number", 0), sign_note)
    new_round    = next_hole is not None and 0 < next_hole < r.get("hole_number", 0)
    end_total = r.get("end_of_round_strokes")
    if end_total is not None and (new_round or parsed is None):
        # The round ended on this hole: GSPro's end-of-round total gives its
        # strokes (T-28, v3.8.2).
        actual = _strokes_from_end_total(r, end_total)
        if actual is not None:
            missing    = actual - r["shots_completed"]
            reconciled = True
            source     = (f"GSPro end-of-round score {end_total} strokes → hole "
                          f"{r['hole_number']}: {actual} strokes")
        r["score_carry"] = None
    if reconciled:
        pass
    elif new_round:
        source += "; new GSPro round (round score reset), not used"
        r["score_carry"] = None
    elif score_now is not None and score_before is not None:
        diff   = score_now - score_before
        actual = r["hole_par"] + diff
        m      = actual - r["shots_completed"]
        if 0 <= m <= MAX_RECONCILE_STROKES and actual >= 1:
            missing    = m
            reconciled = True
            source  = (f"round score Δ{diff:+d} ({score_before:+d} → {score_now:+d}, "
                       f"par {r['hole_par']} → {actual} strokes)")
        else:
            log.warning("  ⚠️  Round score implies %d stroke(s) but %d were captured "
                        "— not plausible, using only certain strokes",
                        actual, r["shots_completed"])

    if over is None and not reconciled:
        if new_round or parsed is None:
            why = ("GSPro's end-of-round score could not settle it" if end_total is not None
                   else "the round ended on it and GSPro's end-of-round score was not read"
                   if new_round else "the session ended before it finished")
        elif score_now is None or score_before is None:
            why = "round score unreadable"
        else:
            why = "round score did not add up (probably misread)"
        _note_hole(r, "lost", reason=why)
        if r.get("shots_completed", 0) > 0:
            log.warning("  ⚠️  Hole %d was not seen finished and the round score cannot "
                        "say how it ended (%s) — not recorded, rather than recorded "
                        "with only its %d captured stroke(s)",
                        r["hole_number"], source.replace("hole left unfinished; ", ""),
                        r["shots_completed"])
            if not dry_run:
                _discard_incomplete_hole(r, sink)
        else:
            log.warning("  ⚠️  Hole %d: no stroke was captured and the round score cannot "
                        "say how it went (%s) — not recorded", r["hole_number"],
                        source.replace("hole left unfinished; ", ""))
        return False
    if r.get("shots_completed", 0) == 0 and missing == 0:
        return False
    plan = _plan_final_stroke(r, parsed, missing) if (over is None and reconciled) else None
    log.info("  Hole %d closing: captured=%d  +%d  (%s)%s",
             r["hole_number"], r["shots_completed"], missing, source,
             "  — final stroke on the next tee frame" if plan else "")
    if plan:
        # The hole's strokes-gained total is fixed by where the final stroke
        # started: holed from there, plus one stroke per conceded putt. The
        # conceded putt takes whatever keeps that total exact, so splitting the
        # value between two rounded strokes cannot drift it by 0.001.
        holed_from_start = sg_mod.sg_shot(r["pre_distance"], r["pre_lie"], 0.0, sg_mod.LIE_GREEN)
        _add_recovered_shot(r, parsed, sink, dry_run, plan["end"], plan["holed"])
        if plan["conceded"]:
            final_sg = r["pending_shots"][-1]["sg"]
            _add_reconciliation_shot(r, parsed or {}, sink, "gimmie", dry_run,
                                     sg_override=round(holed_from_start - 1.0 - final_sg, 3))
    else:
        for i in range(missing):
            note = "gimmie" if (over == "gimmie" and i == 0) else "uncaptured stroke"
            _add_reconciliation_shot(r, parsed or {}, sink, note, dry_run)
    if not dry_run:
        _finalize_hole(r, sink)
    return True


def close_open_hole(r: dict, sink, discard_incomplete: bool = True) -> str:
    """
    End of a session or run. A hole that is OVER (holed, or ball in the gimmie
    zone) is finalised — previously a hole holed on the last capture of a run
    could be lost. A hole still in play is discarded, since it is not a result.
    Returns "finalised", "discarded" or "none".
    """
    if r.get("hole_over") and not r.get("hole_finalised"):
        return "finalised" if _reconcile_and_finalise(r, None, sink) else "none"
    if (r.get("end_of_round_strokes") is not None and r.get("hole_started")
            and not r.get("hole_finalised")):
        # The round ended on this hole and GSPro showed its total (T-28).
        result = "finalised" if _reconcile_and_finalise(r, None, sink) else "discarded"
        _report_round(r, r.get("end_of_round_strokes"))
        return result
    if discard_incomplete and r.get("hole_started") and not r.get("hole_finalised"):
        _note_hole(r, "lost", reason="the session ended before it finished")
    result = ("discarded" if discard_incomplete and _discard_incomplete_hole(r, sink)
              else "none")
    _report_round(r, heading="Round so far", final=False)
    return result


def _discard_incomplete_hole(r: dict, sink) -> bool:
    """
    Remove an unfinished hole's contribution from the round before stats are
    written.  Returns True if anything was discarded.

    Why this is needed
    ------------------
    Some GSPro game modes advance to the next target distance the moment a
    round ends.  The daemon sees a new hole, initialises it, and may record a
    shot or two — but if you quit before finishing, that hole is not a real
    result.  Leaving it in would inflate shot counts, skew per-shot SG averages
    and distort the lie distribution, all with data from a hole you never
    played out.

    What is and is not affected
    ---------------------------
    Hole-level totals (holes_completed, completed_shots, completed_par,
    score_vs_par, pro_baseline_round, hole_history) are only ever updated by
    _finalize_hole, so they are already clean and are left alone.

    The per-shot accumulators ARE updated as each shot lands, so they are
    rolled back here: shots_total, sg_round, shot_counts_by_cat,
    shot_counts_by_lie, sg_by_lie and gimmies_count.

    SG_Shots rows for the abandoned hole are retracted from the spreadsheet.
    Sheet1 raw OCR rows are deliberately kept — that sheet is the capture audit
    trail, not a statistic, and preserving it means the discarded shots can
    still be inspected after the fact.
    """
    pending = r.get("pending_shots") or []
    hole    = r.get("hole_number", 0)

    # A hole is finished only if it reached hole_history via _finalize_hole.
    # Explicit flag: "is the last history entry this hole NUMBER?" was wrong
    # whenever a hole number repeats (distance-challenge attempts).
    if r.get("hole_finalised") or not r.get("hole_started"):
        return False

    if not pending:
        # Hole was set up but no shot was recorded — nothing to roll back,
        # though it is still worth saying so plainly in the log.
        if hole:
            log.info("Hole %d was started but never played — nothing to discard.", hole)
        return False

    n_shots  = len(pending)
    n_gimmie = sum(1 for s in pending if s.get("gimmie"))
    sg_back  = round(sum(s.get("sg", 0.0) for s in pending), 3)

    log.warning(
        "━━━  Discarding incomplete hole %d: %d shot(s), %+.3f SG  ━━━━━━━━━━━",
        hole, n_shots, sg_back,
    )

    sg_round = r.setdefault("sg_round", _zero_cats())
    cat_cnt  = r.setdefault("shot_counts_by_cat", {c: 0 for c in sg_mod.SG_CATS})
    lie_cnt  = r.setdefault("shot_counts_by_lie", {})
    lie_sg   = r.setdefault("sg_by_lie", {})

    for s in pending:
        cat, lie, sg = s.get("cat"), s.get("lie"), s.get("sg", 0.0)
        if s.get("band"):
            _unrecord_band(r, cat, s["band"], sg,
                           s.get("post_dist", 0.0), bool(s.get("holed")))
        if cat in sg_round:
            sg_round[cat] = round(sg_round[cat] - sg, 3)
        if cat in cat_cnt:
            cat_cnt[cat] = max(0, cat_cnt[cat] - 1)
        if lie in lie_cnt:
            lie_cnt[lie] = max(0, lie_cnt[lie] - 1)
            if lie_cnt[lie] == 0:
                lie_cnt.pop(lie, None)
        if lie in lie_sg:
            lie_sg[lie] = round(lie_sg[lie] - sg, 3)
            if lie not in lie_cnt:
                lie_sg.pop(lie, None)

    r["shots_total"]   = max(0, r.get("shots_total", 0) - n_shots)
    r["gimmies_count"] = max(0, r.get("gimmies_count", 0) - n_gimmie)

    # Retract the SG_Shots rows this hole wrote.
    first_row = r.get("sg_shots_row", 1) - n_shots
    if first_row >= 1:
        removed = sink.truncate("SG_Shots", first_row)
        r["sg_shots_row"] = first_row
        if removed:
            log.info("  Removed %d SG_Shots row(s) for hole %d", removed, hole)

    # The hole is gone: clear its live state so nothing downstream reads it.
    r["pending_shots"]   = []
    r["shots_completed"] = 0
    r["hole_started"]    = False
    r["sg_this_hole"]    = _zero_cats()

    log.info("  Round totals corrected — %d shot(s) and %+.3f SG removed",
             n_shots, sg_back)
    log.info("  Sheet1 raw capture rows kept as an audit trail")
    return True

def _close_session(state: dict, sink, reason: str) -> None:
    """Archive the open session and start a clean one. Called mid-run."""
    r = state["round"]
    # Settle the open hole first: a session whose only hole was just holed has
    # holes_completed == 0 until that hole is finalised here.
    close_open_hole(r, sink)
    if r.get("holes_completed", 0) > 0:
        log.warning("━━━  Session ended: %s  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━", reason)
        sink.archive_session(r)
        log.info("  Archived: %s — %d hole(s), %+d vs par",
                 r.get("session_course") or "(course unknown)",
                 r.get("holes_completed", 0), r.get("score_vs_par", 0))
    else:
        log.info("Session boundary (%s) — nothing completed to archive", reason)
    # SG_Shots and SG_Holes are append-only logs spanning every session, so the
    # next-free-row positions must survive the reset. Before v2.9.0 they reset
    # to 1 here, so each new session overwrote the previous session's rows from
    # the top, and the end-of-run discard then cut the sheets down to the new
    # session's shorter length.
    keep = {k: r.get(k) for k in ("sg_shots_row", "sg_holes_row") if r.get(k)}
    state["round"] = _fix_round({})
    state["round"].update(keep)

def _open_session(r: dict, parsed: dict) -> None:
    """Stamp a fresh session with its course and start time."""
    started = now()
    r["session_course"]  = parsed.get("course", "") or r.get("session_course", "")
    r["session_started"] = started.strftime("%Y-%m-%d %H:%M:%S")
    r["round_start_time"] = started.strftime("%Y-%m-%d %H:%M")
    r["course_name"]     = r["session_course"]
    log.info("◉  New session — course=%r  started %s",
             r["session_course"] or "(unknown)", r["session_started"])

def _progression_check(parsed: dict, last: dict | None) -> tuple[bool, str]:
    """
    Decide whether the game state has actually ADVANCED since the last accepted
    capture.  Returns (ok, reason_if_not_ok).

    This is a different question from _capture_is_valid.  That function asks
    "is this a plausible golf screen?" — which a replay, a camera flyby, or a
    scoreboard overlay all answer yes to, because the hole, par and yardage on
    screen are perfectly legitimate.  The giveaway is that nothing has moved:
    the stroke counter has not advanced and the ball is where it was.

    Progression is any ONE of:
      * the hole number changed                    → new hole
      * the stroke ticker advanced (same hole)     → a shot was played
      * the round score changed                    → a hole was completed
      * the ball moved materially (same hole)      → fallback when the ticker
                                                     could not be read

    Two patterns are rejected outright:
      * nothing changed at all                     → replay / flyby / paused
      * ticker went BACKWARD on the same hole      → replay of an earlier shot
    """
    if not last:
        return True, ""          # first capture of the session: nothing to compare

    hole      = parsed.get("hole", 0)
    ticker    = parsed.get("hole_score_ticker", 0) or 0
    remaining = parsed.get("remaining", 0.0) or 0.0
    score     = parsed.get("round_score")

    l_hole    = last.get("hole", 0)
    l_ticker  = last.get("ticker", 0) or 0
    l_remain  = last.get("remaining", 0.0) or 0.0
    l_score   = last.get("round_score")

    # New hole — always progression, and the ticker legitimately resets to 1
    # A different course is always progression — its hole numbers start again,
    # and "hole 1, stroke 1" after "hole 1, stroke 4" is not a replay.
    course, l_course = (parsing._normalize_course(parsed.get("course", "")),
                        parsing._normalize_course(last.get("course", "")))
    if course and l_course and course != l_course:
        return True, ""

    if hole != l_hole:
        return True, ""

    # The previous capture finished this hole. A changed stroke count or ball
    # position is the next attempt (distance-challenge stations repeat a hole
    # number); an identical screen is the result screen captured again.
    if last.get("hole_over"):
        if (ticker and l_ticker and ticker != l_ticker) or abs(remaining - l_remain) > 1.0:
            return True, ""
        return False, f"hole {hole} already complete — result screen captured again"

    # Same hole from here on.
    # A ticker that moves backward means GSPro is re-showing an earlier shot.
    if ticker and l_ticker and ticker < l_ticker:
        return False, (f"stroke ticker went backward on hole {hole} "
                       f"({l_ticker} → {ticker}) — replay of an earlier shot")

    if ticker and l_ticker and ticker > l_ticker:
        return True, ""          # a stroke was played

    # Round score changing means a hole completed even if the ticker is stale
    if score is not None and l_score is not None and score != l_score:
        return True, ""

    # Ticker unreadable or unchanged — fall back to whether the ball moved.
    # 1.0 yd of tolerance absorbs OCR jitter on the remaining-distance field
    # without masking a real shot (even a tap-in changes distance by more).
    if abs(remaining - l_remain) > 1.0:
        if ticker and l_ticker and ticker == l_ticker:
            return False, (f"ball moved but stroke ticker is unchanged at "
                           f"{ticker} on hole {hole} — replay or camera move")
        return True, ""

    return False, (f"no progression — hole {hole}, stroke {ticker or '?'}, "
                   f"{sg_mod.format_distance(remaining, parsed.get('lie', ''))} "
                   f"remaining are all unchanged")

def _capture_fingerprint(parsed: dict) -> dict:
    """Snapshot of the fields _progression_check compares against next time."""
    d, t = _now_parts()
    return {
        "hole":        parsed.get("hole", 0),
        "ticker":      parsed.get("hole_score_ticker", 0) or 0,
        "remaining":   round(parsed.get("remaining", 0.0) or 0.0, 1),
        "round_score": parsed.get("round_score"),
        "date":        d,
        "time":        t,
    }


# ── Configuration ──────────────────────────────────────────────────────────────

def configure(config: dict) -> None:
    """
    Apply the scoring settings from config.json: gimmie radius and capture
    validation bounds. The daemon and the rebuild tool both call this, so a
    replay is always scored under the same rules as live play.
    """
    global GIMMIE_ZONE_YD, MAX_RECONCILE_STROKES, RECOVER_FINAL_STROKES
    RECOVER_FINAL_STROKES = bool(config.get("recover_final_strokes", True))
    # GSPro sets the gimmie radius in FEET, so that is the preferred key;
    # gimmie_zone_yd is honoured for configs written before v2.6.
    if "gimmie_zone_ft" in config:
        GIMMIE_ZONE_YD = float(config["gimmie_zone_ft"]) / sg_mod.FT_PER_YD
    elif "gimmie_zone_yd" in config:
        GIMMIE_ZONE_YD = float(config["gimmie_zone_yd"])
    parsing._VALIDATION.update(config.get("validation", {}) or {})
    if "max_reconcile_strokes" in config:
        MAX_RECONCILE_STROKES = int(config["max_reconcile_strokes"])


# ── One capture, end to end ────────────────────────────────────────────────────

def handle_capture(state: dict, by_cell: dict[str, str], by_name: dict[str, str],
                   sink, config: dict, *, forced: bool = False,
                   dry_run: bool = False) -> tuple[dict, str, str]:
    """
    Decide what one capture means and apply it. Returns (state, outcome, reason).

    outcome
      "invalid"         not a plausible golf screen (menu, loading); nothing done
      "no_progression"  nothing advanced since the last capture (replay, camera
                        move); nothing done. The caller decides whether to offer
                        a forced capture.
      "accepted"        raw row written, session boundaries applied, shot scored

    The caller persists the returned state. Order matters and matches the live
    daemon exactly: validate → progression → raw row → session boundary →
    score → fingerprint.
    """
    parsed = parsing.parse_ocr_fields(by_cell, by_name)
    if parsed.get("end_of_round_strokes") is not None:
        return _record_end_of_round(state, parsed), "end_of_round", ""
    ok, reason = parsing._capture_is_valid(parsed)
    if ok and parsing.COURSE_BOOK is not None and parsed.get("course_raw"):
        parsing.COURSE_BOOK.observe(parsed["course_raw"])
    if not ok:
        q = _quality(state); q["captures"] += 1; q["invalid"] += 1
        log.warning("  ✗  Capture rejected: %s", reason)
        log.warning("     Nothing written. GSPro may be on a menu or "
                    "loading screen.")
        return state, "invalid", reason

    prog_ok, prog_reason = _progression_check(parsed, state.get("last_capture"))
    if not prog_ok and not forced:
        q = _quality(state); q["captures"] += 1; q["no_progression"] += 1
        return state, "no_progression", prog_reason
    if forced:
        log.warning("  ⚠️  Forced capture — progression check overridden")

    # Raw row: every region's text in its column, then the capture timestamp.
    offset = state.get("row_offset", 0)
    cap_date, cap_time = _now_parts()
    if not dry_run:
        cols = []
        for addr in by_cell:
            try:
                cols.append(parsing.parse_cell(addr)[1])
            except Exception:
                continue
        max_col = max(cols, default=0)
        row_vals = [""] * (max_col + 3)
        for addr, txt in by_cell.items():
            try:
                row_vals[parsing.parse_cell(addr)[1]] = txt
            except Exception:
                continue
        row_vals[max_col + 1] = cap_date
        row_vals[max_col + 2] = cap_time
        try:
            sink.write_row("Sheet1", offset, row_vals)
        except Exception as exc:
            log.error("  Could not write raw row: %s", exc)
    log.info("  captured at %s %s", cap_date, cap_time)
    log.info("  parsed → hole=%s  par=%s  yardage=%.0f yd  remaining=%s  lie=%s",
             parsed["hole"], parsed["par"], parsed["hole_yardage"],
             sg_mod.format_distance(parsed["remaining"], parsed["lie"]),
             parsed["lie"])

    # Session boundary — detected BEFORE the shot is scored, so the first
    # capture of a new course or sitting opens a clean session.
    if not dry_run and parsed["hole"] > 0:
        r = state["round"]
        gap = float(config.get("session_gap_minutes", 90))
        reason = _session_boundary(r, parsed, gap)
        if reason:
            _close_session(state, sink, reason)
            r = state["round"]
        if not r.get("session_started"):
            _open_session(r, parsed)
        r["last_capture_ts"] = now().strftime("%Y-%m-%d %H:%M:%S")

    # Counted after any session boundary, so the capture belongs to the
    # session it opened.
    q = _quality(state); q["captures"] += 1; q["accepted"] += 1
    _record_field_health(q, parsed, by_cell)

    if parsed["hole"] > 0:
        # A round score that keeps failing costs holes: warn while it happens (T-35).
        rr = state["round"]
        if parsed.get("round_score") is None:
            rr["score_bad_streak"] = rr.get("score_bad_streak", 0) + 1
            if rr["score_bad_streak"] == SCORE_WARN_AFTER:
                log.warning("  ⚠️  Round score not readable on the last %d captures (read %r) "
                            "— holes may be lost until it reads again. If it persists, "
                            "check the round-score region (U1) with --check-regions.",
                            SCORE_WARN_AFTER, by_cell.get("U1", ""))
        else:
            rr["score_bad_streak"] = 0
        state = process_shot(parsed, state, sink, dry_run=dry_run)
        state["round"]["last_panel"] = _panel(parsed)    # see _plan_final_stroke
    else:
        log.info("  Hole number not detected — SG skipped this capture")

    if not dry_run:
        state["last_capture"] = _capture_fingerprint(parsed)
        state["last_capture"]["hole_over"] = state["round"].get("hole_over")
        state["last_capture"]["course"]    = parsed.get("course", "")
        state["row_offset"]   = offset + 1
    return state, "accepted", ""


# ── In-memory sink ─────────────────────────────────────────────────────────────

class MemorySink:
    """
    Collects everything the engine writes, in memory. Used by the rebuild tool
    (which turns it into a report at the end) and by tests.
    """

    def __init__(self, history: list | None = None) -> None:
        self.sheets: dict[str, list[list[str]]] = {}
        self.history: list[dict] = list(history or [])
        self.summary_updates = 0

    def write_row(self, sheet: str, row: int, values: list) -> bool:
        rows = self.sheets.setdefault(sheet, [])
        while len(rows) <= row:
            rows.append([])
        rows[row] = [str(v) if v is not None else "" for v in values]
        return True

    def truncate(self, sheet: str, keep_rows: int) -> int:
        rows = self.sheets.get(sheet)
        if not rows or len(rows) <= keep_rows:
            return 0
        removed = len(rows) - keep_rows
        self.sheets[sheet] = rows[:keep_rows]
        return removed

    def summary_changed(self, round_state: dict) -> None:
        self.summary_updates += 1

    def archive_session(self, round_state: dict) -> list:
        entry = build_session_entry(round_state)
        if entry is not None:
            self.history = merge_session(self.history, entry)
        return self.history
