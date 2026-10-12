#!/usr/bin/env python3
"""
reports.py — Build the summary sheets (SG_Summary and Round Stats).

Pure functions: given the round state and the session history, they return the
rows of each sheet. They do not read or write files, so the live daemon and
rebuild_from_log.py render identical summaries from identical data.

  build_sg_summary(r, history, version)   → SG_Summary rows
  build_round_stats(r, rounds, version)   → Round Stats rows
"""

from __future__ import annotations

import math

import logging
import statistics
from datetime import datetime, timedelta

import strokes_gained as sg_mod
from scoring import DATE_FMT, _iso_week_label, _now_parts

log = logging.getLogger(__name__)


def _week_date_range(label: str) -> str:
    """Convert '2026-W36' to a readable 'Mon 31 Aug – Sun 06 Sep' range."""
    try:
        year, week = label.split("-W")
        monday = datetime.fromisocalendar(int(year), int(week), 1)
        sunday = datetime.fromisocalendar(int(year), int(week), 7)
        return f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b')}"
    except (ValueError, TypeError):
        return ""

_SG_SUMMARY_LABELS = [
    ("SG: Off Tee",      sg_mod.CAT_OTT),
    ("SG: Approach",     sg_mod.CAT_APP),
    ("SG: Around Green", sg_mod.CAT_ATG),
    ("SG: Putting",      sg_mod.CAT_PUTT),
]

# ── Capture coverage beside strokes-gained figures (T-18, v3.8.12) ─────────────
# Strokes inferred from the round score count in SG Total (as Unattributed) but
# in none of the four categories, so every SG figure says how much of the play
# behind it was actually captured.
COVERAGE_LOW = 0.80

COVERAGE_NOTE = ("Coverage = share of strokes recorded from a capture (including a hole's final "
                 "stroke recovered from the next tee frame) or known for certain (a conceded "
                 "gimmie). The rest were inferred from the round score: they count in SG Total, "
                 "under Unattributed, but in none of the four categories — so at \"low\" "
                 f"coverage (under {COVERAGE_LOW:.0%}) the category figures leave out a fifth or "
                 "more of the play.")


def quality_counts(q: dict | None) -> tuple[int, int] | None:
    """(strokes known, strokes in all) from a session's capture-quality counters;
    None for a session recorded before the counters existed (v3.1)."""
    if not q:
        return None
    known = (q.get("strokes_captured", 0) + q.get("strokes_recovered", 0)
             + q.get("strokes_conceded", 0))
    return known, known + q.get("strokes_uncaptured", 0)


def coverage_counts(sessions: list[dict]) -> tuple[int, int, int]:
    """(strokes known, strokes in all, sessions with no counters) over sessions."""
    known = total = missing = 0
    for s in sessions:
        c = quality_counts(s.get("quality"))
        if c is None:
            missing += 1
            continue
        known, total = known + c[0], total + c[1]
    return known, total, missing


def coverage_label(known: int, total: int) -> str:
    """'100%', '87%', '43% (low)', or '—' when there are no strokes. Never rounds
    up to 100% unless every stroke was captured."""
    if not total:
        return "—"
    pct = 100 if known >= total else min(99, int(known * 100 / total))
    return f"{pct}%" + (" (low)" if known / total < COVERAGE_LOW else "")


def _coverage_of(sessions: list[dict]) -> str:
    known, total, _ = coverage_counts(sessions)
    return coverage_label(known, total)


def _coverage_note_rows(sessions: list[dict]) -> list[list[str]]:
    rows = [[COVERAGE_NOTE]]
    missing = coverage_counts(sessions)[2]
    if missing:
        rows.append([f"{missing} session(s) from before v3.1 recorded no coverage and are "
                     "left out of the coverage figures."])
    return rows


def _aggregate_weeks(rounds: list[dict]) -> dict[str, dict]:
    """
    Group archived rounds into ISO weeks and total each week's figures.

    Returns {week_label: {...aggregates...}} sorted chronologically by key.
    """
    weeks: dict[str, dict] = {}
    for rd in rounds:
        wk = rd.get("week") or ""
        if not wk:
            # Entries archived before the week field existed still carry a date.
            # Deriving the week here rather than skipping is what keeps older
            # sessions visible in the week-by-week view.
            try:
                wk = _iso_week_label(datetime.strptime(rd.get("date", ""), DATE_FMT))
            except (ValueError, TypeError):
                continue
        w = weeks.setdefault(wk, {
            "rounds": 0, "holes": 0, "shots": 0, "par": 0, "vs_par": 0,
            "pro_baseline": 0.0, "gimmies": 0,
            "sg": {c: 0.0 for c in sg_mod.SG_CATS},
            "sg_total": 0.0, "courses": set(), "minutes": 0,
            "sessions": [],
        })
        w["sessions"].append(rd)
        w["rounds"]       += 1
        w["holes"]        += rd.get("holes", 0)
        w["shots"]        += rd.get("shots", 0)
        w["par"]          += rd.get("par", 0)
        w["vs_par"]       += rd.get("vs_par", 0)
        w["pro_baseline"] += rd.get("pro_baseline", 0.0)
        w["gimmies"]      += rd.get("gimmies", 0)
        w["sg_total"]     += rd.get("sg_total", 0.0)
        w["minutes"]      += rd.get("duration_min", 0)
        if rd.get("course"):
            w["courses"].add(rd["course"])
        for c in sg_mod.SG_CATS:
            w["sg"][c] += rd.get("sg", {}).get(c, 0.0)
    return dict(sorted(weeks.items()))

def _delta_cell(curr: float, prev: float | None, decimals: int = 3) -> str:
    """Format a week-over-week change, or '—' when there is no prior week."""
    if prev is None:
        return "—"
    return f"{curr - prev:+.{decimals}f}"

def build_sg_summary(r: dict, history: list, version: str) -> list[list[str]]:
    """
    Build the SG_Summary sheet's rows from the round state and session history.

    Two sections:
      1. CURRENT ROUND  — live totals for the round in progress.
      2. WEEK BY WEEK   — one row per ISO week from round_history.json, with
                          a Δ column showing the change from the previous week
                          so improvement or decline is visible at a glance.

    The weekly section only appears once at least one round has been archived
    (rounds are archived when the daemon exits).
    """
    gdate, gtime = _now_parts()
    sg_r  = r["sg_round"]
    total = round(sum(sg_r.values()), 3)

    pro_baseline = r.get("pro_baseline_round", 0.0)
    player_shots = r.get("completed_shots",    0)
    holes_done   = r.get("holes_completed",    0)
    vs_pro   = round(player_shots - pro_baseline, 2) if pro_baseline else None
    per_shot = round(total / r.get("shots_total", 1), 3) if r.get("shots_total") else None

    def _vs_label(diff: float | None) -> str:
        if diff is None:
            return "—"
        direction = "WORSE" if diff > 0 else "BETTER" if diff < 0 else "EVEN"
        return f"{abs(diff):.2f} strokes {direction} than PGA Tour average"

    rows: list[list[str]] = []

    # ── Section 1: current round ──────────────────────────────────────────────
    rows.append(["SG SUMMARY", f"Updated {gdate} {gtime}", version])
    rows.append(["", "", ""])
    rows.append(["CURRENT ROUND", "Value", "Interpretation"])
    for label, cat in _SG_SUMMARY_LABELS:
        val = sg_r.get(cat, 0.0)
        rows.append([label, f"{val:+.3f}", sg_mod.explain_sg(val, cat)])
    rows.append(["SG: Total", f"{total:+.3f}", sg_mod.explain_sg(total)])
    _q = quality_counts(r.get("quality"))
    if _q:
        rows.append(["Capture coverage", coverage_label(*_q),
                     f"{_q[0]} of {_q[1]} strokes captured; {_q[1] - _q[0]} inferred from the "
                     "round score (in Unattributed, not in the categories)"])
    rows.append(["", "", ""])
    rows.append(["Holes Played (basis for all comparisons)",
                 str(holes_done), _sample_quality(holes_done)[1]])
    rows.append(["Pro Baseline (these holes only)",
                 f"{pro_baseline:.2f}" if pro_baseline else "—",
                 f"Tour player's expected strokes for the same {holes_done} hole(s)"])
    rows.append(["Your Strokes (completed holes)", str(player_shots),
                 f"{player_shots/holes_done:.2f} per hole" if holes_done else ""])
    rows.append(["You vs PGA Tour Average",
                 f"{vs_pro:+.2f}" if vs_pro is not None else "—",
                 _vs_label(vs_pro)])
    rows.append(["  … per hole played",
                 f"{vs_pro/holes_done:+.3f}" if (vs_pro is not None and holes_done) else "—",
                 "Comparable across sessions of different length"])
    _p18_lbl = "  … average per 18 holes" if holes_done >= 18 else "  … projected over 18 holes"
    _p18_note = ("Average across holes actually played"
                 if holes_done >= 18 else
                 "Linear extrapolation — scales the noise too")
    rows.append([_p18_lbl,
                 f"{vs_pro/holes_done*18:+.2f}" if (vs_pro is not None and holes_done) else "—",
                 _p18_note])
    rows.append(["  … round equivalents",
                 f"{holes_done/18:.2f}" if holes_done else "—",
                 "Session length in 18-hole units"])
    rows.append(["Per-Shot SG Average",
                 f"{per_shot:+.3f}" if per_shot is not None else "—",
                 "Average strokes gained per shot vs PGA Tour"])
    rows.append(["Holes Completed",   str(holes_done), ""])
    rows.append(["All Shots (round)", str(r.get("shots_total", 0)),
                 "Includes shots on any active incomplete hole"])

    # ── Section 2: week by week ───────────────────────────────────────────────
    weeks = _aggregate_weeks(history)
    rows.append(["", "", ""])
    rows.append(["WEEK BY WEEK", "", ""])

    if not weeks:
        rows.append(["No completed rounds archived yet", "", 
                     "Weekly stats appear after the first round finishes"])
    else:
        rows.append([
            "Week", "Dates", "Rounds", "Holes", "Shots",
            "Score vs Par", "vs Par/Hole",
            "vs PGA Tour", "vs PGA Tour/Hole", "Sample", "Coverage",
            f"{sg_mod.CAT_OTT}/hole",  f"Δ {sg_mod.CAT_OTT}",
            f"{sg_mod.CAT_APP}/hole",  f"Δ {sg_mod.CAT_APP}",
            f"{sg_mod.CAT_ATG}/hole",  f"Δ {sg_mod.CAT_ATG}",
            f"{sg_mod.CAT_PUTT}/hole", f"Δ {sg_mod.CAT_PUTT}",
            f"{sg_mod.CAT_UNATTRIBUTED}/hole", f"Δ {sg_mod.CAT_UNATTRIBUTED}",
            "SG/Hole", "Δ SG/Hole", "SG Total (raw)", "Courses", "Time (min)",
        ])

        prev: dict | None = None
        for label, w in weeks.items():
            # Normalise by HOLES PLAYED, not by rounds. A week of three
            # 3-hole sessions and a week of two 18-hole rounds are only
            # comparable per hole; dividing by rounds would make a short
            # session look like a full round's result.
            holes    = w["holes"] or 1
            sg_hole  = w["sg_total"] / holes
            vs_pgat  = w["shots"] - w["pro_baseline"]
            prev_sg  = prev["sg"] if prev else None
            row = [
                label,
                _week_date_range(label),
                str(w["rounds"]),
                str(w["holes"]),
                str(w["shots"]),
                f"{w['vs_par']:+d}",
                f"{w['vs_par']/holes:+.3f}",
                f"{vs_pgat:+.2f}",
                f"{vs_pgat/holes:+.3f}",
                _sample_quality(w["holes"])[0],
                _coverage_of(w["sessions"]),
            ]
            # Deltas are computed on PER-HOLE rates, so an improvement is a
            # real rate change rather than an artefact of playing more holes.
            prev_holes = (prev["holes"] or 1) if prev else None
            for cat in sg_mod.SG_CATS:
                curr_rate = w["sg"][cat] / holes
                prev_rate = (prev_sg[cat] / prev_holes) if prev_sg else None
                row.append(f"{curr_rate:+.3f}")
                row.append(_delta_cell(curr_rate, prev_rate))
            row.append(f"{sg_hole:+.3f}")
            row.append(_delta_cell(
                sg_hole,
                (prev["sg_total"] / prev_holes) if prev else None))
            row.append(f"{w['sg_total']:+.3f}")
            row.append(", ".join(sorted(w["courses"])) or "—")
            row.append(str(w["minutes"]))
            rows.append(row)
            prev = w
        rows.extend(_coverage_note_rows(history))

        # Trend note comparing the two most recent weeks
        if len(weeks) >= 2:
            labels = list(weeks)
            last, prior = weeks[labels[-1]], weeks[labels[-2]]
            lh = last["holes"] or 1
            ph = prior["holes"] or 1
            # Rate comparison: playing more holes must not read as improvement.
            delta = (last["sg_total"] / lh) - (prior["sg_total"] / ph)
            best_cat, best_delta = None, 0.0
            worst_cat, worst_delta = None, 0.0
            for cat in sg_mod.SG_CATS:
                d = (last["sg"][cat] / lh) - (prior["sg"][cat] / ph)
                if d > best_delta:
                    best_delta, best_cat = d, cat
                if d < worst_delta:
                    worst_delta, worst_cat = d, cat
            rows.append(["", "", ""])
            rows.append([
                f"{labels[-1]} vs {labels[-2]}  (per hole)",
                f"{delta:+.3f}",
                (f"SG/hole improvement — {lh} vs {ph} holes played"
                 if delta > 0 else
                 f"SG/hole decline — {lh} vs {ph} holes played"
                 if delta < 0 else "No change"),
            ])
            if best_cat:
                rows.append(["  Most improved", f"{best_delta:+.3f}", best_cat])
            if worst_cat:
                rows.append(["  Biggest decline", f"{worst_delta:+.3f}", worst_cat])

    return rows

def _geometry_rows(geom_stats: dict, heading: str) -> list[list[str]]:
    """
    Render the per-club short/long table, each row in its own unit.

    Putting is shown in FEET and every club in yards. Values are stored in
    canonical yards, so the conversion happens here, at presentation. The
    tendency thresholds are scaled per unit as well: "3 short" means 3 yards
    for a wedge but 3 FEET for a putt — applying the yard threshold to putts
    would call a putter that leaves everything 8 ft short "well calibrated".
    """
    if not geom_stats:
        return []
    rows = [[heading, "Shots", "Short", "Long", "Pin High",
             "Mean Bias", "Mean Miss", "Mean Offline", "Unit", "Tendency"]]
    # Putting first, then clubs by usage
    order = sorted(geom_stats.items(),
                   key=lambda kv: (kv[1].get("unit") != "ft", -kv[1]["n"]))
    for club, e in order:
        n    = e["n"] or 1
        unit = e.get("unit", "yd")
        k    = sg_mod.FT_PER_YD if unit == "ft" else 1.0
        bias = e["delta_sum"] / n * k
        miss = e["abs_delta_sum"] / n * k
        off  = e["lateral_sum"] / n * k
        # thresholds in the row's own unit
        strong, mild = (1.5, 0.5) if unit == "ft" else (3.0, 1.0)
        tend = ("consistently short" if bias < -strong else
                "consistently long"  if bias >  strong else
                "slightly short"     if bias < -mild   else
                "slightly long"      if bias >  mild   else "well calibrated")
        rows.append([club, str(e["n"]), str(e["short"]), str(e["long"]),
                     str(e["pinhigh"]), f"{bias:+.1f}", f"{miss:.1f}",
                     f"{off:.1f}", unit, tend])
    return rows

def _sample_quality(holes: int) -> tuple[str, str]:
    """
    Describe how much weight a strokes-gained figure from *holes* holes carries.

    Strokes gained is a noisy per-shot measure: a single chunked approach or
    holed 30-footer moves a short sample a long way.  Reporting a 3-hole SG
    number beside a 36-hole one without saying which is which invites reading
    noise as signal, so every normalised block carries one of these labels.

    Bands are expressed in holes, not rounds, because a session can be any
    length — 3 holes, 27, 45 — and the reliability of the numbers depends on
    how many holes were actually played, nothing else.

    Returns (label, guidance).
    """
    if holes <= 0:
        return "no data", "No completed holes."
    rounds = holes / 18.0
    if holes < 3:
        return "very thin", "One or two holes — treat as anecdote, not measurement."
    if holes < 9:
        return "indicative", f"{holes} holes — directionally interesting, not conclusive."
    if holes < 18:
        return "partial round", (f"{holes} holes ({rounds:.1f} rounds) — "
                                 "totals are real but category splits stay noisy.")
    if holes < 27:
        return "one round", (f"{holes} holes ({rounds:.1f} rounds) — totals "
                             "meaningful, category splits still move.")
    if holes < 54:
        return "extended session", (f"{holes} holes ({rounds:.1f} rounds) — "
                                    "more than a single round; totals carry real weight.")
    if holes < 180:
        return "moderate", (f"{holes} holes ({rounds:.1f} rounds) — "
                            "category trends becoming reliable.")
    return "solid", (f"{holes} holes ({rounds:.1f} rounds) — "
                     "per-hole rates are dependable.")

def _normalized_stats(
    holes: int,
    shots: int,
    par: int,
    baseline: float,
    sg: dict,
) -> dict:
    """
    Express every comparative figure per hole actually played.

    The pro baseline is already accumulated one hole at a time, so a 3-hole
    session is compared against a PGA Tour player's expected score for those
    same 3 holes — never against a notional 9- or 18-hole round.  This function
    takes that correct total and derives the per-hole rates, which are the only
    form in which sessions of different lengths can be compared to each other.

    The 18-hole projection is a straight linear scaling of the per-hole rate.
    It is included because it is the number most golfers think in, but it is an
    extrapolation, not a result, and is labelled as such wherever it appears.
    """
    h = holes if holes > 0 else 0
    d = float(h) if h else 1.0
    sg_total = round(sum(sg.values()), 3)
    vs_pro   = round(shots - baseline, 3)
    vs_par   = shots - par
    label, guidance = _sample_quality(h)

    return {
        "holes":            h,
        "shots":            shots,
        "par":              par,
        "baseline":         round(baseline, 3),
        "vs_par":           vs_par,
        "vs_pro":           vs_pro,
        "sg_total":         sg_total,
        # ── per hole played — the comparable forms ──────────────────────────
        "shots_per_hole":    round(shots / d, 3)     if h else 0.0,
        "par_per_hole":      round(par / d, 3)       if h else 0.0,
        "baseline_per_hole": round(baseline / d, 3)  if h else 0.0,
        "vs_par_per_hole":   round(vs_par / d, 3)    if h else 0.0,
        "vs_pro_per_hole":   round(vs_pro / d, 3)    if h else 0.0,
        "sg_total_per_hole": round(sg_total / d, 3)  if h else 0.0,
        "sg_per_hole":       {c: round(v / d, 3) for c, v in sg.items()} if h
                             else {c: 0.0 for c in sg},
        # ── per 18 holes ────────────────────────────────────────────────────
        # Below 18 holes this is an EXTRAPOLATION (scaling a short sample up,
        # which scales its noise up too).  At or above 18 holes it is an
        # AVERAGE of holes actually played — a real measurement.  The two are
        # arithmetically identical and epistemically very different, so the
        # semantics are reported alongside the numbers rather than assumed.
        "round_equivalents": round(h / 18.0, 2) if h else 0.0,
        "per18_is_average":  h >= 18,
        "per18_label":       "Avg per 18" if h >= 18 else "Projected 18",
        "per18_note":        ("Average across holes actually played"
                              if h >= 18 else
                              "Linear extrapolation — scales the noise too"),
        "per18_vs_par":   round(vs_par / d * 18, 2) if h else 0.0,
        "per18_vs_pro":   round(vs_pro / d * 18, 2) if h else 0.0,
        "per18_sg":       round(sg_total / d * 18, 2) if h else 0.0,
        "per18_shots":    round(shots / d * 18, 1)  if h else 0.0,
        "sample_label":    label,
        "sample_guidance": guidance,
    }

def _merge_band_stats(target: dict, source: dict) -> dict:
    """Accumulate one round's band_stats into a running total."""
    for cat, bands in (source or {}).items():
        t = target.setdefault(cat, {})
        for band, e in (bands or {}).items():
            a = t.setdefault(band, {"n": 0, "sg": 0.0, "holed": 0,
                                    "prox_sum": 0.0, "prox_n": 0})
            a["n"]        += e.get("n", 0)
            a["sg"]        = round(a["sg"] + e.get("sg", 0.0), 3)
            a["holed"]    += e.get("holed", 0)
            a["prox_sum"]  = round(a["prox_sum"] + e.get("prox_sum", 0.0), 2)
            a["prox_n"]   += e.get("prox_n", 0)
    return target

def _band_rows(band_stats: dict, holes: int, heading: str) -> list[list[str]]:
    """
    Render a distance-band breakdown.

    Bands and their order come from strokes_gained.band_order(), which derives
    them from the baseline table knots — putting in feet at the green table's
    resolution, approach/tee in the 20-yd source-table steps, around-the-green
    at the fringe table's finer spacing. Empty bands are omitted.

    Proximity is the mean resulting distance to the hole for shots that did not
    hole out; it is the standard approach measure and is reported in the same
    unit as the band. Make % is only meaningful for putting and around-green.
    """
    rows: list[list[str]] = []
    if not band_stats:
        return rows

    rows.append([heading, "Shots", "SG Total", "SG/Shot",
                 "Per Hole", "Holed", "Make %", "Avg Proximity"])

    for cat in sg_mod.ALL_CATS:
        bands = band_stats.get(cat) or {}
        if not bands:
            continue
        rows.append([f"— {cat} —", "", "", "", "", "", "", ""])


        c_n, c_sg, c_holed = 0, 0.0, 0
        for band in sg_mod.band_order(cat):
            e = bands.get(band)
            if not e or e.get("n", 0) == 0:
                continue
            n, s, hd = e["n"], e["sg"], e.get("holed", 0)
            pn, ps   = e.get("prox_n", 0), e.get("prox_sum", 0.0)
            prox     = (ps / pn) if pn else None
            # Proximity for putting is stored in yards; show it in feet so it
            # matches the band unit the reader is looking at.
            prox_disp = ("—" if prox is None else
                         f"{prox * sg_mod.FT_PER_YD:.1f} ft" if cat == sg_mod.CAT_PUTT
                         else f"{prox:.1f} yd")
            rows.append([
                f"  {band}", str(n), f"{s:+.3f}", f"{s/n:+.3f}",
                f"{s/holes:+.3f}" if holes else "—",
                str(hd) if hd else "",
                f"{hd/n*100:.0f}%" if hd else "",
                prox_disp,
            ])
            c_n += n; c_sg += s; c_holed += hd

        if c_n:
            rows.append([
                f"  {cat} — all distances", str(c_n), f"{c_sg:+.3f}",
                f"{c_sg/c_n:+.3f}",
                f"{c_sg/holes:+.3f}" if holes else "—",
                str(c_holed) if c_holed else "",
                f"{c_holed/c_n*100:.0f}%" if c_holed else "", "",
            ])
    return rows

def _lie_penalty_rows(shot_rows: list) -> list[list[str]]:
    """
    Speed and spin kept after shots from each lie, from the SG_Shots rows —
    only when GSPro's LAST SHOT panel was recorded for some shot (T-08).
    """
    if len(shot_rows) < 2:
        return []
    h = shot_rows[0]
    try:
        ilie, isg, isp, isn = (h.index(c) for c in ("Pre-Lie", "SG", "Speed Kept %", "Spin Kept %"))
    except ValueError:
        return []
    by: dict[str, list] = {}
    for row in shot_rows[1:]:
        if len(row) <= max(ilie, isg, isp, isn) or not str(row[isp]).strip():
            continue
        try:
            sp, sg = float(row[isp]), float(row[isg] or 0)
            sn = float(row[isn]) if str(row[isn]).strip() else None
        except ValueError:
            continue
        by.setdefault(row[ilie] or "?", []).append((sp, sn, sg))
    if not by:
        return []
    out = [[""], ["LIE PENALTY — speed and spin kept after shots from each lie (all recorded shots)",
                  "Shots", "Avg Speed Kept %", "Avg Spin Kept %", "Avg SG/Shot"]]
    for lie in sorted(by, key=lambda k: (-len(by[k]), k)):
        v = by[lie]
        spins = [s for _, s, _ in v if s is not None]
        out.append([lie, str(len(v)), f"{sum(p for p, _, _ in v) / len(v):.0f}",
                    f"{sum(spins) / len(spins):.0f}" if spins else "",
                    f"{sum(g for _, _, g in v) / len(v):+.3f}"])
    out.append(["From GSPro's LAST SHOT panel, shown after a shot from a penalising lie. Only "
                "shots that started off the green are counted — the panel stays on screen "
                "through the putts that follow."])
    return out


def build_round_stats(r: dict, rounds: list, version: str,
                      shot_rows: list | None = None) -> list[list[str]]:
    """
    Write the "Round Stats" sheet: ONE ROW PER ROUND across all sessions.

    *rounds* is the full session archive, already including the session just
    finished (the caller archives it). The whole archive is rendered as a table.  This makes the sheet a permanent log —
    every round played, oldest to newest, with a career totals row — instead
    of a snapshot that is overwritten each session.

    A second section breaks the CURRENT round down hole by hole, since that
    detail is only meaningful for the round just finished.
    """
    holes = r.get("holes_completed", 0)

    gdate, gtime = _now_parts()

    # ── Normalised view of the round just finished ────────────────────────────
    # Every comparison is against the PGA Tour baseline for the holes ACTUALLY
    # played. Play 3 holes and you are measured against a tour player's expected
    # score for those same 3 holes — never against a notional 9- or 18-hole round.
    this = _normalized_stats(
        holes    = holes,
        shots    = r.get("completed_shots", 0),
        par      = r.get("completed_par", 0),
        baseline = r.get("pro_baseline_round", 0.0),
        sg       = r.get("sg_round") or {},
    )

    rows: list[list[str]] = []
    rows.append(["SESSION STATISTICS", f"Generated {gdate} {gtime}",
                 f"{len(rounds)} session(s) on file", version])
    rows.append([""])

    rows.append([f"THIS SESSION — {holes} HOLE(S) PLAYED", "Total",
                 "Per Hole", this["per18_label"], "Note"])
    rows.append(["Holes played (basis for everything below)",
                 str(holes),
                 f"{this['round_equivalents']:.2f} rounds", "",
                 this["sample_guidance"]])
    rows.append(["Strokes", str(this["shots"]),
                 f"{this['shots_per_hole']:.3f}",
                 f"{this['per18_shots']:.1f}",
                 this["per18_note"]])
    rows.append(["Par of holes played", str(this["par"]),
                 f"{this['par_per_hole']:.3f}", "", ""])
    rows.append(["Score vs par", f"{this['vs_par']:+d}",
                 f"{this['vs_par_per_hole']:+.3f}",
                 f"{this['per18_vs_par']:+.2f}", ""])
    rows.append(["PGA Tour baseline (these holes)", f"{this['baseline']:.2f}",
                 f"{this['baseline_per_hole']:.3f}", "",
                 f"Tour player's expected strokes for the SAME {holes} hole(s)"])
    rows.append(["You vs PGA Tour", f"{this['vs_pro']:+.2f}",
                 f"{this['vs_pro_per_hole']:+.3f}",
                 f"{this['per18_vs_pro']:+.2f}",
                 "Negative = better than tour average"])
    _q = quality_counts(r.get("quality"))
    if _q:
        rows.append(["Capture coverage", coverage_label(*_q), "", "",
                     f"{_q[0]} of {_q[1]} strokes captured; {_q[1] - _q[0]} inferred from the "
                     "round score — counted in SG total (Unattributed), not in the categories"])
    rows.append(["Strokes gained total", f"{this['sg_total']:+.3f}",
                 f"{this['sg_total_per_hole']:+.3f}",
                 f"{this['per18_sg']:+.2f}", ""])
    for cat in sg_mod.SG_CATS:
        rows.append([f"  SG {cat}",
                     f"{(r.get('sg_round') or {}).get(cat, 0.0):+.3f}",
                     f"{this['sg_per_hole'].get(cat, 0.0):+.3f}", "", ""])
    rows.append([""])

    # ── Section 1: one row per round ──────────────────────────────────────────
    rows.append(["SESSION LOG — one row per sitting, per course",
                 "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                 "", "", "", "", "", ""])
    rows.append([
        "Date", "Start", "End", "Mins", "Week", "Course",
        "Holes", "Par", "Strokes", "vs Par", "vs Par/Hole", "Gimmies",
        "Pro Baseline", "vs PGA Tour", "vs PGA Tour/Hole",
        f"{sg_mod.CAT_OTT}/hole", f"{sg_mod.CAT_APP}/hole",
        f"{sg_mod.CAT_ATG}/hole", f"{sg_mod.CAT_PUTT}/hole",
        f"{sg_mod.CAT_UNATTRIBUTED}/hole",
        "SG/Hole", "Δ SG/Hole vs prev", "Coverage",
    ])

    tot = {"holes": 0, "par": 0, "shots": 0, "vs_par": 0, "gimmies": 0,
           "baseline": 0.0, "sg_total": 0.0,
           "sg": {c: 0.0 for c in sg_mod.SG_CATS}, "mins": 0}

    _prev_rate = [None]     # SG/hole of the previous session, for the Δ column
    for rd in rounds:
        h = rd.get("holes", 0) or 1
        rows.append([
            rd.get("date", ""), rd.get("start_time", ""), rd.get("end_time", ""),
            str(rd.get("duration_min", 0)), rd.get("week", ""),
            rd.get("course", "") or "—",
            str(rd.get("holes", 0)), str(rd.get("par", 0)), str(rd.get("shots", 0)),
            f"{rd.get('vs_par', 0):+d}",
            f"{rd.get('vs_par', 0)/h:+.3f}",
            str(rd.get("gimmies", 0)),
            f"{rd.get('pro_baseline', 0.0):.2f}",
            f"{rd.get('vs_pro', 0.0):+.2f}",
            f"{rd.get('vs_pro', 0.0)/h:+.3f}",
            f"{rd.get('sg', {}).get(sg_mod.CAT_OTT,  0.0)/h:+.3f}",
            f"{rd.get('sg', {}).get(sg_mod.CAT_APP,  0.0)/h:+.3f}",
            f"{rd.get('sg', {}).get(sg_mod.CAT_ATG,  0.0)/h:+.3f}",
            f"{rd.get('sg', {}).get(sg_mod.CAT_PUTT, 0.0)/h:+.3f}",
            f"{rd.get('sg', {}).get(sg_mod.CAT_UNATTRIBUTED, 0.0)/h:+.3f}",
            f"{rd.get('sg_total', 0.0)/h:+.3f}",
            (f"{rd.get('sg_total', 0.0)/h - _prev_rate[0]:+.3f}"
             if _prev_rate[0] is not None else "—"),
            _coverage_of([rd]),
        ])
        _prev_rate[0] = rd.get('sg_total', 0.0) / h
        tot["holes"]    += rd.get("holes", 0)
        tot["par"]      += rd.get("par", 0)
        tot["shots"]    += rd.get("shots", 0)
        tot["vs_par"]   += rd.get("vs_par", 0)
        tot["gimmies"]  += rd.get("gimmies", 0)
        tot["baseline"] += rd.get("pro_baseline", 0.0)
        tot["sg_total"] += rd.get("sg_total", 0.0)
        tot["mins"]     += rd.get("duration_min", 0)
        for c in sg_mod.SG_CATS:
            tot["sg"][c] += rd.get("sg", {}).get(c, 0.0)

    th = tot["holes"] or 1
    rows.append([
        "CAREER TOTAL", "", "", str(tot["mins"]), f"{len(rounds)} rounds", "",
        str(tot["holes"]), str(tot["par"]), str(tot["shots"]),
        f"{tot['vs_par']:+d}",
        f"{tot['vs_par']/th:+.3f}",
        str(tot["gimmies"]),
        f"{tot['baseline']:.2f}",
        f"{tot['shots'] - tot['baseline']:+.2f}",
        f"{(tot['shots'] - tot['baseline'])/th:+.3f}",
        f"{tot['sg'][sg_mod.CAT_OTT]/th:+.3f}",
        f"{tot['sg'][sg_mod.CAT_APP]/th:+.3f}",
        f"{tot['sg'][sg_mod.CAT_ATG]/th:+.3f}",
        f"{tot['sg'][sg_mod.CAT_PUTT]/th:+.3f}",
        f"{tot['sg'][sg_mod.CAT_UNATTRIBUTED]/th:+.3f}",
        f"{tot['sg_total']/th:+.3f}", "", _coverage_of(rounds),
    ])

    # ── Section 1b: by course ─────────────────────────────────────────────────
    by_course: dict[str, dict] = {}
    for rd in rounds:
        k = rd.get("course") or "(unknown)"
        c = by_course.setdefault(k, {"sessions": 0, "holes": 0, "shots": 0,
                                     "par": 0, "vs_par": 0, "baseline": 0.0,
                                     "sg": 0.0, "mins": 0, "list": []})
        c["list"].append(rd)
        c["sessions"] += 1
        c["holes"]    += rd.get("holes", 0)
        c["shots"]    += rd.get("shots", 0)
        c["par"]      += rd.get("par", 0)
        c["vs_par"]   += rd.get("vs_par", 0)
        c["baseline"] += rd.get("pro_baseline", 0.0)
        c["sg"]       += rd.get("sg_total", 0.0)
        c["mins"]     += rd.get("duration_min", 0)
    if by_course:
        rows.append([""])
        rows.append(["BY COURSE", "Sessions", "Holes", "Strokes", "vs Par",
                     "vs Par/Hole", "vs PGA Tour", "vs Tour/Hole", "SG/Hole", "Coverage"])
        for name, c in sorted(by_course.items(), key=lambda kv: -kv[1]["holes"]):
            ch = c["holes"] or 1
            vp = c["shots"] - c["baseline"]
            rows.append([name, str(c["sessions"]), str(c["holes"]),
                         str(c["shots"]), f"{c['vs_par']:+d}",
                         f"{c['vs_par']/ch:+.3f}", f"{vp:+.2f}",
                         f"{vp/ch:+.3f}", f"{c['sg']/ch:+.3f}", _coverage_of(c["list"])])

    # ── Section 1c: week by week ──────────────────────────────────────────────
    wk = _aggregate_weeks(rounds)
    if wk:
        rows.append([""])
        rows.append(["WEEK BY WEEK", "Dates", "Sessions", "Holes", "Strokes",
                     "vs Par/Hole", "vs Tour/Hole", "SG/Hole", "Δ SG/Hole",
                     "Coverage", "Courses"])
        _pw = None
        for label, w in wk.items():
            wh   = w["holes"] or 1
            rate = w["sg_total"] / wh
            rows.append([
                label, _week_date_range(label), str(w["rounds"]),
                str(w["holes"]), str(w["shots"]),
                f"{w['vs_par']/wh:+.3f}",
                f"{(w['shots'] - w['pro_baseline'])/wh:+.3f}",
                f"{rate:+.3f}",
                f"{rate - _pw:+.3f}" if _pw is not None else "—",
                _coverage_of(w["sessions"]),
                ", ".join(sorted(w["courses"])) or "—",
            ])
            _pw = rate
    rows.extend(_coverage_note_rows(rounds))

    # ── Section 2: hole-by-hole for the round just finished ───────────────────
    rows.append([""])
    rows.append([f"HOLE BY HOLE — round of {r.get('round_start_time', '')}",
                 r.get("course_name", "") or "—"])
    rows.append(["Date", "Time", "Hole", "Par", "Score", "+/- Par",
                 "Pro Baseline",
                 f"SG {sg_mod.CAT_OTT}", f"SG {sg_mod.CAT_APP}",
                 f"SG {sg_mod.CAT_ATG}", f"SG {sg_mod.CAT_PUTT}", "SG Total", "Captured"])
    for h in r.get("hole_history", []):
        sg_h = h.get("sg_by_cat", {})
        rows.append([
            h.get("date", ""), h.get("time", ""),
            str(h.get("hole", "")), str(h.get("par", "")), str(h.get("shots", "")),
            f"{h.get('diff', 0):+d}", f"{h.get('hole_baseline', 0.0):.2f}",
            f"{sg_h.get(sg_mod.CAT_OTT,  0.0):+.3f}",
            f"{sg_h.get(sg_mod.CAT_APP,  0.0):+.3f}",
            f"{sg_h.get(sg_mod.CAT_ATG,  0.0):+.3f}",
            f"{sg_h.get(sg_mod.CAT_PUTT, 0.0):+.3f}",
            f"{h.get('sg_total', 0.0):+.3f}",
            (f"{h.get('shots', 0) - h['estimated']} of {h.get('shots', 0)}"
             if "estimated" in h else ""),
        ])

    # ── Section 3: shot distribution by lie, current round ────────────────────
    lie_cnt = r.get("shot_counts_by_lie", {})
    sg_lie  = r.get("sg_by_lie", {})
    if lie_cnt:
        rows.append([""])
        _qc = quality_counts(r.get("quality"))
        rows.append([f"SHOT DISTRIBUTION BY LIE — current round (captured shots; coverage "
                     f"{coverage_label(*_qc) if _qc else '—'})",
                     "Shots", "SG Total", "Avg SG/Shot"])
        for lie in (sg_mod.LIE_TEE, sg_mod.LIE_FAIRWAY, sg_mod.LIE_ROUGH,
                    sg_mod.LIE_RECOVERY, sg_mod.LIE_BUNKER,
                    sg_mod.LIE_FRINGE, sg_mod.LIE_GREEN):
            n = lie_cnt.get(lie, 0)
            if n == 0:
                continue
            s = sg_lie.get(lie, 0.0)
            rows.append([lie, str(n), f"{s:+.3f}", f"{s/n:+.3f}"])

    # ── What each lie costs: GSPro's LAST SHOT panel, all recorded shots ──────
    rows.extend(_lie_penalty_rows(shot_rows or []))

    # ── Short / long by club — this session ───────────────────────────────────
    gs = r.get("geom_stats") or {}
    if gs:
        rows.append([""])
        rows.extend(_geometry_rows(gs, "SHORT / LONG BY CLUB — THIS SESSION"))
        rows.append(["Negative bias = finishing short of the pin. Putting is in "
                     "FEET, clubs in yards. Offline is the lateral component, "
                     "separated from distance control by the law of cosines; a big "
                     "offline with small bias means the distance was right and the "
                     "line was not."])

    # ── Short / long by club — all rounds ─────────────────────────────────────
    career_geom: dict = {}
    for rd in rounds:
        for club, e in (rd.get("geom_stats") or {}).items():
            a = career_geom.setdefault(club, {"n": 0, "short": 0, "long": 0,
                                              "pinhigh": 0, "delta_sum": 0.0,
                                              "abs_delta_sum": 0.0,
                                              "lateral_sum": 0.0,
                                              "unit": e.get("unit", "yd")})
            for k, v in a.items():
                if k == "unit":
                    continue
                a[k] = (round(v + e.get(k, 0.0), 3) if isinstance(v, float)
                        else v + e.get(k, 0))
    if career_geom:
        rows.append([""])
        rows.extend(_geometry_rows(career_geom, "SHORT / LONG BY CLUB — ALL ROUNDS"))

    # ── Distance-band breakdown — this session ────────────────────────────────
    band_now = r.get("band_stats") or {}
    if band_now:
        rows.append([""])
        rows.extend(_band_rows(
            band_now, holes,
            f"DISTANCE BANDS — THIS SESSION ({holes} hole(s); coverage "
            f"{coverage_label(*(quality_counts(r.get('quality')) or (0, 0)))})"))

    # ── Distance-band breakdown — all rounds on file ──────────────────────────
    career_bands: dict = {}
    career_holes = 0
    for rd in rounds:
        _merge_band_stats(career_bands, rd.get("band_stats") or {})
        career_holes += rd.get("holes", 0)
    if career_bands:
        rows.append([""])
        rows.extend(_band_rows(
            career_bands, career_holes,
            f"DISTANCE BANDS — ALL ROUNDS ({career_holes} hole(s), "
            f"{len(rounds)} round(s); coverage {_coverage_of(rounds)})"))
        rows.append([""])
        rows.append(["Bands are derived from the strokes-gained baseline tables:",
                     "putting at the green table's foot spacing, approach and tee "
                     "at the 20-yd source-table steps, around-green at the fringe "
                     "table's spacing."])

    # ── Capture quality ───────────────────────────────────────────────────────
    rows.append([""])
    rows.append(["CAPTURE QUALITY — how much of each session was actually captured"])
    rows.append(["Date", "Course", "Captures", "Accepted", "Menu / blank",
                 "Replays skipped", "Strokes captured", "Conceded", "Estimated",
                 "Coverage"])
    for rd in rounds:
        q = rd.get("quality") or {}
        if not q:
            rows.append([rd.get("date", ""), rd.get("course", "") or "—",
                         "not recorded (session before v3.1)"])
            continue
        cap, con, est = (q.get("strokes_captured", 0), q.get("strokes_conceded", 0),
                         q.get("strokes_uncaptured", 0))
        rec = q.get("strokes_recovered", 0)
        cap += rec                    # a recovered final stroke is recorded from a frame too
        tot = cap + con + est
        rows.append([rd.get("date", ""), rd.get("course", "") or "—",
                     str(q.get("captures", 0)), str(q.get("accepted", 0)),
                     str(q.get("invalid", 0)), str(q.get("no_progression", 0)),
                     str(cap), str(con), str(est),
                     f"{(cap + con) / tot * 100:.0f}%" if tot else "—"])
    rows.append(["Coverage = strokes recorded by a capture (including a hole's final stroke "
                 "recovered from the next tee frame, v3.7), or known for certain (a "
                 "conceded gimmie). Estimated strokes are inferred from the round "
                 "score: real strokes, but their strokes-gained values are approximate."])

    fields = (r.get("quality") or {}).get("fields") or {}
    if fields:
        rows.append([""])
        rows.append(["FIELD READ HEALTH — this session, golf screens only",
                     "Reads", "No data (---)", "Blank", "Unreadable", "OK"])
        def with_data(e):
            return e["n"] - e.get("nodata", 0)
        def ok_share(e):
            d = with_data(e)
            return (d - e["blank"] - e["bad"]) / d if d > 0 else 1.0
        for cell, e in sorted(fields.items(), key=lambda kv: ok_share(kv[1])):
            d = with_data(e) or 1
            rows.append([f"{e['name']} ({cell})", str(e["n"]), str(e.get("nodata", 0)),
                         f"{e['blank'] / d * 100:.0f}%", f"{e['bad'] / d * 100:.0f}%",
                         f"{ok_share(e) * 100:.0f}%"])
        rows.append(["Percentages are of reads that had data: GSPro shows '---' when "
                     "there is no shot yet, which is not a misread. Launch-monitor "
                     "fields are not counted on putts, where GSPro leaves them blank. A field that is often blank or unreadable on "
                     "full shots usually means its region needs adjusting."])

    return rows


# ── Clubs: gapping and strokes gained by club ──────────────────────────────────
#
# Built from the SG_Shots rows, so it covers every shot already recorded. Each
# shot is credited to the club in its Club column — the club that HIT it. Rows
# written before v3.3 credited the NEXT shot's club; the format-4 upgrade fixes
# them, except a hole's first shot, which only a rebuild from the log recovers.

MIN_GAP_SHOTS   = 3      # a club needs this many full shots before gaps are judged
OVERLAP_YD      = 5.0    # two clubs closer than this in median carry overlap
LARGE_GAP_YD    = 20.0   # a gap wider than this leaves distances you cannot hit


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _bag_order() -> list[str]:
    try:
        import ocr_vocab
        return list(ocr_vocab.CLUBS)
    except ImportError:
        return []


# ── Which side of the line a shot finished (T-31, v3.8.13) ─────────────────────
# The geometry gives how far offline a shot finished, from distances alone, but
# not the side. The launch data does: predicted lateral ≈ L·sin(HLA) +
# SIDE_CURVE·L·sin(spin axis), positive = right of the target line as you face
# it. Fitted on 75 full shots (4, 5 and 8 Oct, 13–110 yd wedges): its size
# matches the measured offline distance with r = 0.93 (±1.3 yd), and the side
# matched the screen on 7 of 7 ball-camera frames where the predicted miss was
# 0.5 yd or more (4 of 6 below that). Checked with left-handed play; re-check
# with verify_sides.py after a change of hand or for clubs longer than a wedge.
SIDE_CURVE = 0.28
SIDE_FIRM_YD = 0.5


def estimate_side(hla: float | None, axis: float | None, total: float | None,
                  unit: str = "yd") -> tuple[float | None, int, bool]:
    """
    (predicted lateral, side, firm): side +1 = right, -1 = left, 0 = unknown.
    Firm when a full shot is predicted to miss by SIDE_FIRM_YD or more; a putt's
    side is its start line only (the green's slope is not known), never firm.
    """
    if hla is None or not total:
        return None, 0, False
    pred = total * math.sin(math.radians(hla)) + SIDE_CURVE * total * math.sin(math.radians(axis or 0.0))
    side = 1 if pred > 0 else -1 if pred < 0 else 0
    return pred, side, (unit != "ft" and abs(pred) >= SIDE_FIRM_YD)


def _shots_from_rows(shot_rows: list[list[str]]) -> tuple[list[dict], dict]:
    """Read SG_Shots rows into shot dicts; count what had to be left out."""
    excluded = {"estimated": 0, "conceded": 0, "club_unknown": 0}
    if not shot_rows or len(shot_rows) < 2:
        return [], excluded
    idx = {name: i for i, name in enumerate(shot_rows[0])}
    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else ""
    shots = []
    for row in shot_rows[1:]:
        if not row:
            continue
        note = get(row, "Note")
        if note and not note.startswith("recovered"):   # inferred strokes carry no club;
                                                        # recovered ones have club + launch data
            excluded["conceded" if note == "gimmie" else "estimated"] += 1
            continue
        club = get(row, "Club")
        if not club:
            excluded["club_unknown"] += 1
            continue
        shots.append({
            "club": club, "date": get(row, "Date"), "unit": get(row, "Pre Unit"),
            "cat": get(row, "SG Category"), "sg": _num(get(row, "SG")),
            "carry": _num(get(row, "Carry Raw")) or _num(get(row, "Carry Game")),
            "total": _num(get(row, "Total Length")), "speed": _num(get(row, "Ball Speed")),
            "bias": _num(get(row, "Short/Long")), "bias_unit": get(row, "Geom Unit"),
            "offline": _num(get(row, "Offline")), "time": get(row, "Time"),
            "hole": get(row, "Hole"), "course": get(row, "Course"),
            "hla": _num(get(row, "HLA")), "axis": _num(get(row, "Spin Axis")),
        })
        s = shots[-1]
        s["side_pred"], s["side"], s["side_firm"] = estimate_side(
            s["hla"], s["axis"], s["total"], s["bias_unit"] or "yd")
    return shots, excluded


def gapping_stats(shots: list[dict]) -> tuple[list[dict], int]:
    """
    The numbers behind club gapping, one dict per club in bag order: carry
    median, quartiles, spread, extremes, gap to the next judged club and a note.
    Used by the Clubs sheet and the dashboard, so the two always agree.
    Returns (stats, number of full shots with no carry reading).
    """
    full = [s for s in shots if s["unit"] == "yd" and s["club"] != "Putter"]
    by: dict[str, list[dict]] = {}
    for s in full:
        by.setdefault(s["club"], []).append(s)
    order = [c for c in _bag_order() if c in by] + sorted(c for c in by if c not in _bag_order())
    stats: list[dict] = []
    blank_carry = 0
    for club in order:
        carries = [s["carry"] for s in by[club] if s["carry"] and s["carry"] > 0]
        blank_carry += len(by[club]) - len(carries)
        st = {"club": club, "n": len(by[club]), "carries": carries,
              "counted": len([s for s in by[club] if s["carry"]]),
              "median": None, "gap": None, "gap_to": "", "note": ""}
        if not carries:
            st["note"] = "no carry readings"
            stats.append(st)
            continue
        if len(carries) >= 2:
            p25, _, p75 = statistics.quantiles(carries, n=4, method="inclusive")
            sd = statistics.stdev(carries)
        else:
            p25 = p75 = carries[0]
            sd = 0.0
        totals = [s["total"] for s in by[club] if s["total"]]
        speeds = [s["speed"] for s in by[club] if s["speed"]]
        st.update(median=statistics.median(carries), p25=p25, p75=p75, sd=sd,
                  min=min(carries), max=max(carries),
                  total_median=statistics.median(totals) if totals else None,
                  speed_median=statistics.median(speeds) if speeds else None)
        stats.append(st)
    med = {st["club"]: st["median"] for st in stats if st["median"] is not None}
    judged = [c for c in order if c in med
              and next(st for st in stats if st["club"] == c)["counted"] >= MIN_GAP_SHOTS]
    for st in stats:
        club = st["club"]
        if club not in med:
            continue
        if st["counted"] < MIN_GAP_SHOTS:
            st["note"] = f"few shots (under {MIN_GAP_SHOTS}) — not judged"
            continue
        later = judged[judged.index(club) + 1:]
        if later:
            nxt = later[0]
            st["gap"], st["gap_to"] = med[club] - med[nxt], nxt
            if st["gap"] < OVERLAP_YD:
                st["note"] = f"overlaps {nxt}"
            elif st["gap"] > LARGE_GAP_YD:
                st["note"] = f"large gap to {nxt}"
    return stats, blank_carry


def _gapping_rows(shots: list[dict], title: str) -> list[list[str]]:
    rows = [[title], ["Club", "Full shots", "Carry median", "Carry p25", "Carry p75",
                      "IQR", "Std dev", "Shortest", "Longest", "Total median",
                      "Ball speed median", "Gap to next", "Note"]]
    stats, blank_carry = gapping_stats(shots)
    if not stats:
        rows.append(["No full-swing shots with a club yet."])
        return rows
    for st in stats:
        if st["median"] is None:
            rows.append([st["club"], str(st["n"]), "—", "", "", "", "", "", "", "", "", "",
                         st["note"]])
            continue
        rows.append([st["club"], str(st["n"]), f"{st['median']:.1f}", f"{st['p25']:.1f}",
                     f"{st['p75']:.1f}", f"{st['p75'] - st['p25']:.1f}", f"{st['sd']:.1f}",
                     f"{st['min']:.1f}", f"{st['max']:.1f}",
                     f"{st['total_median']:.1f}" if st["total_median"] is not None else "",
                     f"{st['speed_median']:.1f}" if st["speed_median"] is not None else "",
                     f"{st['gap']:+.1f} to {st['gap_to']}" if st["gap"] is not None else "",
                     st["note"]])
    if blank_carry:
        rows.append([f"{blank_carry} full shot(s) had no carry reading and are not in "
                     "the carry figures (see FIELD READ HEALTH in Round Stats)."])
    return rows


def build_clubs(shot_rows: list[list[str]]) -> list[list[str]]:
    """The Clubs sheet: gapping, then strokes gained by club."""
    shots, excluded = _shots_from_rows(shot_rows)
    rows: list[list[str]] = [["CLUBS — distances and strokes gained by the club that hit each shot"], [""]]
    rows += _gapping_rows(shots, "CLUB GAPPING — all sessions")

    dates = sorted(s["date"] for s in shots if s["date"])
    if dates:
        latest = datetime.strptime(dates[-1], DATE_FMT)
        since = (latest - timedelta(days=30)).strftime(DATE_FMT)
        recent = [s for s in shots if s["date"] and s["date"] > since]
        if 0 < len(recent) < len(shots):
            rows.append([""])
            rows += _gapping_rows(recent, f"CLUB GAPPING — last 30 days of play (to {dates[-1]})")

    rows.append([""])
    rows.append(["STROKES GAINED BY CLUB", "Shots", "SG total", "SG/shot",
                 f"{sg_mod.CAT_OTT}/shot", f"{sg_mod.CAT_APP}/shot",
                 f"{sg_mod.CAT_ATG}/shot", f"{sg_mod.CAT_PUTT}/shot",
                 "Mean short/long", "Unit"])
    by: dict[str, list[dict]] = {}
    for s in shots:
        if s["sg"] is not None:
            by.setdefault(s["club"], []).append(s)
    order = [c for c in _bag_order() if c in by] + sorted(c for c in by if c not in _bag_order())
    for club in order:
        ss = by[club]
        tot = sum(s["sg"] for s in ss)
        cells = [club, str(len(ss)), f"{tot:+.3f}", f"{tot / len(ss):+.3f}"]
        for cat in sg_mod.ALL_CATS:
            c = [s["sg"] for s in ss if s["cat"] == cat]
            cells.append(f"{sum(c) / len(c):+.3f}" if c else "")
        b = [s for s in ss if s["bias"] is not None]
        if b:
            unit = b[0]["bias_unit"] or "yd"     # stored already in this unit
            cells += [f"{sum(s['bias'] for s in b) / len(b):+.1f}", unit]
        else:
            cells += ["", ""]
        rows.append(cells)
    if not order:
        rows.append(["No scored shots with a known club yet."])
    on_record = len(shots) + excluded["estimated"] + excluded["conceded"] + excluded["club_unknown"]
    if on_record:
        rows.append([f"From captured shots only. Coverage: {on_record - excluded['estimated']} of "
                     f"{on_record} strokes on record were captured or conceded "
                     f"({coverage_label(on_record - excluded['estimated'], on_record)}); the "
                     "rest were inferred from the round score and have no club."])

    rows.append([""])
    rows.append(["Each shot is credited to the club GSPro showed BEFORE it. After a shot "
                 "settles the HUD already shows the next shot's club, so crediting the "
                 "club shown after a shot (as versions before 3.3 did) is wrong."])
    left = []
    if excluded["estimated"]:
        left.append(f"{excluded['estimated']} estimated stroke(s)")
    if excluded["conceded"]:
        left.append(f"{excluded['conceded']} conceded putt(s)")
    if excluded["club_unknown"]:
        left.append(f"{excluded['club_unknown']} shot(s) with no club recorded "
                    "(first shots of holes in rows from before v3.3 — rebuild from the "
                    "log to recover them)")
    if left:
        rows.append(["Not included: " + "; ".join(left) + "."])
    rows.append([f"Gaps are judged only for clubs with {MIN_GAP_SHOTS}+ full shots. "
                 f"Under {OVERLAP_YD:.0f} yd between neighbouring clubs = overlap; over "
                 f"{LARGE_GAP_YD:.0f} yd = a distance range you may not be able to hit."])
    return rows


def build_summary_sheets(round_state: dict, sessions: list, shot_rows: list,
                         version: str) -> dict:
    """
    Every generated sheet, built in one place. The database export and the
    rebuild tool both call this, so their reports cannot drift apart.
    """
    out = {"SG_Summary": build_sg_summary(round_state, sessions, version)}
    if sessions:
        out["Round Stats"] = build_round_stats(round_state, sessions, version, shot_rows)
    out["Clubs"] = build_clubs(shot_rows)
    return out
