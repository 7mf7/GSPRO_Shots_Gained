#!/usr/bin/env python3
"""
test_dashboard.py — The HTML dashboard (dashboard.py, T-16).

    python test_dashboard.py
"""
import random
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import logging                       # noqa: E402
logging.disable(logging.CRITICAL)
import dashboard                     # noqa: E402
import reports                       # noqa: E402
import scoring                       # noqa: E402
import strokes_gained as sg          # noqa: E402

WHEN = datetime(2026, 10, 4, 18, 0)
BAG = [("7 Iron", 160, 5), ("8 Iron", 149, 5), ("9 Iron", 137, 5), ("P Wedge", 124, 4)]


def sample(weeks=4, seed=3):
    """Archived sessions and SG_Shots rows over several weeks."""
    rnd, head = random.Random(seed), scoring._SG_SHOTS_HEADERS
    sessions, rows = [], [head]
    for w in range(weeks):
        d = date(2026, 9, 1) + timedelta(days=7 * w)
        cats = {c: round(rnd.uniform(-2, 1), 3) for c in sg.SG_CATS}
        sessions.append({"round_id": f"r{w}", "date": d.isoformat(),
                         "week": f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}",
                         "course": "Rock Bay <Links> & Co", "holes": 9, "shots": 40, "par": 36,
                         "vs_par": 4, "sg": cats, "sg_total": round(sum(cats.values()), 3),
                         "quality": {"strokes_captured": 30, "strokes_recovered": 5,
                                     "strokes_conceded": 3, "strokes_uncaptured": 2}})
        for i in range(12):
            club, carry, spread = rnd.choice(BAG)
            c, pre = rnd.gauss(carry, spread), carry
            row = [""] * len(head)
            for k, v in {"Date": d.isoformat(), "Time": f"10:{i:02d}:00", "Hole": str(i + 1),
                         "SG Category": sg.CAT_APP, "Club": club, "Pre-Dist": f"{pre:.1f}",
                         "Pre Unit": "yd", "SG": f"{rnd.gauss(0, .3):+.3f}", "Carry Raw": f"{c:.1f}",
                         "Total Length": f"{c + 6:.1f}", "Ball Speed": f"{c * .9 + 40:.1f}",
                         "Offline": f"{abs(rnd.gauss(0, 4)):.1f}", "Short/Long": f"{rnd.gauss(2, 4):+.1f}",
                         "Geom Unit": "yd"}.items():
                row[head.index(k)] = v
            rows.append(row)
    return sessions, rows


class Page(unittest.TestCase):
    def setUp(self):
        self.sessions, self.rows = sample()
        self.page = dashboard.build_dashboard(None, self.sessions, self.rows, "test", WHEN)

    def test_self_contained(self):
        p = self.page
        for bad in ("<script", "<link", "<img", "src=", "url(", "@import"):
            self.assertNotIn(bad, p, bad)
        refs = set(re.findall(r"https?://[^\"' )]+", p))
        self.assertEqual(refs, {"http://www.w3.org/2000/svg"}, "only the SVG namespace, never fetched")

    def test_weekly_figures_match_the_report(self):
        for label, w in reports._aggregate_weeks(self.sessions).items():
            for c in sg.ALL_CATS:
                expect = dashboard._signed(w["sg"][c] / w["holes"])
                self.assertIn(f"{label} · {c}: {expect} per hole", self.page)

    def test_gapping_matches_the_clubs_sheet(self):
        shots, _ = reports._shots_from_rows(self.rows)
        stats, _ = reports.gapping_stats(shots)
        sheet = reports.build_clubs(self.rows)
        for st in stats:
            row = next(r for r in sheet if r and r[0] == st["club"])
            self.assertEqual(row[2], f"{st['median']:.1f}")               # the sheet's carry median
            self.assertIn(f"{st['club']}: median carry {st['median']:.1f} yd", self.page)

    def test_every_section_present(self):
        for h in ("Week by week", "By category", "Club gapping", "Dispersion by club"):
            self.assertIn(f"<h2>{h}</h2>", self.page)
        self.assertEqual(self.page.count('class="panel"'), len(BAG))

    def test_text_is_escaped(self):
        self.assertNotIn("<Links>", self.page)
        self.assertIn("Rock Bay &lt;Links&gt; &amp; Co", self.page)

    def test_same_inputs_same_page(self):
        self.assertEqual(self.page, dashboard.build_dashboard(None, self.sessions, self.rows, "test", WHEN))


class EdgeCases(unittest.TestCase):
    def test_no_data_at_all(self):
        p = dashboard.build_dashboard(None, [], [], "", WHEN)
        self.assertIn("No completed holes yet.", p)
        self.assertIn("No full-swing shots with a club yet.", p)
        self.assertTrue(p.startswith("<!doctype html>") and p.endswith("</html>"))

    def test_one_week_says_trends_need_more(self):
        s, r = sample(weeks=1)
        self.assertIn("Trend lines appear from the second week", dashboard.build_dashboard(None, s, r, "", WHEN))

    def test_archived_session_is_not_counted_twice(self):
        s, _ = sample(weeks=2)
        live = dict(s[-1])
        rounds = dashboard._rounds({"fake": True}, s)
        self.assertEqual(len(rounds), 2)
        orig = scoring.build_session_entry
        try:
            scoring.build_session_entry = lambda r: dict(live)               # same round_id
            self.assertEqual(len(dashboard._rounds({"x": 1}, s)), 2)
            scoring.build_session_entry = lambda r: dict(live, round_id="new")
            self.assertEqual(len(dashboard._rounds({"x": 1}, s)), 3)
        finally:
            scoring.build_session_entry = orig

    def test_zero_is_not_signed(self):
        self.assertEqual((dashboard._signed(-0.0004), dashboard._signed(0.25), dashboard._signed(-0.25)),
                         ("0.00", "+0.25", "−0.25"))


class InTheDaemon(unittest.TestCase):
    def setUp(self):
        import test_capture as tc
        self.od = od = tc._import_daemon()
        import db
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_dash_"))
        self.saved = (od.SCRIPT_DIR, od._DB, od._ods_save)
        od.SCRIPT_DIR = self.dir
        od._DB = None
        od._open_database({"db_file": str(self.dir / "t.db")})
        self.db = db

    def tearDown(self):
        od = self.od
        if od._DB is not None:
            od._DB.close()
        od.SCRIPT_DIR, od._DB, od._ods_save = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_written_even_when_the_report_is_locked(self):
        def locked(path, sheets):
            raise self.od.storage.FileLockedError(str(path))
        self.od._ods_save = locked
        self.od.export_report({"ods_file": str(self.dir / "r.ods")})
        page = (self.dir / "gspro_dashboard.html").read_text(encoding="utf-8")
        self.assertIn("<h1>Strokes gained dashboard</h1>", page)

    def test_can_be_turned_off(self):
        self.assertIsNone(self.od.write_dashboard({"dashboard": False}))
        self.assertFalse((self.dir / "gspro_dashboard.html").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
