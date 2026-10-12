#!/usr/bin/env python3
"""
test_rebuild_screenshots.py — Re-reading saved screenshots (rebuild_from_screenshots.py, T-23).

    python test_rebuild_screenshots.py
"""
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
from PIL import Image                       # noqa: E402
import rebuild_from_log as rb               # noqa: E402
import rebuild_from_screenshots as rs       # noqa: E402
import ocr_reader                           # noqa: E402
import test_engine as te                    # noqa: E402

T0 = datetime(2026, 10, 5, 12, 0, 0)


class Frames(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_shots_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def touch(self, name):
        (self.dir / name).write_bytes(b"x")

    def test_captures_are_found_classified_and_ordered(self):
        for n in ("2026-10-05_12-00-05-100_auto_h1_s1_accepted.png",
                  "2026-10-05_12-00-01-000_settle_skipped.png",
                  "2026-10-05_12-00-09-000_settle_cooldown.png",
                  "2026-10-05_12-00-10-000_manual.png",
                  "2026-10-05_12-00-20-000_manual_h1_s2_accepted.jpg",
                  "2026-10-05_12-00-30-000_auto_hx_sx_invalid.png",
                  "2026-10-05_12-00-40-000_auto_h2_s1_no_progression.png",
                  "notes.png"):
            self.touch(n)
        frames, counts = rs.collect_frames([self.dir])
        self.assertEqual([(f.kind, f.live) for f in frames],
                         [("skipped", "skipped"), ("auto", "accepted"), ("manual", "accepted"),
                          ("auto", "invalid"), ("auto", "no_progression")])
        self.assertEqual((counts["cooldown"], counts["not a capture"]), (1, 1))
        self.assertEqual(frames[1].ts, datetime(2026, 10, 5, 12, 0, 5, 100000))

    def test_log_captures_inside_a_sitting_are_replaced(self):
        frames = [rs.FrameCapture(ts=T0), rs.FrameCapture(ts=T0 + timedelta(minutes=20)),
                  rs.FrameCapture(ts=T0 + timedelta(hours=2))]
        logged = [rb.Capture(ts=T0 + timedelta(minutes=m)) for m in (-2, 10, 21.5, 60, 120.5)]
        merged, replaced = rs.merge(logged, frames)
        kept = [c.ts for c in merged if not isinstance(c, rs.FrameCapture)]
        self.assertEqual(replaced, 2)
        self.assertEqual(kept, [T0 - timedelta(minutes=2), T0 + timedelta(minutes=21.5),
                                T0 + timedelta(minutes=60)])
        self.assertEqual([c.ts for c in merged], sorted(c.ts for c in merged))


class CorrectedLog(unittest.TestCase):
    def test_round_trip_through_parse_log(self):
        d = Path(tempfile.mkdtemp(prefix="gspro_log_"))
        self.addCleanup(shutil.rmtree, d, True)
        cap = rs.FrameCapture(ts=T0, reads=[("hole", "B1", "Hole 3", ""),
                                            ("stroke ticker", "V1", "2", "(stroke ticker, par=4)"),
                                            ("remaining", "K1", "12' 4\"", "(unchanged)")])
        cap.by_cell = {c: t for _, c, t, _ in cap.reads}
        skipped = rs.FrameCapture(ts=T0 + timedelta(seconds=5))            # pre-check skipped it
        n = rs.write_log(d / "c.log", [cap, skipped], {"regions": []})
        back = rb.parse_log(d / "c.log")
        self.assertEqual(n, 1)
        self.assertEqual((back[0].ts, back[0].by_cell), (T0, cap.by_cell))


class PreCheckOnFrames(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_pre_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        Image.new("RGB", (1920, 1080)).save(self.dir / "f.png")
        cfg = {"regions": [{"cell": "B1", "name": "hole", "x": 3544, "y": 67, "width": 49, "height": 21},
                           {"cell": "V1", "name": "ticker", "x": 3500, "y": 900, "width": 300, "height": 40}],
               "ocr_cache": False}
        self.reader = rs.FrameReader(cfg)
        self.saved = (ocr_reader.ocr_image, ocr_reader.read_frame, ocr_reader._ARROW_OK)
        self.hole_reads = []
        ocr_reader.ocr_image = lambda region, img, lang, save_debug=False, **kw: (
            self.hole_reads.append(1) or ("Hole 3", img))
        ocr_reader.read_frame = lambda *a, **k: ([("hole", "B1", "Hole 3", "")], [], 0, 0.0, 1)
        ocr_reader._ARROW_OK = True
        self.saved_ticker = getattr(ocr_reader._arrow, "detect_stroke_ticker")
        self.ticker = 2
        ocr_reader._arrow.detect_stroke_ticker = lambda img, hole_par=5, debug_path=None: self.ticker
        self.state = {"last_capture": {"hole": 3, "ticker": 2}, "round": {"hole_par": 4}}

    def tearDown(self):
        ocr_reader.ocr_image, ocr_reader.read_frame, ocr_reader._ARROW_OK = self.saved
        ocr_reader._arrow.detect_stroke_ticker = self.saved_ticker

    def frame(self, kind):
        return rs.FrameCapture(ts=T0, path=self.dir / "f.png", kind=kind)

    def test_unchanged_hole_and_stroke_is_skipped(self):
        self.assertFalse(self.reader.prepare(self.frame("auto"), self.state))

    def test_a_new_stroke_is_read(self):
        self.ticker = 3
        cap = self.frame("skipped")
        self.assertTrue(self.reader.prepare(cap, self.state))
        self.assertEqual(cap.by_cell, {"B1": "Hole 3"})

    def test_manual_captures_have_no_pre_check(self):
        self.assertTrue(self.reader.prepare(self.frame("manual"), self.state))
        self.assertEqual(self.hole_reads, [], "the live hotkey capture runs no pre-check")


class ReplayThroughTheHook(unittest.TestCase):
    def test_frames_are_scored_by_the_shared_engine(self):
        script = te.nine_holes()[:6]                        # holes 1-2 and the next tee
        caps = []
        for i, by_cell in enumerate(script):
            c = rs.FrameCapture(ts=T0 + timedelta(seconds=20 * i), kind="manual")
            c.prepare = (lambda state, c=c, b=by_cell: (setattr(c, "by_cell", dict(b)) or True))
            caps.append(c)
        sheets, history, st = rb.replay(caps, te.CONFIG)
        self.assertGreaterEqual(st["holes"], 1)
        self.assertEqual(st["prechecked"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
