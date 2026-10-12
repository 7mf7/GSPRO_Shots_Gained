#!/usr/bin/env python3
"""
test_sides.py — Left/right in the dispersion plot (T-31, v3.8.13).

    python test_sides.py
"""
import json
import logging
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
from PIL import Image, ImageDraw          # noqa: E402
import dashboard                          # noqa: E402
import reports                            # noqa: E402
import verify_sides                       # noqa: E402

VERIFIED = json.load(open(HERE / "test_fixtures" / "shot_sides.json"))["shots"]


class Estimate(unittest.TestCase):
    def test_every_firm_estimate_matches_the_screen(self):
        firm = 0
        for s in VERIFIED:
            pred, side, is_firm = reports.estimate_side(s["hla"], s["axis"], s["total"])
            if is_firm:
                firm += 1
                self.assertEqual(side, s["seen"], s["frame"])
        self.assertEqual(firm, 7)

    def test_positive_means_right_and_axis_bends_with_hla(self):
        self.assertEqual(reports.estimate_side(+3.0, 0.0, 100)[1], +1)
        self.assertEqual(reports.estimate_side(-3.0, 0.0, 100)[1], -1)
        self.assertEqual(reports.estimate_side(0.0, +10.0, 100)[1], +1, "positive axis curves right")
        self.assertEqual(reports.estimate_side(+1.0, -10.0, 100)[1], -1, "curve can outweigh the start line")

    def test_firmness(self):
        self.assertFalse(reports.estimate_side(0.1, -1.0, 30)[2], "a predicted miss under 0.5 yd")
        self.assertFalse(reports.estimate_side(5.0, 0.0, 30, unit="ft")[2], "putts: start line only")
        self.assertEqual(reports.estimate_side(None, 0.0, 30), (None, 0, False))
        self.assertEqual(reports.estimate_side(2.0, 0.0, None), (None, 0, False))

    def test_shot_rows_carry_the_side(self):
        head = ["Date", "Time", "Hole", "Club", "Pre Unit", "SG", "Total Length", "Short/Long",
                "Geom Unit", "Offline", "HLA", "Spin Axis", "Note"]
        rows = [head, ["2026-10-04", "19:24:21", "6", "L Wedge", "yd", "-0.1", "43.3", "-2.0", "yd", "2.0",
                       "-0.5", "-11.8", ""]]
        s = reports._shots_from_rows(rows)[0][0]
        self.assertEqual((s["side"], s["side_firm"]), (-1, True))
        self.assertAlmostEqual(s["side_pred"], -2.9, places=1)


class Plot(unittest.TestCase):
    def shot(self, side, firm, offline=2.0, bias=-1.0, unit="yd", hla=1.0, axis=-3.0):
        return {"club": "L Wedge", "date": "2026-10-04", "time": "19:00:00", "hole": "1", "bias": bias,
                "offline": offline, "bias_unit": unit, "side": side, "side_firm": firm,
                "hla": hla, "axis": axis, "side_pred": side * offline}

    def test_left_and_right_of_the_line(self):
        svg = dashboard._panel("L Wedge", [self.shot(-1, True), self.shot(+1, True), self.shot(+1, False, 0.3)], "yd")
        centre = float(re.search(r'<line class="zero" x1="([\d.]+)" x2="\1" ', svg).group(1))
        xs = [float(x) for x in re.findall(r'<circle class="pt[^"]*" cx="([\d.]+)"', svg)]
        self.assertLess(xs[0], centre)
        self.assertGreater(xs[1], centre)
        self.assertIn('class="pt open"', svg, "too close to call: hollow")
        self.assertIn("1 left, 1 right, 1 too close to call", svg)
        self.assertIn("← left", svg)
        self.assertIn("right →", svg)

    def test_tooltips_say_it_is_an_estimate(self):
        svg = dashboard._panel("L Wedge", [self.shot(-1, True), self.shot(-1, True), self.shot(-1, True)], "yd")
        self.assertIn("left (estimated from HLA +1.0°, spin axis -3.0°)", svg)

    def test_putts_are_start_line_only(self):
        svg = dashboard._panel("Putter", [self.shot(+1, False, unit="ft", axis=0.0)] * 3, "ft")
        self.assertIn("start line only", svg)
        self.assertNotIn('class="pt"', svg.replace('class="pt open"', ""))


class ScreenCheck(unittest.TestCase):
    """verify_sides.py reads the side from GSPro's ball-camera view."""

    def frame(self, pole_x=None, ball_box=True):
        im = Image.new("RGB", (1920, 1080), (52, 84, 38))                   # grass
        d = ImageDraw.Draw(im)
        if ball_box:
            d.rectangle([968, 800, 1067, 847], fill=(30, 42, 66))           # the ball's distance box
        if pole_x is not None:
            d.line([(pole_x + 12, 200), (pole_x, 440)], fill=(230, 230, 232), width=4)   # leaning flagstick
        return im

    def test_pin_left_of_centre_means_ball_right(self):
        self.assertEqual(verify_sides.side_seen(self.frame(700))[0], +1)
        self.assertEqual(verify_sides.side_seen(self.frame(1250))[0], -1)
        self.assertEqual(verify_sides.side_seen(self.frame(958))[0], 0)

    def test_address_view_and_missing_pin_say_nothing(self):
        self.assertIsNone(verify_sides.side_seen(self.frame(700, ball_box=False))[0])
        self.assertIsNone(verify_sides.side_seen(self.frame(None))[0])

    def test_frame_matched_to_the_shot_recorded_next(self):
        shots = [{"date": "2026-10-04", "time": "19:24:21", "hla": -0.5, "axis": -11.8},
                 {"date": "2026-10-04", "time": "19:40:00", "hla": -0.5, "axis": -11.8}]
        s = verify_sides.match_shot(shots, "2026-10-04", datetime(2026, 10, 4, 19, 24, 17), -0.5, -11.8)
        self.assertEqual(s["time"], "19:24:21")
        self.assertIsNone(verify_sides.match_shot(shots, "2026-10-04", datetime(2026, 10, 4, 19, 24, 17), 1.0, -11.8))


if __name__ == "__main__":
    unittest.main(verbosity=1)
