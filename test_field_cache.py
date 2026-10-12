#!/usr/bin/env python3
"""
test_field_cache.py — Re-reading HUD fields only when their pixels change
(field_cache.py, T-13).

    python test_field_cache.py
"""
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
import numpy as np                    # noqa: E402
from PIL import Image                 # noqa: E402
import field_cache                    # noqa: E402

FIX = HERE / "test_fixtures"


def fx(cell, frame):
    return Image.open(FIX / f"{cell}_{frame}.png").convert("RGB")


class Cache(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.c = field_cache.FieldCache(["A1", "B1"], clock=lambda: self.now[0])

    def test_unchanged_pixels_reuse_the_reading(self):
        img = fx("B1", "r2_13_04_26")                         # "Hole 8"
        self.c.store("B1", img, "Hole8")
        self.assertEqual(self.c.lookup("B1", img.copy()), "Hole8")

    def test_a_changed_character_is_always_read_again(self):
        self.c.store("B1", fx("B1", "r2_13_04_26"), "Hole8")
        self.assertIsNone(self.c.lookup("B1", fx("B1", "r2_13_04_57")), "Hole 9: not reused")

    def test_one_changed_pixel_means_a_fresh_read(self):
        img = fx("B1", "r2_13_04_26")
        self.c.store("B1", img, "Hole8")
        a = np.asarray(img).copy()
        a[5, 5] = 255 - a[5, 5]
        self.assertIsNone(self.c.lookup("B1", Image.fromarray(a)))

    def test_a_few_slightly_different_pixels_are_tolerated(self):
        img = fx("B1", "r2_13_04_26")
        self.c.store("B1", img, "Hole8")
        a = np.asarray(img).astype(np.int16).copy()
        a[:2, :5] += 5                                        # 10 pixels, 5 levels
        self.assertEqual(self.c.lookup("B1", Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))), "Hole8")

    def test_the_whole_region_dimming_is_a_fresh_read(self):
        """e.g. GSPro's end-of-round overlay dims the HUD: never reuse across it."""
        img = fx("B1", "r2_13_04_26")
        self.c.store("B1", img, "Hole8")
        a = np.asarray(img).astype(np.int16) + 3
        self.assertIsNone(self.c.lookup("B1", Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))))

    def test_a_different_size_is_a_fresh_read(self):
        img = fx("B1", "r2_13_04_26")
        self.c.store("B1", img, "Hole8")
        self.assertIsNone(self.c.lookup("B1", img.crop((0, 0, img.width - 1, img.height))))

    def test_entries_expire(self):
        img = fx("B1", "r2_13_04_26")
        self.c.store("B1", img, "Hole8")
        self.now[0] = 601.0
        self.assertIsNone(self.c.lookup("B1", img))

    def test_only_listed_fields_and_never_an_empty_reading(self):
        img = fx("B1", "r2_13_04_26")
        self.c.store("K1", img, "25.0yd")
        self.assertIsNone(self.c.lookup("K1", img), "remaining distance is not cached")
        self.c.store("A1", img, "")
        self.assertIsNone(self.c.lookup("A1", img), "an unreadable field is retried")

    def test_summary_counts_reuse(self):
        img = fx("B1", "r2_13_04_26")
        self.c.lookup("B1", img)
        self.c.store("B1", img, "Hole8")
        self.c.lookup("B1", img)
        self.assertEqual(self.c.summary(), "reused unchanged readings: B1 1/2")


class InTheDaemon(unittest.TestCase):
    def setUp(self):
        import test_capture as tc
        od = self.od = tc._import_daemon()
        self.tc = tc
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_cache_"))
        self.desktop = Image.new("RGB", (3840, 1080), (20, 30, 40))
        self.desktop.paste(fx("A1", "r2_13_04_26") if (FIX / "A1_r2_13_04_26.png").exists()
                           else Image.new("RGB", (175, 21), (240, 240, 240)), (3543, 30))
        self.desktop.paste(fx("B1", "r2_13_04_26"), (3544, 67))
        self.saved = (od.screen_capture.get_sct, od.SCRIPT_DIR, od._DB, od._FIELDS, od.ocr_reader.ocr_image,
                      od._screenshots_on, od.screen_capture.window_x_offset)
        od.screen_capture.get_sct = lambda: tc.FakeSct(self.desktop)
        od.SCRIPT_DIR, od._DB, od._FIELDS, od._screenshots_on, od.screen_capture.window_x_offset = \
            self.dir, None, None, False, 0
        od._open_database({"db_file": str(self.dir / "t.db")})
        self.calls = []
        od.ocr_reader.ocr_image = lambda region, img, lang, save_debug=False, **kw: (
            self.calls.append(region["cell"]) or {"A1": "Central Park South", "B1": "Hole8"}[region["cell"]], img)
        self.cfg = {"regions": [{"cell": "A1", "name": "course", "x": 3543, "y": 30, "width": 175, "height": 21},
                                {"cell": "B1", "name": "hole", "x": 3544, "y": 67, "width": 49, "height": 21}],
                    "db_file": str(self.dir / "t.db"), "report_refresh": "exit", "ocr_workers": 1}

    def tearDown(self):
        od = self.od
        if od._DB is not None:
            od._DB.close()
        (od.screen_capture.get_sct, od.SCRIPT_DIR, od._DB, od._FIELDS, od.ocr_reader.ocr_image,
         od._screenshots_on, od.screen_capture.window_x_offset) = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unchanged_fields_are_read_once(self):
        self.od.run_pipeline(self.cfg, source="auto")
        self.od.run_pipeline(self.cfg, source="auto")
        self.assertEqual(sorted(self.calls), ["A1", "B1"], "second capture reused both")

    def test_a_changed_field_is_read_again(self):
        self.od.run_pipeline(self.cfg, source="auto")
        self.desktop.paste(fx("B1", "r2_13_04_57"), (3544, 67))      # now "Hole 9"
        self.od.run_pipeline(self.cfg, source="auto")
        self.assertEqual(sorted(self.calls), ["A1", "B1", "B1"])

    def test_off_when_disabled(self):
        cfg = dict(self.cfg, ocr_cache=False)
        self.od.run_pipeline(cfg, source="auto")
        self.od.run_pipeline(cfg, source="auto")
        self.assertEqual(len(self.calls), 4)


if __name__ == "__main__":
    unittest.main(verbosity=1)
