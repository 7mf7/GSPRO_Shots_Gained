#!/usr/bin/env python3
"""
test_engine.py — Tests for the shared scoring engine and log replay.

Run:  python test_engine.py

Covers what v2.9.0 introduced: the engine running without the daemon, the
injectable clock, exact log decoding, dated replay, and a regression test for
the shared-defaults bug the rebuild-versus-live parity test uncovered.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import reports               # noqa: E402
import strokes_gained as sg  # noqa: E402
import scoring               # noqa: E402
import rebuild_from_log as rb  # noqa: E402

logging.disable(logging.CRITICAL)

CONFIG = {"session_gap_minutes": 90}


def capture(hole, rem, lie, ticker, yd=150, course="Rock Bay Golf", club="L Wedge",
            score=None):
    c = {"A1": course, "B1": f"Hole {hole}", "C1": "Par 4", "D1": f"{yd} yds",
         "G1": lie, "K1": rem, "V1": str(ticker), "I1": club, "P1": ""}
    if score is not None:
        c["U1"] = score                 # GSPro's round score vs par, e.g. "E", "+1", "-10"
    return c


def fresh_state():
    return {"row_offset": 0, "round": scoring._fix_round({}), "last_capture": None}


def play(captures, start=datetime(2026, 9, 1, 10, 0), step_min=2, end=False):
    """
    Run captures through the engine with a MemorySink and a stepped clock.
    end=True closes the open hole as the end of a run does: a hole that is over
    is finalised, one still in play is discarded.
    """
    sink, state, outcomes = scoring.MemorySink(), fresh_state(), []
    t = start
    for c in captures:
        scoring.set_clock(lambda t=t: t)
        state, outcome, _ = scoring.handle_capture(state, c, {}, sink, CONFIG)
        outcomes.append(outcome)
        t = t.replace(minute=t.minute + step_min) if t.minute + step_min < 60 else t
    if end:
        scoring.close_open_hole(state["round"], sink)
    scoring.set_clock(None)
    return sink, state, outcomes


ONE_HOLE = [
    capture(1, "150.0yd", "Tee", 1),        # hole set-up
    capture(1, "40.0yd", "Fairway", 2),     # tee shot
    capture(1, "3.0yd", "Green", 3),        # approach to 9 ft
    capture(1, "0.0yd", "Green", 4),        # holed putt
]


class SharedDefaults(unittest.TestCase):
    """Regression: fresh rounds once shared one set of statistics containers."""

    def test_fresh_rounds_are_independent(self):
        a, b = scoring._fix_round({}), scoring._fix_round({})
        for key in ("band_stats", "geom_stats", "hole_history", "pending_shots",
                    "shot_counts_by_lie", "sg_by_lie"):
            self.assertIsNot(a[key], b[key], f"{key} shared between fresh rounds")
        a["band_stats"]["Putting"] = {"0-1 ft": {"n": 1}}
        a["hole_history"].append({"hole": 1})
        self.assertEqual(scoring._fix_round({})["band_stats"], {})
        self.assertEqual(scoring.DEFAULT_ROUND["hole_history"], [])

    def test_second_session_does_not_inherit_first(self):
        sink, state, _ = play(ONE_HOLE)
        before = sum(e["n"] for c in state["round"]["band_stats"].values() for e in c.values())
        self.assertGreater(before, 0)
        scoring._close_session(state, sink, "test")
        self.assertEqual(state["round"]["band_stats"], {},
                         "a new session must start with empty statistics")


class Engine(unittest.TestCase):
    def test_hole_scored_and_finalised_without_the_daemon(self):
        sink, state, _ = play(ONE_HOLE)
        self.assertEqual(state["round"]["hole_over"], "holed")
        self.assertEqual(len(sink.sheets.get("SG_Holes", [])) - 1 if "SG_Holes" in sink.sheets else 0, 0,
                         "a holed hole is finalised when it closes, not at the hole-out")
        sink, state, outcomes = play(ONE_HOLE, end=True)
        self.assertEqual(outcomes, ["accepted"] * 4)
        shots = sink.sheets["SG_Shots"][1:]
        self.assertEqual(len(shots), 3, "tee shot, approach, putt")
        self.assertEqual(len(sink.sheets["SG_Holes"]) - 1, 1, "one finished hole")
        self.assertEqual(state["round"]["holes_completed"], 1)
        self.assertEqual(len(sink.sheets["Sheet1"]), 4, "one raw row per accepted capture")

    def test_replayed_capture_is_skipped(self):
        sink, state, outcomes = play(ONE_HOLE[:2] + [ONE_HOLE[1]])
        self.assertEqual(outcomes[-1], "no_progression")

    def test_menu_screen_is_invalid(self):
        _, _, outcomes = play([{"A1": "", "B1": "", "C1": ""}])
        self.assertEqual(outcomes, ["invalid"])

    def test_clock_injection_stamps_rows(self):
        sink, _, _ = play(ONE_HOLE[:2], start=datetime(2031, 1, 2, 3, 4))
        raw = sink.sheets["Sheet1"][0]
        self.assertIn("2031-01-02", raw, "rows carry the injected time, not the real clock")

    def test_idle_gap_splits_sessions(self):
        sink, state, _ = play(ONE_HOLE[:2], start=datetime(2026, 9, 1, 10, 0))
        scoring.set_clock(lambda: datetime(2026, 9, 1, 14, 0))     # 4 hours later
        state, outcome, _ = scoring.handle_capture(
            state, capture(1, "40.0yd", "Fairway", 2, course="Rock Bay Golf") | {"B1": "Hole 2", "V1": "1", "G1": "Tee", "K1": "150.0yd"},
            {}, sink, CONFIG)
        scoring.set_clock(None)
        self.assertEqual(outcome, "accepted")
        self.assertTrue(state["round"]["session_started"].startswith("2026-09-01 14:00"),
                        "the late capture opened a new session")


class RowPositions(unittest.TestCase):
    """
    Regression: SG_Shots / SG_Holes row positions used to reset at every session
    boundary, so a new session overwrote the previous session's rows and the
    end-of-run discard then cut the sheets down to the new session's length.
    """

    def two_sessions(self, then_incomplete=False):
        # The second course deliberately starts on a DIFFERENT hole number:
        # same-number transitions hit a separate known issue (see KnownIssues).
        h = "The Hills Course"
        caps = list(ONE_HOLE) + [                                   # Rock Bay: 3 shots
            capture(3, "120.0yd", "Tee",   1, yd=120, course=h),
            capture(3, "2.0yd",   "Green", 2, yd=120, course=h),
            capture(3, "0.0yd",   "Green", 3, yd=120, course=h),
        ]                                                            # Hills: 2 shots
        if then_incomplete:
            caps += [capture(4, "130.0yd", "Tee",     1, yd=130, course=h),
                     capture(4, "45.0yd",  "Fairway", 2, yd=130, course=h)]
        return play(caps)

    def test_new_session_does_not_overwrite_earlier_rows(self):
        sink, state, _ = self.two_sessions()
        scoring.close_open_hole(state["round"], sink)               # end of run
        shots = sink.sheets["SG_Shots"]
        course_col = shots[0].index("Course")
        courses = [r[course_col] for r in shots[1:]]
        self.assertEqual(len(courses), 5, "3 shots from the first session + 2 from the second")
        self.assertEqual(courses.count("Rock Bay Golf"), 3, "first session's rows survived")
        self.assertEqual(len(sink.sheets["SG_Holes"]) - 1, 2, "one finished hole per session")

    def test_exit_discard_removes_only_the_unfinished_hole(self):
        sink, state, _ = self.two_sessions(then_incomplete=True)
        self.assertEqual(len(sink.sheets["SG_Shots"]) - 1, 6)
        scoring._discard_incomplete_hole(state["round"], sink)
        self.assertEqual(len(sink.sheets["SG_Shots"]) - 1, 5,
                         "only the unfinished hole's shot is removed; both sessions intact")


def hole_scores(sink):
    h = sink.sheets.get("SG_Holes", [])
    return [r[h[0].index("Score")] for r in h[1:]] if h else []


class Scenarios(unittest.TestCase):
    """How holes start, end and are counted — including distance-challenge
    stations that repeat hole numbers and GSPro rounds that reset the score."""

    def test_holed_hole_counted_exactly_once(self):
        """Regression: a hole-out was finalised, then finalised AGAIN at the next hole."""
        sink, state, _ = play(list(ONE_HOLE) + [capture(2, "150.0yd", "Tee", 1)])
        self.assertEqual(hole_scores(sink), ["3"])
        self.assertEqual(state["round"]["holes_completed"], 1)
        self.assertEqual(state["round"]["completed_shots"], 3)

    def test_round_score_adds_a_missed_stroke_within_one_finalisation(self):
        caps = [capture(1, "150.0yd", "Tee", 1, score="E"),
                capture(1, "40.0yd", "Fairway", 2, score="E"),
                capture(1, "0.0yd", "Green", 3, score="E"),        # holed: 2 captured
                capture(2, "150.0yd", "Tee", 1, score="-1")]        # par 4 −1 → 3 strokes
        sink, state, _ = play(caps)
        self.assertEqual(hole_scores(sink), ["3"], "one row, with the verified stroke count")
        notes = [dict(zip(sink.sheets["SG_Shots"][0], r))["Note"] for r in sink.sheets["SG_Shots"][1:]]
        self.assertEqual(notes.count("uncaptured stroke"), 1)

    def test_repeated_hole_number_is_a_fresh_attempt(self):
        """Regression: distance-challenge stations repeat hole numbers; strokes carried over."""
        again = [capture(1, "150.0yd", "Tee", 1), capture(1, "0.0yd", "Green", 2)]
        sink, state, outcomes = play(list(ONE_HOLE) + again, end=True)
        self.assertEqual(outcomes, ["accepted"] * 6)
        self.assertEqual(hole_scores(sink), ["3", "1"])
        self.assertEqual(state["round"]["completed_shots"], 4)

    def test_new_course_starting_on_same_hole_number_is_not_a_replay(self):
        """Regression: 'hole 1, stroke 1' on a new course was rejected as a replay."""
        caps = list(ONE_HOLE) + [
            capture(1, "120.0yd", "Tee",   1, yd=120, course="The Hills Course"),
            capture(1, "2.0yd",   "Green", 2, yd=120, course="The Hills Course")]
        _, _, outcomes = play(caps)
        self.assertEqual(outcomes[4:], ["accepted", "accepted"])

    def test_new_gspro_round_adds_no_phantom_strokes(self):
        """Regression: round score resetting −10 → E read as +10, scoring a 25 yd hole at 14."""
        caps = [capture(9, "25.0yd", "Tee",   1, yd=25, score="-10"),
                capture(9, "0.3yd",  "Green", 2, yd=25, score="-10"),   # 0.9 ft: gimmie zone
                capture(1, "50.0yd", "Tee",   1, yd=50, score="E")]     # new round: 9 → 1
        sink, _, _ = play(caps)
        self.assertEqual(hole_scores(sink), ["2"], "1 captured + the conceded putt, not 14")

    def test_implausible_round_score_jump_is_rejected(self):
        caps = [capture(2, "150.0yd", "Tee",   1, score="E"),
                capture(2, "0.0yd",   "Green", 2, score="E"),
                capture(3, "150.0yd", "Tee",   1, score="+9")]          # misread: +9 on one hole
        sink, _, _ = play(caps)
        self.assertEqual(hole_scores(sink), ["1"], "only the certain stroke count is used")

    def test_gimmie_at_end_of_run_counts_the_conceded_putt(self):
        caps = [capture(1, "150.0yd", "Tee", 1), capture(1, "0.3yd", "Green", 2)]
        sink, state, _ = play(caps, end=True)
        self.assertEqual(hole_scores(sink), ["2"])
        self.assertEqual([dict(zip(sink.sheets["SG_Shots"][0], r))["Note"] for r in sink.sheets["SG_Shots"][1:]], ["", "gimmie"])

    def test_unfinished_hole_is_discarded_at_end_of_run(self):
        caps = [capture(1, "150.0yd", "Tee", 1), capture(1, "45.0yd", "Fairway", 2)]
        sink, state, _ = play(caps, end=True)
        self.assertEqual(hole_scores(sink), [])
        self.assertEqual(len(sink.sheets["SG_Shots"]) - 1, 0, "its shot is removed")
        self.assertEqual(state["round"]["shots_total"], 0)

    def test_result_screen_captured_again_is_skipped(self):
        sink, _, outcomes = play(list(ONE_HOLE) + [ONE_HOLE[-1]])
        self.assertEqual(outcomes[-1], "no_progression")

    def test_ticker_going_backward_mid_hole_is_still_a_replay(self):
        caps = [capture(1, "150.0yd", "Tee", 1), capture(1, "40.0yd", "Fairway", 2),
                capture(1, "3.0yd", "Green", 3), capture(1, "40.0yd", "Fairway", 2)]
        _, _, outcomes = play(caps)
        self.assertEqual(outcomes[-1], "no_progression", "replay protection still works in play")


class Quality(unittest.TestCase):
    """Capture quality is counted by the shared engine, so rebuilds report it too."""

    def test_outcomes_are_counted(self):
        menu = {"A1": "", "B1": "", "C1": ""}
        _, state, _ = play([menu] + list(ONE_HOLE) + [ONE_HOLE[-1]])
        q = state["round"]["quality"]
        self.assertEqual((q["captures"], q["accepted"], q["invalid"], q["no_progression"]),
                         (6, 4, 1, 1))

    def test_stroke_sources(self):
        caps = [capture(1, "150.0yd", "Tee", 1, score="E"),
                capture(1, "0.3yd", "Green", 2, score="E"),          # gimmie zone
                capture(2, "150.0yd", "Tee", 1, score="E")]          # par 4, E → 4 strokes
        _, state, _ = play(caps)
        q = state["round"]["quality"]
        self.assertEqual((q["strokes_captured"], q["strokes_conceded"], q["strokes_uncaptured"]),
                         (1, 1, 2), "1 captured, the conceded gimmie, 2 inferred from the score")

    def test_discarded_hole_strokes_are_not_counted(self):
        caps = [capture(1, "150.0yd", "Tee", 1), capture(1, "45.0yd", "Fairway", 2)]
        _, state, _ = play(caps, end=True)
        self.assertEqual(state["round"]["quality"]["strokes_captured"], 0)

    def test_field_health(self):
        caps = [capture(1, "150.0yd", "Tee", 1, club="L Weda"),      # snaps: fine
                capture(1, "40.0yd", "", 2, club="zz")]               # blank lie, bad club
        _, state, _ = play(caps)
        f = state["round"]["quality"]["fields"]
        self.assertEqual((f["I1"]["n"], f["I1"]["bad"]), (2, 1))
        self.assertEqual(f["G1"]["blank"], 1)

    def test_quality_is_archived_with_the_session(self):
        sink, state, _ = play(ONE_HOLE, end=True)
        scoring._close_session(state, sink, "test")
        self.assertEqual(sink.history[-1]["quality"]["strokes_captured"], 3)


class ClubAttribution(unittest.TestCase):
    """Regression: every shot was credited to the club shown AFTER it — the next club."""

    def test_each_shot_takes_the_club_shown_before_it(self):
        caps = [capture(1, "150.0yd", "Tee",     1, club="8 Iron"),   # set-up: tee club
                capture(1, "10.0yd",  "Fairway", 2, club="L Wedge"),  # after shot 1
                capture(1, "3.0yd",   "Green",   3, club="Putter"),   # after shot 2
                capture(1, "0.0yd",   "Green",   4, club="Driver")]   # holed; next hole's club
        sink, _, _ = play(caps)
        h = sink.sheets["SG_Shots"][0]
        self.assertEqual([r[h.index("Club")] for r in sink.sheets["SG_Shots"][1:]],
                         ["8 Iron", "L Wedge", "Putter"])
        self.assertEqual([r[h.index("Region9")] for r in sink.sheets["SG_Shots"][1:]],
                         ["L Wedge", "Putter", "Driver"], "the raw read is kept as recorded")


def shot_rows(specs):
    """SG_Shots rows from (club, carry, unit, sg, category, note) tuples."""
    h = list(scoring._SG_SHOTS_HEADERS)
    rows = [h]
    for club, carry, unit, sgv, cat, note in specs:
        d = {"Date": "2026-09-05", "Club": club, "Carry Raw": carry, "Pre Unit": unit,
             "SG": sgv, "SG Category": cat, "Note": note}
        rows.append([d.get(c, "") for c in h])
    return rows


class Clubs(unittest.TestCase):
    SPECS = ([("8 Iron", c, "yd", "+0.1", "Approach", "") for c in ("140", "145", "150")] +
             [("9 Iron", c, "yd", "-0.1", "Approach", "") for c in ("140", "142", "144")] +
             [("P Wedge", c, "yd", "+0.2", "Approach", "") for c in ("100", "105", "110")] +
             [("L Wedge", "60", "yd", "+0.0", "Approach", ""),
              ("Putter", "", "ft", "+0.3", "Putting", ""),
              ("", "", "yd", "-1.0", "Approach", "uncaptured stroke"),
              ("", "120", "yd", "+0.0", "Approach", "")])

    def sheet(self):
        return reports.build_clubs(shot_rows(self.SPECS))

    def row(self, sheet, club):
        return next(r for r in sheet if r and r[0] == club)

    def test_medians_and_quartiles(self):
        r = self.row(self.sheet(), "8 Iron")
        self.assertEqual(r[1:5], ["3", "145.0", "142.5", "147.5"])

    def test_overlap_and_large_gap_are_flagged(self):
        s = self.sheet()
        self.assertEqual(self.row(s, "8 Iron")[11:13], ["+3.0 to 9 Iron", "overlaps 9 Iron"])
        self.assertEqual(self.row(s, "9 Iron")[12], "large gap to P Wedge")

    def test_small_samples_are_not_judged(self):
        self.assertIn("few shots", self.row(self.sheet(), "L Wedge")[12])

    def test_putts_excluded_from_gapping_but_in_strokes_gained(self):
        s = self.sheet()
        sg_start = next(i for i, r in enumerate(s) if r and r[0] == "STROKES GAINED BY CLUB")
        self.assertFalse(any(r and r[0] == "Putter" for r in s[:sg_start]))
        self.assertEqual(self.row(s[sg_start:], "Putter")[2], "+0.300")

    def test_exclusions_are_stated(self):
        notes = " ".join(r[0] for r in self.sheet() if r and r[0].startswith("Not included"))
        self.assertIn("1 estimated stroke", notes)
        self.assertIn("1 shot(s) with no club recorded", notes)


class Categories(unittest.TestCase):
    def test_par3_tee_shot_is_approach(self):
        self.assertEqual(sg.shot_category(1, "Tee", 165, par=3), sg.CAT_APP)
        self.assertEqual(sg.shot_category(1, "Tee", 25, par=3), sg.CAT_ATG)
        self.assertEqual(sg.shot_category(1, "Tee", 400, par=4), sg.CAT_OTT)

    def test_missed_strokes_are_unattributed_and_totals_unchanged(self):
        caps = [capture(1, "150.0yd", "Tee", 1, score="E"),
                capture(1, "0.3yd", "Green", 2, score="E"),          # gimmie zone
                capture(2, "150.0yd", "Tee", 1, score="E")]          # 4 strokes, 1 captured
        sink, state, _ = play(caps)
        h = sink.sheets["SG_Shots"][0]
        rows = [dict(zip(h, r)) for r in sink.sheets["SG_Shots"][1:]]
        self.assertEqual([r["SG Category"] for r in rows if r["Note"]],
                         [sg.CAT_PUTT, sg.CAT_UNATTRIBUTED, sg.CAT_UNATTRIBUTED],
                         "the conceded putt stays Putting; the two missed strokes are Unattributed")
        r = state["round"]
        self.assertAlmostEqual(sum(r["sg_round"].values()),
                               sum(float(x["SG"]) for x in rows), places=2)

    def test_old_state_without_the_new_category_loads(self):
        old = scoring._fix_round({"sg_round": {c: 0.0 for c in sg.ALL_CATS},
                                  "shot_counts_by_cat": {c: 0 for c in sg.ALL_CATS}})
        self.assertIn(sg.CAT_UNATTRIBUTED, old["sg_round"])
        self.assertIn(sg.CAT_UNATTRIBUTED, old["shot_counts_by_cat"])


class ScreenshotFindings(unittest.TestCase):
    def test_placeholder_dashes_are_no_data_not_misreads(self):
        caps = [capture(1, "150.1yd", "Tee", 1) | {"L1": "--", "N1": "-—="},
                capture(1, "40.0yd", "Fairway", 2) | {"L1": "25.0", "N1": "130.6"}]
        _, state, _ = play(caps)
        f = state["round"]["quality"]["fields"]
        self.assertEqual((f["L1"]["nodata"], f["L1"]["bad"]), (1, 0))
        self.assertEqual((f["N1"]["nodata"], f["N1"]["bad"]), (1, 0))

    def test_deep_lie_is_recognised(self):
        _, state, _ = play([capture(1, "150.0yd", "Tee", 1), capture(1, "116.6yd", "Deep", 2)])
        self.assertEqual(state["round"]["quality"]["fields"]["G1"]["bad"], 0)

    def test_calm_wind_has_no_direction(self):
        import parsing
        p = parsing.parse_ocr_fields({"E1": "0 MPH", "T1": "NW (Head+Left)"}, {})
        self.assertEqual((p["wind_speed"], p["wind_2"]), ("0", "calm"))
        p = parsing.parse_ocr_fields({"E1": "12 MPH", "T1": "NW"}, {})
        self.assertEqual((p["wind_speed"], p["wind_2"]), ("12", "NW"))


class LogReplay(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_engine_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        p = self.dir / "ocr_daemon.log"
        p.write_text(text, encoding="utf-8")
        return p

    def test_repr_decoded_exactly(self):
        p = self.write(
            "10:00:00  INFO      ━━━  OCR pipeline  (10:00:00)  raw-row=0  ━━━\n"
            "10:00:01  INFO        [total length           P1]  '12\\' 4\"'\n"
            "10:00:01  INFO        [current hole score ticker V1]  '3'  (stroke ticker, par=4)\n")
        cap = rb.parse_log(p, default_date="2026-09-01")[0]
        self.assertEqual(cap.by_cell["P1"], "12' 4\"", "quotes in feet-inch values survive")
        self.assertEqual(cap.by_cell["V1"], "3")

    def test_first_date_applies_from_the_start(self):
        """No fake gap before the first dated line (the old harness's bug)."""
        p = self.write(
            "10:00:00  INFO      ━━━  OCR pipeline  (10:00:00)  raw-row=0  ━━━\n"
            "10:05:00  INFO      ━━━  OCR pipeline  (10:05:00)  raw-row=1  ━━━\n"
            "10:05:02  INFO        captured at 2026-09-05 10:05:02\n")
        caps = rb.parse_log(p)
        self.assertEqual({c.ts.date().isoformat() for c in caps}, {"2026-09-05"})

    def test_midnight_rollover(self):
        p = self.write(
            "23:59:50  INFO      ━━━  OCR pipeline  (23:59:50)  raw-row=0  ━━━\n"
            "00:00:10  INFO      ━━━  OCR pipeline  (00:00:10)  raw-row=1  ━━━\n")
        a, b = rb.parse_log(p, default_date="2026-09-05")
        self.assertEqual((b.ts - a.ts).total_seconds(), 20)

    def test_new_dated_format(self):
        p = self.write(
            "2026-10-03 09:00:00  INFO      ━━━  OCR pipeline  (09:00:00)  raw-row=0  ━━━\n"
            "2026-10-03 09:00:01  INFO        [Hole                   B1]  'Hole 1'\n")
        cap = rb.parse_log(p)[0]
        self.assertEqual(cap.ts, datetime(2026, 10, 3, 9, 0, 0))
        self.assertEqual(cap.by_cell["B1"], "Hole 1")

    def test_several_log_files_replay_as_one(self):
        """Archived logs + the current log must equal one continuous log."""
        lines = ["2026-10-03 09:%02d:00  INFO      ━━━  OCR pipeline  (09:%02d:00)  raw-row=0  ━━━\n"
                 "2026-10-03 09:%02d:01  INFO        [Hole                   B1]  'Hole %d'\n"
                 % (i, i, i, i + 1) for i in range(4)]
        whole = self.write("".join(lines))
        a = self.dir / "a.log"; a.write_text("".join(lines[:2]), encoding="utf-8")
        b = self.dir / "b.log"; b.write_text("".join(lines[2:]), encoding="utf-8")
        one = [(c.ts, c.by_cell) for c in rb.parse_log(whole)]
        two = [(c.ts, c.by_cell) for c in rb.parse_log(a) + rb.parse_log(b)]
        self.assertEqual(one, two)

    def test_rebuild_uses_the_engine(self):
        """rebuild.replay must give exactly what driving the engine directly gives."""
        caps = [rb.Capture(ts=datetime(2026, 9, 1, 10, i * 2), by_cell=c)
                for i, c in enumerate(ONE_HOLE)]
        sheets, history, stats = rb.replay(caps, CONFIG)
        sink, _, _ = play(ONE_HOLE, end=True)
        self.assertEqual(sheets["SG_Shots"], sink.sheets["SG_Shots"])
        self.assertEqual(sheets["SG_Holes"], sink.sheets["SG_Holes"])
        self.assertEqual(stats["shots"], 3)
        self.assertEqual(len(history), 1, "the session is archived at end of log")
        self.assertEqual(list(sheets)[-1], "_meta", "bookkeeping tab is last")
        self.assertIn("Clubs", sheets)


class ShortHoleRounds(unittest.TestCase):
    """v3.6.1 — found in a real Operation 36 round (nine 25-yd par 4s, 26 strokes)."""

    def sink(self, caps):
        res = play(caps)
        return res[0] if isinstance(res, tuple) else res

    def test_hole_with_no_captured_shot_is_reconciled(self):
        """Hole 1: the only shot finished in the gimmie circle, GSPro went straight
        to hole 2, so nothing was captured on hole 1 — it used to vanish."""
        s = self.sink([capture(1, "25.0yd", "Tee", 1, yd=25, score="E"),
                       capture(2, "25.1yd", "Tee", 1, yd=25, score="-2")])
        self.assertEqual(hole_scores(s), ["2"], "E → −2 on a par 4 is two strokes")

    def test_unfinished_hole_is_not_recorded_short_across_a_new_round(self):
        """Hole 7 was recorded as a 1 (−3) when the next GSPro round started."""
        s = self.sink([capture(7, "25.0yd", "Tee", 1, yd=25, score="-5"),
                       capture(7, "7ft 5", "Green", 2, yd=25, score="-5"),
                       capture(1, "25.0yd", "Tee", 1, yd=25, score="E")])
        self.assertEqual(hole_scores(s), [], "not seen finished, no usable score: not recorded")

    def test_unfinished_hole_is_not_recorded_short_when_the_score_is_unreadable(self):
        s = self.sink([capture(1, "50.1yd", "Tee", 1, yd=50, score="E"),
                       capture(1, "7.0yd", "Green", 2, yd=50, score="E"),
                       capture(2, "50.1yd", "Tee", 1, yd=50, score="-")])
        self.assertEqual(hole_scores(s), [])

    def test_normal_reconciliation_is_unchanged(self):
        s = self.sink([capture(6, "25.0yd", "Tee", 1, yd=25, score="-4"),
                       capture(6, "5ft 5", "Green", 2, yd=25, score="-4"),
                       capture(7, "25.0yd", "Tee", 1, yd=25, score="-5")])
        self.assertEqual(hole_scores(s), ["3"])


def with_panel(c, vla, hla, speed, total, spin="0", axis="0.0"):
    """A capture whose shot panel shows a shot's launch data."""
    return dict(c, L1=vla, M1=hla, Q1=speed, P1=total, S1=spin, R1=axis)


def shots_of(sink, hole):
    """SG_Shots rows for one hole as dicts keyed by header."""
    rows = sink.sheets.get("SG_Shots", [])
    if not rows:
        return []
    head = rows[0]
    hcol = next(i for i, h in enumerate(head) if h.strip().lower() == "hole")
    return [dict(zip(head, r)) for r in rows[1:] if str(r[hcol]) == str(hole)]


def col(row, *names):
    for n in names:
        for k in row:
            if k.strip().lower() == n.lower():
                return row[k]
    raise KeyError(names)


# The real round's hole 2: tee shot captured; a 21' 2" putt from 20 ft 4 in
# stopped in the gimmie circle and was conceded; GSPro went straight to hole 3.
HOLE2 = [with_panel(capture(2, "25.1yd", "Tee", 1, yd=25, score="-2"), "27.2", "2.3", "29.2", "26.1", "4183", "-3.6"),
         with_panel(capture(2, "20ft 4", "Green", 2, yd=25, score="-2", club="Putter"), "27.5", "0.5", "31.8", "31.8", "4492", "1.0"),
         with_panel(capture(3, "25.0yd", "Tee", 1, yd=25, score="-3"), "0.2", "-2.1", "7.2", "21' 2\"")]


class FinalStrokeRecovery(unittest.TestCase):
    """T-27 (v3.7.0): a hole's final stroke, read from the next hole's tee frame."""

    def tearDown(self):
        scoring.RECOVER_FINAL_STROKES = True

    def test_putt_into_the_gimmie_circle_is_a_real_putt(self):
        sink = play(HOLE2)[0]
        rows = shots_of(sink, 2)
        self.assertEqual([col(r, "SG Category") for r in rows], ["Off Tee", "Putting", "Putting"])
        putt = rows[1]
        self.assertEqual((col(putt, "Club"), col(putt, "Note")), ("Putter", "recovered: stopped in gimmie circle"))
        self.assertEqual(col(rows[2], "Note"), "gimmie", "then the conceded putt")
        self.assertEqual(hole_scores(sink), ["3"])

    def test_holed_final_putt(self):
        caps = [with_panel(capture(3, "25.0yd", "Tee", 1, yd=25, score="-3"), "0.2", "-2.1", "7.2", "21' 2\""),
                with_panel(capture(3, "6ft 4", "Green", 2, yd=25, score="-3", club="Putter"), "28.8", "1.7", "34.9", "33.6"),
                with_panel(capture(4, "25.0yd", "Tee", 1, yd=25, score="-5"), "0.1", "0.3", "3.3", "6' 2\"")]
        rows = shots_of(play(caps)[0], 3)
        self.assertEqual([col(r, "Note") for r in rows], ["", "recovered: holed"])
        self.assertEqual(col(rows[1], "SG Category"), "Putting")

    def test_hole_with_no_captured_shot_recovers_the_tee_shot(self):
        caps = [with_panel(capture(1, "25.0yd", "Tee", 1, yd=25, score="E"), "---", "---", "---", "---", "---", "---"),
                with_panel(capture(2, "25.1yd", "Tee", 1, yd=25, score="-2"), "27.2", "2.3", "29.2", "26.1", "4183", "-3.6")]
        rows = shots_of(play(caps)[0], 1)
        self.assertEqual([(col(r, "SG Category"), col(r, "Club")) for r in rows],
                         [("Off Tee", "L Wedge"), ("Putting", "")])

    def test_hole_strokes_gained_is_unchanged(self):
        """Only the classification changes: the hole's SG comes from the round score."""
        def total(sink):
            h = sink.sheets["SG_Holes"]
            return [r[h[0].index("SG-Total")] for r in h[1:]]
        with_rec = total(play(HOLE2)[0])
        scoring.RECOVER_FINAL_STROKES = False
        without = total(play(HOLE2)[0])
        self.assertEqual(with_rec, without)

    def test_falls_back_when_the_length_does_not_fit(self):
        caps = HOLE2[:2] + [with_panel(HOLE2[2], "0.2", "-2.1", "17.2", "15.0")]   # 15 yd from 20 ft
        rows = shots_of(play(caps)[0], 2)
        self.assertEqual(col(rows[1], "SG Category"), "Unattributed")

    def test_falls_back_when_the_next_frame_is_not_a_clean_tee(self):
        caps = HOLE2[:2] + [with_panel(capture(3, "10ft 1", "Green", 2, yd=25, score="-3"), "0.2", "-2.1", "7.2", "21' 2\"")]
        rows = shots_of(play(caps)[0], 2)
        self.assertEqual(col(rows[1], "SG Category"), "Unattributed")

    def test_falls_back_when_the_panel_shows_no_new_shot(self):
        """e.g. a penalty stroke: the round score moves, nothing new was hit."""
        caps = HOLE2[:2] + [dict(HOLE2[2], L1="27.5", M1="0.5", Q1="31.8", P1="31.8", S1="4492", R1="1.0")]
        rows = shots_of(play(caps)[0], 2)
        self.assertEqual(col(rows[1], "SG Category"), "Unattributed")

    def test_coverage_counts_recovered_strokes(self):
        sink, state, _ = play(HOLE2)
        q = state["round"]["quality"]
        self.assertEqual((q["strokes_captured"], q["strokes_recovered"], q["strokes_conceded"],
                          q["strokes_uncaptured"]), (1, 1, 1, 0))


class LostMinusSign(unittest.TestCase):
    """v3.8.1 — from a real Operation 36 session: GSPro's "-6" was read as "6"."""

    def sink(self, caps):
        return play(caps)[0]

    def test_bare_number_is_not_a_positive_score(self):
        import parsing
        p = parsing.parse_ocr_fields({"U1": "6"}, {})
        self.assertEqual((p["round_score"], p["round_score_unsigned"]), (None, 6))
        self.assertEqual(parsing.parse_ocr_fields({"U1": "+2"}, {})["round_score"], 2)

    def test_sign_restored_when_only_one_fits(self):
        """Hole 4: -5 at the tee, "6" at the next tee — only -6 is a possible hole."""
        s = self.sink([capture(4, "25.0yd", "Tee", 1, yd=25, score="-5"),
                       capture(4, "11ft 1", "Green", 2, yd=25, score="-5", club="Putter"),
                       capture(5, "25.0yd", "Tee", 1, yd=25, score="6")])
        self.assertEqual(hole_scores(s), ["3"])

    def test_restored_sign_carries_to_the_next_hole(self):
        """Hole 5 then read "6" all the way through: still a par, not a discard."""
        s = self.sink([capture(4, "25.0yd", "Tee", 1, yd=25, score="-5"),
                       capture(4, "11ft 1", "Green", 2, yd=25, score="-5", club="Putter"),
                       capture(5, "25.0yd", "Tee", 1, yd=25, score="6"),
                       capture(5, "24ft 6", "Green", 2, yd=25, score="6", club="Putter"),
                       capture(5, "4ft 3", "Green", 3, yd=25, score="6", club="Putter"),
                       capture(6, "25.0yd", "Tee", 1, yd=25, score="6")])
        self.assertEqual(hole_scores(s), ["3", "4"])

    def test_score_unreadable_on_the_tee_is_filled_in_later(self):
        """Hole 7: "-8" read as nothing on the tee, but readable on the green."""
        s = self.sink([capture(7, "25.0yd", "Tee", 1, yd=25, score=""),
                       capture(7, "21.3yd", "Fairway", 2, yd=25, score=""),
                       capture(7, "9ft 1", "Green", 3, yd=25, score="-8", club="Putter"),
                       capture(8, "25.0yd", "Tee", 1, yd=25, score="-9")])
        self.assertEqual(hole_scores(s), ["3"], "-8 → -9 on a par 4: a birdie, as played")

    def test_truly_ambiguous_reading_is_not_recorded(self):
        s = self.sink([capture(3, "25.0yd", "Tee", 1, yd=25, score="3"),
                       capture(3, "10ft 1", "Green", 2, yd=25, score="3", club="Putter"),
                       capture(4, "25.0yd", "Tee", 1, yd=25, score="5")])
        self.assertEqual(hole_scores(s), [], "2 or 6 strokes both fit: not guessed")

    def test_hole_with_no_shot_and_no_score_is_reported(self):
        import logging
        logging.disable(logging.NOTSET)                  # this suite silences logging
        try:
            with self.assertLogs("scoring", level="WARNING") as cm:
                play([capture(1, "25.0yd", "Tee", 1, yd=25, score="E"),
                      capture(2, "25.1yd", "Tee", 1, yd=25, score="")])
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("no stroke was captured" in m for m in cm.output))


def nine_holes(skip=(), end=None, then=None):
    """A 9-hole par-4 round: holes 1-8 birdied (tee shot captured, the putt
    recovered from the next tee), hole 9 tee shot to 5 ft, then GSPro's end
    screen showing `end` strokes, then `then` (e.g. the next round's tee)."""
    caps, score = [], 0
    for h in range(1, 10):
        s = "E" if score == 0 else f"{score:+d}"
        if h not in skip:
            caps.append(with_panel(capture(h, "25.0yd", "Tee", 1, yd=25, score=s), "0.2", "0.1", "6.0", "12' 0\""))
            caps.append(with_panel(capture(h, "12ft 0" if h < 9 else "5ft 2", "Green", 2, yd=25, score=s, club="Putter"),
                                   "30.0", "0.5", "30.0", "24.0"))
        score -= 1 if h < 9 else 0
    if end is not None:
        caps.append({"W1": str(end), "W2": "SCORE"})
    if then is not None:
        caps.append(then)
    return caps


class EndOfRoundScore(unittest.TestCase):
    """T-28: the last hole of a round closes from GSPro's end-of-round total."""

    NEXT_TEE = with_panel(capture(1, "50.1yd", "Tee", 1, yd=50, score="E"), "0.3", "0.2", "3.4", "5' 0\"")

    def test_parsing_requires_the_score_label(self):
        import parsing
        self.assertEqual(parsing.parse_ocr_fields({"W1": "24", "W2": "SCORE"}, {})["end_of_round_strokes"], 24)
        self.assertIsNone(parsing.parse_ocr_fields({"W1": "24", "W2": "LEVEL"}, {})["end_of_round_strokes"])

    def test_last_hole_closes_when_the_next_round_starts(self):
        sink, state, outcomes = play(nine_holes(end=26, then=self.NEXT_TEE))
        self.assertIn("end_of_round", outcomes)
        self.assertEqual(hole_scores(sink), ["3"] * 8 + ["2"], "8 birdies (24) + 2 = 26")
        last = shots_of(sink, 9)
        self.assertEqual(col(last[-1], "Note"), "recovered: holed", "the putt, from the next tee frame")

    def test_last_hole_closes_at_exit(self):
        sink, *_ = play(nine_holes(end=26), end=True)
        self.assertEqual(hole_scores(sink)[-1], "2")

    def test_total_not_used_if_a_hole_was_not_seen(self):
        sink, *_ = play(nine_holes(skip=(4,), end=26, then=self.NEXT_TEE))
        self.assertNotIn(9, [int(r[2]) for r in sink.sheets["SG_Holes"][1:]],
                         "strokes before hole 9 unknown: not guessed")


class RoundCheck(unittest.TestCase):
    """T-35 (v3.8.6): every GSPro round is checked as it ends."""

    NEXT_TEE = EndOfRoundScore.NEXT_TEE

    def logged(self, caps, end=False):
        import logging
        logging.disable(logging.NOTSET)                  # this suite silences logging
        try:
            with self.assertLogs("scoring", level="INFO") as cm:
                play(caps, end=end)
        finally:
            logging.disable(logging.CRITICAL)
        return [m.split(":", 2)[2] for m in cm.output]

    def rounds(self, lines):
        return [l for l in lines if "━━━  Round" in l or "not recorded:" in l]

    def test_complete_round_matches_gspro(self):
        out = self.rounds(self.logged(nine_holes(end=26, then=self.NEXT_TEE)))
        self.assertEqual(len(out), 1, out)
        self.assertIn("Round complete — 9 of 9 holes recorded, 26 strokes, matching GSPro's 26", out[0])

    def test_a_lost_hole_is_named_with_its_reason(self):
        caps = nine_holes(end=26, then=self.NEXT_TEE)
        tee5 = next(c for c in caps if c.get("B1") == "Hole 5" and c.get("V1") == "1")
        tee5["U1"] = ""                                   # round score unreadable at hole 5's tee
        out = self.rounds(self.logged(caps))
        self.assertIn("8 of 9 holes recorded", out[0])
        self.assertIn("(GSPro's total: 26)", out[0])
        self.assertIn("hole 4 — round score unreadable", out[1])

    def test_round_ended_without_the_end_screen(self):
        out = self.rounds(self.logged(nine_holes(then=self.NEXT_TEE)))
        self.assertIn("Round ended — 8 of 9 holes recorded", out[0])
        self.assertIn("hole 9 — the round ended on it and GSPro's end-of-round score was not read", out[1])

    def test_exit_mid_round_is_an_interim_report(self):
        out = self.rounds(self.logged(nine_holes(), end=True))
        self.assertIn("Round so far — 8 of 9 holes recorded", out[0])
        self.assertIn("hole 9 — the session ended before it finished", out[1])

    def test_strokes_that_do_not_add_up_are_flagged(self):
        r = {"round_pars": {str(h): 4 for h in range(1, 10)},
             "round_log": {str(h): {"status": "recorded", "strokes": 3, "reason": ""} for h in range(1, 10)}}
        lines, bad = scoring._round_summary(r, end_total=28)
        self.assertTrue(bad)
        self.assertIn("27 strokes — but GSPro shows 28", lines[0])

    def test_reported_once_even_if_the_end_screen_is_seen_again(self):
        caps = nine_holes(end=26)
        caps.insert(len(caps), {"W1": "26", "W2": "SCORE"})
        caps.append(self.NEXT_TEE)
        out = [l for l in self.rounds(self.logged(caps)) if "━━━" in l]
        self.assertEqual(len(out), 1)

    def test_live_warning_once_per_run_of_failures(self):
        caps = [capture(1, "25.0yd", "Tee", 1, yd=25, score="E")]
        caps += [capture(1, f"{20 - i}ft 0", "Green", 2 + i, yd=25, score="", club="Putter") for i in range(5)]
        warns = [l for l in self.logged(caps) if "Round score not readable" in l]
        self.assertEqual(len(warns), 1, "after 3 in a row, not repeated")


class LiePenalty(unittest.TestCase):
    """T-08 (v3.8.10): speed and spin kept are credited to the shot from the penalising lie."""

    def shots(self):
        caps = [capture(1, "150.0yd", "Tee", 1, score="E"),
                capture(1, "60.0yd", "Deep", 2, score="E"),
                dict(capture(1, "25ft 0", "Green", 3, score="E", club="Putter"), X1="80%", Y1="53%"),
                dict(capture(1, "3ft 0", "Green", 4, score="E", club="Putter"), X1="80%", Y1="53%")]
        sink, _, _ = play(caps)
        head = sink.sheets["SG_Shots"][0]
        return [dict(zip(head, r)) for r in sink.sheets["SG_Shots"][1:]], sink

    def test_credited_to_the_shot_from_the_penalising_lie_only(self):
        rows, _ = self.shots()
        kept = [(r["Pre-Lie"], r["Speed Kept %"], r["Spin Kept %"]) for r in rows]
        self.assertEqual(kept[1][1:], ("80", "53"), kept)
        self.assertNotEqual(kept[1][0], scoring.sg_mod.LIE_GREEN)
        self.assertEqual(kept[2], (scoring.sg_mod.LIE_GREEN, "", ""), "the panel lingering through a putt is not credited")
        self.assertEqual(kept[0][1:], ("", ""), "no panel, no penalty")

    def test_percent_parsing(self):
        import parsing
        self.assertEqual([parsing._percent(s) for s in ("80%", " 53 % ", "100%", "0%", "180%", "", "8O%")],
                         [80, 53, 100, None, None, None, None])

    def test_round_stats_table_only_with_readings(self):
        import reports
        rows, sink = self.shots()
        table = reports._lie_penalty_rows(sink.sheets["SG_Shots"])
        self.assertTrue(table[1][0].startswith("LIE PENALTY"))
        self.assertEqual(table[2][1:4], ["1", "80", "53"])
        bare = [r[:-2] for r in sink.sheets["SG_Shots"]]
        self.assertEqual(reports._lie_penalty_rows(bare), [], "older reports keep their layout")

    def test_format_6_upgrade_adds_the_columns(self):
        import ods_upgrade
        old_head = scoring._SG_SHOTS_HEADERS[:-2]
        data = {"SG_Shots": [list(old_head), ["2026-10-04"] + [""] * (len(old_head) - 1)]}
        notes = ods_upgrade.MIGRATIONS[5](data, {"headers": {"SG_Shots": scoring._SG_SHOTS_HEADERS}})
        self.assertEqual(data["SG_Shots"][0], scoring._SG_SHOTS_HEADERS)
        self.assertEqual(data["SG_Shots"][1][-2:], ["", ""])
        self.assertTrue(notes and "Speed Kept %" in notes[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
