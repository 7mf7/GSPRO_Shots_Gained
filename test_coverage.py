#!/usr/bin/env python3
"""
test_coverage.py — Capture coverage beside every strokes-gained figure (T-18, v3.8.12).

    python test_coverage.py
"""
import copy
import logging
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
import dashboard                       # noqa: E402
import reports                         # noqa: E402
import test_engine as te               # noqa: E402


def quality(captured, conceded=0, estimated=0, recovered=0):
    return {"strokes_captured": captured, "strokes_conceded": conceded,
            "strokes_uncaptured": estimated, "strokes_recovered": recovered}


def session(date, course, holes, q, sg_total=-2.0):
    return {"date": date, "week": "", "course": course, "holes": holes, "shots": holes * 4,
            "par": holes * 4, "vs_par": 0, "pro_baseline": holes * 3.9, "vs_pro": 0.0,
            "sg": {c: 0.0 for c in reports.sg_mod.SG_CATS}, "sg_total": sg_total,
            "duration_min": 60, "quality": q}


SESSIONS = [session("2026-09-14", "Rock Bay Golf", 18, quality(25, 6, 41)),       # 43%
            session("2026-09-21", "Central Park South", 9, quality(28, 8)),       # 100%
            session("2026-09-22", "Old Course", 9, {})]                           # before v3.1


def find(rows, start):
    return next(k for k, r in enumerate(rows) if r and str(r[0]).startswith(start))


class Calculation(unittest.TestCase):
    def test_label(self):
        self.assertEqual(reports.coverage_label(31, 72), "43% (low)")
        self.assertEqual(reports.coverage_label(80, 100), "80%")
        self.assertEqual(reports.coverage_label(999, 1000), "99%", "never rounds up to 100%")
        self.assertEqual(reports.coverage_label(5, 5), "100%")
        self.assertEqual(reports.coverage_label(0, 0), "—")

    def test_counts_known_strokes_and_sessions_without_counters(self):
        self.assertEqual(reports.coverage_counts(SESSIONS), (31 + 36, 72 + 36, 1))
        self.assertIsNone(reports.quality_counts({}))
        self.assertEqual(reports.quality_counts(quality(10, 2, 3, recovered=4)), (16, 19),
                         "a recovered final stroke counts as captured")

    def test_dashboard_uses_the_same_calculation(self):
        self.assertEqual(dashboard._coverage(SESSIONS), reports.coverage_counts(SESSIONS)[:2])


class RoundStats(unittest.TestCase):
    def setUp(self):
        _, state, _ = te.play(te.nine_holes(end=26, then=te.EndOfRoundScore.NEXT_TEE))
        self.r = state["round"]
        self.rows = reports.build_round_stats(self.r, copy.deepcopy(SESSIONS), "test")

    def table(self, start):
        i = find(self.rows, start)
        head = self.rows[i + (0 if start == "BY COURSE" else 1)]
        return head, i

    def test_session_log_and_career_total(self):
        head, i = self.table("SESSION LOG")
        c = head.index("Coverage")
        self.assertEqual([self.rows[i + 2 + k][c] for k in range(3)], ["43% (low)", "100%", "—"])
        total = self.rows[i + 5]
        self.assertEqual((total[0], total[c]), ("CAREER TOTAL", "62% (low)"))

    def test_by_course_shows_rock_bay_beside_its_numbers(self):
        head, i = self.table("BY COURSE")
        c = head.index("Coverage")
        by = {r[0]: r[c] for r in self.rows[i + 1:i + 4]}
        self.assertEqual(by["Rock Bay Golf"], "43% (low)")
        self.assertEqual(by["Old Course"], "—")

    def test_note_says_older_sessions_are_left_out(self):
        text = " ".join(str(r[0]) for r in self.rows if r)
        self.assertIn("Coverage = share of strokes", text)
        self.assertIn("1 session(s) from before v3.1", text)

    def test_this_session_row_sits_above_the_total(self):
        i = find(self.rows, "Capture coverage")
        self.assertTrue(self.rows[i + 1][0].startswith("Strokes gained total"))
        known, total = reports.quality_counts(self.r["quality"])
        self.assertEqual(self.rows[i][1], reports.coverage_label(known, total))

    def test_hole_by_hole_counts_estimated_strokes(self):
        # As on 8 Oct: hole 9's tee shot is captured, then the session ends on
        # GSPro's end screen, so its other strokes come from the total — estimated.
        _, state, _ = te.play(te.nine_holes(end=27), end=True)
        hist = {h["hole"]: h for h in state["round"]["hole_history"]}
        self.assertEqual((hist[1]["estimated"], hist[9]["estimated"]), (0, 2))
        rows = reports.build_round_stats(state["round"], [], "test")
        i = find(rows, "HOLE BY HOLE")
        c = rows[i + 1].index("Captured")
        by = {r[2]: r[c] for r in rows[i + 2:i + 11]}
        self.assertEqual((by["1"], by["9"]), ("3 of 3", "1 of 3"))


class SGSummary(unittest.TestCase):
    def test_week_coverage_beside_the_sample(self):
        _, state, _ = te.play(te.nine_holes(end=26))
        rows = reports.build_sg_summary(state["round"], copy.deepcopy(SESSIONS), "test")
        i = find(rows, "Week")
        s = rows[i].index("Sample")
        self.assertEqual(rows[i][s + 1], "Coverage")
        self.assertEqual([r[s + 1] for r in rows[i + 1:i + 3]], ["43% (low)", "100%"])
        self.assertTrue(any(r and r[0] == "Capture coverage" for r in rows))


class Clubs(unittest.TestCase):
    def test_clubs_state_their_coverage(self):
        sink, _, _ = te.play(te.nine_holes(end=26, then=te.EndOfRoundScore.NEXT_TEE))
        rows = reports.build_clubs(sink.sheets["SG_Shots"])
        line = next(r[0] for r in rows if r and str(r[0]).startswith("From captured shots only"))
        self.assertIn("Coverage:", line)


class Dashboard(unittest.TestCase):
    def test_legend_and_low_weeks(self):
        html = dashboard.build_dashboard(None, copy.deepcopy(SESSIONS), [])
        self.assertIn("coverage 100%", html)
        self.assertIn('class="sub low"', html, "the Rock Bay week is marked low")
        self.assertIn("43% captured", html)
        self.assertIn("coverage 43% (low)", html, "in the week's tooltip")


if __name__ == "__main__":
    unittest.main(verbosity=1)
