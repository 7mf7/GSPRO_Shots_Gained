#!/usr/bin/env python3
"""
test_capture.py — Capture-side tests: the stroke ticker and elevation readers
on REAL GSPro frames, the auto-capture timing rules, and monitor selection.

Run:  python test_capture.py
The elevation tests need Tesseract (as the daemon does) and skip without it.
"""
from __future__ import annotations

import json
import logging
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)

from PIL import Image                      # noqa: E402
import arrow_detector as ad               # noqa: E402
import shot_watcher                       # noqa: E402

FIX = HERE / "test_fixtures"
FRAMES = json.loads((FIX / "frames.json").read_text())["frames"]


def _have_tesseract() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


class RealFrames(unittest.TestCase):
    """Seven frames from a real session, with the values visible on screen."""

    def test_stroke_ticker(self):
        got = [ad.detect_stroke_ticker(Image.open(FIX / f"V1_{f['frame']}.png").convert("RGB"),
                                       hole_par=4) for f in FRAMES]
        self.assertEqual(got, [f["V1"] for f in FRAMES],
                         "regression: strokes 1-2 once read one too high")

    def test_stroke_ticker_with_stale_par(self):
        """At a new hole the daemon may still pass the previous hole's par."""
        got = [ad.detect_stroke_ticker(Image.open(FIX / f"V1_{f['frame']}.png").convert("RGB"),
                                       hole_par=5) for f in FRAMES]
        self.assertEqual(got, [f["V1"] for f in FRAMES])

    @unittest.skipUnless(_have_tesseract(), "Tesseract not installed")
    def test_elevation(self):
        got = [ad.detect_height_delta(Image.open(FIX / f"F1_{f['frame']}.png").convert("RGB"))
               for f in FRAMES]
        self.assertEqual(got, [f["F1"] for f in FRAMES],
                         "regression: the sign came from colour and only inches were read")


class FakeTime:
    def __init__(self):
        self.t = 100.0
    def sleep(self, d):
        self.t += d
    def monotonic(self):
        return self.t


class WatcherTiming(unittest.TestCase):
    CFG = {"regions": [{"cell": "V1", "x": 0, "y": 0, "width": 16, "height": 16}],
           "auto_cooldown_sec": 4.0, "auto_poll_interval": 0.25, "auto_stable_polls": 6}

    def run_frames(self, levels, pre_check):
        frames = [Image.new("L", (16, 16), v).convert("RGB") for v in levels]
        calls = []
        w = shot_watcher.ShotWatcher(self.CFG, pipeline_fn=lambda: calls.append(1),
                                     grab_fn=None, pre_check_fn=pre_check)
        def grab(_region):
            if not frames:
                w._stop_evt.set()
                return Image.new("L", (16, 16), levels[-1]).convert("RGB")
            return frames.pop(0)
        w._grab_fn = grab
        real_time, shot_watcher.time = shot_watcher.time, FakeTime()
        try:
            w._loop()
        finally:
            shot_watcher.time = real_time
        return len(calls)

    def test_counter_update_after_a_skip_is_captured(self):
        """
        Regression: the screen settles a moment before GSPro updates the stroke
        counter, so the pre-check skips; the counter's update 2.25 s later used
        to fall inside a cooldown started BY THE SKIP and was never captured.
        """
        answers = iter([False, True])
        levels = [40] + [200] * 7 + [200] * 2 + [40] * 7
        self.assertEqual(self.run_frames(levels, lambda: next(answers)), 1)

    def test_cooldown_still_applies_after_a_real_capture(self):
        levels = [40] + [200] * 7 + [200] * 2 + [40] * 7       # second settle 2.25 s later
        self.assertEqual(self.run_frames(levels, lambda: True), 1,
                         "the post-shot animation must not trigger a second capture")

    def test_decision_step(self):
        w = shot_watcher.ShotWatcher(self.CFG, pipeline_fn=lambda: None, grab_fn=None,
                                     pre_check_fn=lambda: False)
        self.assertEqual(w._on_settled(100.0), "skipped")
        self.assertEqual(w._last_fire, 0.0, "a skip must not start the cooldown")


def _import_daemon():
    """Import ocr_daemon, stubbing display-only modules where unavailable."""
    for name in ("mss",):
        try:
            __import__(name)
        except Exception:
            m = types.ModuleType(name); m.mss = lambda: None; sys.modules[name] = m
    try:
        __import__("pynput.keyboard")
    except Exception:
        p = types.ModuleType("pynput"); k = types.ModuleType("pynput.keyboard")
        class _G:
            def __init__(self, *a, **kw): pass
        k.GlobalHotKeys = _G; p.keyboard = k
        sys.modules["pynput"] = p; sys.modules["pynput.keyboard"] = k
    import ocr_daemon
    return ocr_daemon


class MonitorSelection(unittest.TestCase):
    def setUp(self):
        self.od = _import_daemon()
        import console_follow
        self.cf = console_follow
        self.saved = (console_follow.find_window_by_title, console_follow.get_monitor_rect,
                      self.od.screen_capture.window_x_offset)
        self.od.screen_capture._offset_checked_at = 0.0

    def tearDown(self):
        self.cf.find_window_by_title, self.cf.get_monitor_rect, self.od.screen_capture.window_x_offset = self.saved

    def test_offset_follows_gspro(self):
        self.assertEqual(self.od.screen_capture.compute_window_offset(0, 1920), -1920)
        self.assertEqual(self.od.screen_capture.compute_window_offset(1920, 1920), 0)
        self.cf.find_window_by_title = lambda pats: 42
        self.cf.get_monitor_rect = lambda h: (0, 0, 1920, 1080)          # left monitor
        self.od.screen_capture.window_x_offset = 0
        self.od.screen_capture.refresh_window_offset({"auto_window_offset": True, "window_x_shift": 1920})
        self.assertEqual(self.od.screen_capture.window_x_offset, -1920)

    def test_manual_setting_kept_when_gspro_not_found(self):
        self.cf.find_window_by_title = lambda pats: 0
        self.od.screen_capture.window_x_offset = -1920
        self.od.screen_capture.refresh_window_offset({"auto_window_offset": True})
        self.assertEqual(self.od.screen_capture.window_x_offset, -1920)


# ── Screenshots ────────────────────────────────────────────────────────────────

import shutil      # noqa: E402
import tempfile    # noqa: E402
from datetime import datetime   # noqa: E402
import screenshots              # noqa: E402

MONITORS = [{"left": 0, "top": 0, "width": 3840, "height": 1080},      # union (mss entry 0)
            {"left": 0, "top": 0, "width": 1920, "height": 1080},      # left monitor
            {"left": 1920, "top": 0, "width": 1920, "height": 1080}]   # right monitor
REGIONS = [{"x": 3544, "y": 67, "width": 49, "height": 21},
           {"x": 2203, "y": 60, "width": 124, "height": 40}]


class FakeSct:
    """mss stand-in: serves crops of one full-desktop image."""
    def __init__(self, desktop):
        self.monitors, self.desktop, self.grabs = MONITORS, desktop, []
    def grab(self, box):
        self.grabs.append(box)
        im = self.desktop.crop((box["left"], box["top"], box["left"] + box["width"],
                                box["top"] + box["height"]))
        r, g, b = im.split()
        return types.SimpleNamespace(size=im.size, bgra=Image.merge(
            "RGBA", (b, g, r, Image.new("L", im.size, 255))).tobytes())


class Screenshots(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_shots_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_picks_the_monitor_showing_the_regions(self):
        self.assertEqual(screenshots.pick_monitor(MONITORS, REGIONS, 0)["left"], 1920)
        self.assertEqual(screenshots.pick_monitor(MONITORS, REGIONS, -1920)["left"], 0,
                         "follows the window offset to the left monitor")

    def test_file_name(self):
        p = screenshots.screenshot_path(self.dir, datetime(2026, 10, 4, 9, 22, 30, 123456),
                                        "auto h2/s2 accepted")
        self.assertEqual(p.relative_to(self.dir).as_posix(),
                         "2026-10-04/2026-10-04_09-22-30-123_auto-h2-s2-accepted.png")

    def test_saver_writes_png_and_jpg(self):
        img = Image.new("RGB", (64, 36), (10, 120, 30))
        for fmt in ("png", "jpg"):
            s = screenshots.ScreenshotSaver(fmt)
            path = screenshots.screenshot_path(self.dir, datetime.now(), "t", s.fmt)
            self.assertTrue(s.submit(img, path))
            s.close()
            self.assertTrue(path.exists(), fmt)
            self.assertEqual(Image.open(path).size, (64, 36))

    def test_saver_drops_rather_than_blocks_when_behind(self):
        import queue
        s = screenshots.ScreenshotSaver("png", max_queue=1)
        s._q = queue.Queue(maxsize=1); s._q.put_nowait(("busy", Path("x")))
        self.assertFalse(s.submit(Image.new("RGB", (4, 4)), self.dir / "y.png"))
        self.assertEqual(s.dropped, 1)

    def test_watcher_reports_screens_it_does_not_capture(self):
        seen = []
        cfg = dict(WatcherTiming.CFG)
        w = shot_watcher.ShotWatcher(cfg, pipeline_fn=lambda: None, grab_fn=None,
                                     pre_check_fn=lambda: False, on_settle=seen.append)
        w._on_settled(100.0)                         # pre-check says nothing advanced
        w._pre_check_fn = lambda: True
        w._on_settled(101.0)                         # captures
        w._on_settled(102.0)                         # inside the cooldown
        self.assertEqual(seen, ["skipped", "cooldown"], "a real capture is not reported")


class ScreenshotsInTheDaemon(unittest.TestCase):
    def setUp(self):
        self.od = _import_daemon()
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_daemon_shots_"))
        desktop = Image.new("RGB", (3840, 1080), (20, 40, 20))
        self.sct = FakeSct(desktop)
        self.saved = (self.od.screen_capture.get_sct, self.od.SCRIPT_DIR, self.od._screenshots_on,
                      self.od._saver, self.od.screen_capture.window_x_offset, self.od._DB)
        self.od.screen_capture.get_sct = lambda: self.sct
        self.od.SCRIPT_DIR = self.dir
        self.od._saver = None
        self.od.screen_capture.window_x_offset = 0
        self.cfg = {"regions": [dict(r, cell=c, name=c) for r, c in zip(REGIONS, ("B1", "V1"))],
                    "screenshot_dir": str(self.dir / "shots"), "auto_window_offset": False,
                    "db_file": str(self.dir / "t.db"), "ods_file": str(self.dir / "r.ods"),
                    "report_refresh": "exit", "ocr_workers": 1}

    def tearDown(self):
        if self.od._saver is not None:
            self.od._saver.close()
        if self.od._DB is not None and self.od._DB is not self.saved[5]:
            self.od._DB.close()
        (self.od.screen_capture.get_sct, self.od.SCRIPT_DIR, self.od._screenshots_on,
         self.od._saver, self.od.screen_capture.window_x_offset, self.od._DB) = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def files(self):
        self.od._saver.close()
        return sorted((self.dir / "shots").rglob("*.png"))

    def test_hotkey_saves_the_whole_gspro_monitor(self):
        self.od._do_screenshot(self.cfg)
        f = self.files()
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0].name.endswith("_manual.png"))
        self.assertEqual(Image.open(f[0]).size, (1920, 1080), "the full monitor, not the regions")

    def test_every_capture_mode_saves_the_frame_the_ocr_read(self):
        od = self.od
        od._DB = None
        od._open_database(self.cfg)
        texts = {"B1": "Hole 2", "C1": "Par 4", "D1": "150 yds", "G1": "Tee",
                 "K1": "150.0yd"}
        saved_ocr, saved_tick = od.ocr_reader.ocr_image, od._arrow.detect_stroke_ticker
        od.ocr_reader.ocr_image = lambda region, img, lang, save_debug=False, **kw: (texts.get(region["cell"], ""), img)
        od._arrow.detect_stroke_ticker = lambda img, hole_par=5, debug_path=None: 1
        self.cfg["regions"] += [{"cell": c, "name": c, "x": 3544, "y": 100 + i * 30,
                                 "width": 40, "height": 20} for i, c in enumerate(("C1", "D1", "G1", "K1"))]
        try:
            od._set_screenshot_mode(self.cfg, True)
            od.run_pipeline(self.cfg, source="auto")
        finally:
            od.ocr_reader.ocr_image, od._arrow.detect_stroke_ticker = saved_ocr, saved_tick
        f = self.files()
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0].name.endswith("_auto_h2_s1_accepted.png"), f[0].name)
        self.assertEqual(len(self.sct.grabs), 1, "one grab: the screenshot IS the OCR frame")
        self.assertEqual((self.sct.grabs[0]["width"], self.sct.grabs[0]["height"]), (1920, 1080))

    def test_manual_capture_releases_its_capture_context(self):
        closed = []
        saved = self.od.screen_capture.close_sct
        self.od.screen_capture.close_sct = lambda: closed.append(1)
        try:
            self.od._then_release(lambda c: None, self.cfg)
        finally:
            self.od.screen_capture.close_sct = saved
        self.assertEqual(closed, [1])


# ── Character matching (digit_reader) ──────────────────────────────────────────

import digit_reader as dr   # noqa: E402

TEXT_FRAMES = [f for f in FRAMES if "text" in f]
ROUND2 = json.loads((FIX / "frames.json").read_text()).get("round2", [])   # a real 9-hole round


def fx(cell, frame):
    return Image.open(FIX / f"{cell}_{frame}.png").convert("RGB")


def frame_with(cell, needle):
    return next(f for f in TEXT_FRAMES if needle in f["text"][cell])


class CharacterMatching(unittest.TestCase):
    SEED = HERE / "digit_templates.seed.json"

    def setUp(self):
        self.saved_digits = dr.DIGITS
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_digits_"))

    def tearDown(self):
        dr.DIGITS = self.saved_digits
        shutil.rmtree(self.dir, ignore_errors=True)

    def store(self, gate=True):
        return dr.TemplateStore(self.SEED, self.dir / "learned.json", require_all_digits=gate)

    def test_every_field_of_every_real_frame(self):
        s = self.store(gate=False)
        for f in TEXT_FRAMES:
            for cell, truth in f["text"].items():
                got = s.read(cell, fx(cell, f["frame"]))
                self.assertEqual((got or "").replace(" ", ""), truth.replace(" ", ""),
                                 f"{cell} in {f['frame']}")

    def test_waits_until_a_font_has_all_ten_digits(self):
        s = self.store()
        self.assertEqual((s.missing_digits("panel"), s.missing_digits("hud")), ("", ""),
                         "the starter set covers both main fonts (v3.6.1)")
        self.assertEqual(s.missing_digits("score"), "", "complete since v3.8.1")
        f = ROUND2[1]
        self.assertEqual(s.read("L1", fx("L1", f["frame"])), "27.2")
        s.samples["panel"].pop("7")
        self.assertEqual(s.missing_digits("panel"), "7")
        self.assertIsNone(s.read("L1", fx("L1", f["frame"])),
                          "an incomplete font falls back to Tesseract")

    def test_every_field_of_a_real_round(self):
        s = self.store()                                  # ten-digit rule ON, as in play
        for f in ROUND2:
            for cell, truth in f["text"].items():
                got = s.read(cell, fx(cell, f["frame"]))
                if got is not None:
                    self.assertEqual(got.replace(" ", ""), truth.replace(" ", ""),
                                     f"{cell} in {f['frame']}")

    def test_values_misread_in_real_play(self):
        """Hole 8/9 (course-panel digits), '-4.5' (low minus sign), and long
        remaining distances that ran past the old K1 region."""
        s = self.store()
        for frame, cell, truth in [("r2_13_04_26", "B1", "Hole8"), ("r2_13_05_16", "B1", "Hole9"),
                                   ("r2_13_00_53", "R1", "-4.5"), ("r2_13_00_53", "K1", "25ft11"),
                                   ("r2_13_05_16", "K1", "9ft11"), ("r2_13_01_06", "K1", "8ft9")]:
            self.assertEqual((s.read(cell, fx(cell, frame)) or "").replace(" ", ""), truth, frame)

    def test_unseen_digit_is_never_guessed(self):
        """Regression: with no '9' on file, '398\' 4\"' read as '308\' 4\"'."""
        dr.DIGITS = set("012345689")                  # pretend 7 is known
        s = self.store()
        s.samples["panel"].pop("9")
        f = frame_with("P1", "398")
        self.assertIsNone(s.read("P1", fx("P1", f["frame"])), "missing digit: must not guess")
        self.assertEqual(self.store().read("P1", fx("P1", f["frame"])), "398' 4\"")

    def test_learning_completes_a_font(self):
        dr.DIGITS = set("012456789")                  # hud then needs only a 9
        s = self.store()
        s.samples["hud"].pop("9")
        f = frame_with("F1", "7ft 9")
        img = fx("F1", f["frame"])
        self.assertEqual(s.missing_digits("hud"), "9")
        s.learn("F1", img, "7ft9")                    # first sighting
        self.assertEqual(s.missing_digits("hud"), "9", "one sighting is not enough")
        s.learn("F1", img, "7ft9")                    # identical second sighting confirms
        self.assertEqual(s.missing_digits("hud"), "")
        self.assertEqual(s.read("F1", img).replace(" ", ""), "7ft9")
        reloaded = self.store()
        reloaded.samples["hud"] = {k: [x for x in v if x["source"] == "learned"] or v
                                   for k, v in reloaded.samples["hud"].items()}
        self.assertTrue(any(x["source"] == "learned" for x in reloaded.samples["hud"]["9"]),
                        "learned characters are saved and reloaded")

    def test_inconsistent_text_is_not_learned(self):
        s = self.store()
        f = frame_with("F1", "0ft 9")
        self.assertEqual(s.learn("F1", fx("F1", f["frame"]), "ft9"), 0,
                         "Tesseract's dropped zero does not line up, so nothing is learned")

    def test_cross_check_switches_a_font_off(self):
        dr.DIGITS = set("012345689")
        s = self.store()
        img = fx("L1", TEXT_FRAMES[0]["frame"])
        self.assertEqual(s.read("L1", img), "25.0")
        for _ in range(3):
            s.record_audit("L1", "25.0", "26.0")
        self.assertIsNone(s.read("L1", img), "after repeated disagreement, Tesseract only")


ROUND3 = json.loads((FIX / "frames.json").read_text()).get("round3_scores", [])


class RoundScore(unittest.TestCase):
    """v3.8.1 — the round score's "-6" was read as "6" (taken as +6), "-7" as "-1"
    and "-8" as nothing, so holes were discarded. The score is drawn in the
    launch panel's typeface and now borrows its digits."""

    def crops(self):
        old = [(f["frame"], f["text"]["U1"]) for f in TEXT_FRAMES + ROUND2]
        return old + [(f["frame"], f["U1"]) for f in ROUND3]

    def test_every_verified_round_score(self):
        s = dr.TemplateStore(CharacterMatching.SEED, None)              # ten-digit rule ON
        for name, truth in self.crops():
            self.assertEqual(s.read("U1", fx("U1", name)), truth, name)

    def test_panel_digits_alone_read_the_score(self):
        s = dr.TemplateStore(CharacterMatching.SEED, None)
        s.samples["score"] = {k: v for k, v in s.samples["score"].items() if not k.isdigit()}
        self.assertEqual(s.missing_digits("score"), "", "borrowed from the panel font")
        for f in ROUND3:
            if f["U1"] in ("-6", "-7", "-8", "-10", "-12"):
                self.assertEqual(s.read("U1", fx("U1", f["frame"])), f["U1"], f["frame"])

    def test_invalid_tesseract_read_does_not_count_against_matching(self):
        s = dr.TemplateStore(CharacterMatching.SEED, None)
        for _ in range(5):
            self.assertTrue(s.record_audit("B1", "Hole8", "Hole"))     # seen in play
        self.assertIsNotNone(s.read("B1", fx("B1", "r2_13_04_26")), "still switched on")


class EndOfRoundScreen(unittest.TestCase):
    """T-28 (v3.8.2): GSPro's end-of-round total, read only on that screen."""

    def frame(self, name):
        man = json.loads((FIX / "frames.json").read_text())
        x0, y0, *_ = man["end_screen_area"]
        canvas = Image.new("RGB", (1920, 1080))
        canvas.paste(Image.open(FIX / f"{name}.jpg").convert("RGB"), (x0, y0))
        return canvas

    def test_reads_every_end_screen_and_nothing_else(self):
        od = _import_daemon()
        cfg = json.loads((HERE / "config.json").read_text())
        saved = od.screen_capture.window_x_offset
        od.screen_capture.window_x_offset = 0
        try:
            for e in json.loads((FIX / "frames.json").read_text())["end_screens"]:
                got = dict((c, t) for c, _, t in od.ocr_reader.read_end_screen(cfg, self.frame(e["frame"]), 1920, 0))
                if e["total"] is None:
                    self.assertEqual(got, {}, e["what"])
                else:
                    self.assertEqual((got.get("W1"), got.get("W2")), (str(e["total"]), "SCORE"), e["frame"])
        finally:
            od.screen_capture.window_x_offset = saved


class CharacterMatchingInTheDaemon(unittest.TestCase):
    def setUp(self):
        self.od = _import_daemon()
        self.saved = (self.od.ocr_reader.DIGITS, dr.DIGITS)
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_digits_d_"))
        import pytesseract
        self.pt = pytesseract
        self.saved_tess = pytesseract.image_to_string
        self.calls = []
        pytesseract.image_to_string = lambda *a, **k: (self.calls.append(1), "25.0")[1]

    def tearDown(self):
        self.od.ocr_reader.DIGITS, dr.DIGITS = self.saved
        self.pt.image_to_string = self.saved_tess
        shutil.rmtree(self.dir, ignore_errors=True)

    def region(self):
        return {"cell": "L1", "name": "vla", "x": 0, "y": 0, "width": 1, "height": 1}

    def test_ready_font_skips_tesseract(self):
        dr.DIGITS = set("012345689")
        self.od.ocr_reader.DIGITS = dr.TemplateStore(CharacterMatching.SEED, self.dir / "l.json")
        text, _ = self.od.ocr_reader.ocr_image(self.region(), fx("L1", TEXT_FRAMES[0]["frame"]), "eng")
        self.assertEqual((text, len(self.calls)), ("25.0", 0))

    def test_unready_font_uses_tesseract(self):
        self.od.ocr_reader.DIGITS = dr.TemplateStore(CharacterMatching.SEED, self.dir / "l.json")
        self.od.ocr_reader.DIGITS.samples["panel"].pop("2")                      # panel font: incomplete
        text, _ = self.od.ocr_reader.ocr_image(self.region(), fx("L1", ROUND2[1]["frame"]), "eng")
        self.assertEqual((text, len(self.calls)), ("25.0", 1))


class AutoPreCheck(unittest.TestCase):
    """v3.6.1 — holes 8 and 9 of a real round were skipped as 'hole ? stroke 1
    unchanged': an unreadable hole number counted as unchanged, and a new hole
    resets the stroke ticker DOWN, which only 'advanced' was checked for."""

    def setUp(self):
        od = self.od = _import_daemon()
        self.saved = (od.load_state, od.ocr_reader.capture_and_ocr, od.screen_capture.grab_region,
                      od._arrow.detect_stroke_ticker, od._ARROW_OK)
        od.load_state = lambda: {"last_capture": {"hole": 7, "ticker": 2},
                                 "round": {"hole_par": 4}}
        od.ocr_reader.capture_and_ocr = lambda region, lang: (self.hole_text, None)
        od.screen_capture.grab_region = lambda region: Image.new("RGB", (4, 4))
        od._arrow.detect_stroke_ticker = lambda img, hole_par=4: self.ticker
        od._ARROW_OK = True
        self.cfg = {"regions": [{"cell": "B1"}, {"cell": "V1"}]}

    def tearDown(self):
        od = self.od
        (od.load_state, od.ocr_reader.capture_and_ocr, od.screen_capture.grab_region,
         od._arrow.detect_stroke_ticker, od._ARROW_OK) = self.saved

    def check(self, hole_text, ticker):
        self.hole_text, self.ticker = hole_text, ticker
        return self.od._auto_precheck(self.cfg)

    def test_unreadable_hole_runs_the_capture(self):
        self.assertTrue(self.check("Holes", 1), "the holes 8-9 regression")

    def test_stroke_reset_runs_the_capture(self):
        self.assertTrue(self.check("Hole7", 1))

    def test_true_repeat_is_still_skipped(self):
        self.assertFalse(self.check("Hole7", 2))

    def test_new_hole_runs_the_capture(self):
        self.assertTrue(self.check("Hole8", 1))


class LaunchDecimalRepair(unittest.TestCase):
    def test_dropped_point_is_restored(self):
        import parsing
        p = parsing.parse_ocr_fields({"R1": "-45", "Q1": "349", "L1": "28.8", "S1": "5526"}, {})
        self.assertEqual((p["spin_axis"], p["ball_speed"], p["vla"], p["total_spin"]),
                         (-4.5, 34.9, 28.8, 5526), "spin (S1) is an integer and untouched")


class CrossCheckFormats(unittest.TestCase):
    """v3.8.7: a Tesseract read GSPro could never display does not count against matching."""

    def setUp(self):
        import digit_reader
        self.store = digit_reader.TemplateStore(HERE / "digit_templates.seed.json", None)

    def test_dropped_decimal_or_digit_does_not_count(self):
        for cell, matched, tess in (("R1", "4.8", "48"), ("Q1", "7.7", "71"),
                                    ("L1", "0.2", "4"), ("F1", "0ft 0", "ft0")):
            self.assertTrue(self.store.record_audit(cell, matched, tess), cell)
        self.assertNotIn("panel", self.store._disabled)
        self.assertEqual(self.store._audits.get("panel", [0, 0])[1], 0)

    def test_a_real_disagreement_still_counts(self):
        self.assertFalse(self.store.record_audit("O1", "27.4", "271.4"))
        self.assertEqual(self.store._audits["panel"], [1, 1])

    def test_feet_and_inches_and_spin_are_valid_formats(self):
        self.assertTrue(self.store.record_audit("O1", "12' 4\"", "12' 4\""))
        self.assertFalse(self.store.record_audit("S1", "2345", "2845"))


class WindArrow(unittest.TestCase):
    """T-06 (v3.8.8): the tip is found from the arrow's notch, not its sharpest corner."""

    def setUp(self):
        import arrow_detector
        self.ad = arrow_detector
        self.saved = arrow_detector.WIND_ARROW_POINTS

    def tearDown(self):
        self.ad.WIND_ARROW_POINTS = self.saved

    def test_all_verified_real_arrows(self):
        import json
        truth = json.load(open(FIX / "wind_arrows.json"))["arrows"]
        wrong = []
        for name, expected in truth.items():
            got = self.ad.detect_wind_direction(Image.open(FIX / name)).split(" ")[0]
            if got != expected:
                wrong.append((name, expected, got))
        self.assertEqual(wrong, [], f"{len(truth) - len(wrong)}/{len(truth)} right")

    def test_effect_follows_the_convention(self):
        self.ad.WIND_ARROW_POINTS = "to"
        self.assertEqual(self.ad.wind_label("N"), "N (helping)")
        self.assertEqual(self.ad.wind_label("E"), "E (left-to-right)")
        self.ad.WIND_ARROW_POINTS = "from"
        self.assertEqual(self.ad.wind_label("N"), "N (into)")
        self.assertEqual(self.ad.wind_label("E"), "E (right-to-left)")
        self.assertEqual(self.ad.wind_label("NNE"), "NNE (into, slightly right-to-left)")

    def test_no_notch_falls_back_to_the_farthest_point(self):
        from PIL import ImageDraw
        img = Image.new("RGB", (43, 49), (255, 255, 255))
        ImageDraw.Draw(img).polygon([(21, 4), (8, 44), (34, 44)], fill=(10, 120, 240))   # ▲, no notch
        self.assertEqual(self.ad.detect_wind_direction(img).split(" ")[0], "N")


class TesseractBatch(unittest.TestCase):
    """T-37 (v3.8.9): fields read by Tesseract share one run per group of settings."""

    CELLS = ("G1", "J1", "I1", "H1", "E1")

    def setUp(self):
        import json
        import ocr_reader
        import pytesseract
        self.ocr, self.pt = ocr_reader, pytesseract
        self.regions = {r["cell"]: r for r in json.load(open(HERE / "config.json"))["regions"]}
        self.crops = [(c, Image.open(FIX / f"tess_{c}_{k}.png").convert("RGB"))
                      for k in range(4) for c in self.CELLS]
        self.saved = (ocr_reader.DIGITS, ocr_reader._tesseract_pages, pytesseract.pytesseract.run_tesseract)
        ocr_reader.DIGITS = None
        self.runs = 0
        real = pytesseract.pytesseract.run_tesseract

        def counting(*a, **k):
            self.runs += 1
            return real(*a, **k)
        pytesseract.pytesseract.run_tesseract = counting

    def tearDown(self):
        self.ocr.DIGITS, self.ocr._tesseract_pages, self.pt.pytesseract.run_tesseract = self.saved

    def singles(self):
        return [self.ocr.ocr_image(self.regions[c], img, "eng")[0] for c, img in self.crops]

    def batched(self):
        jobs = [self.ocr.ocr_image(self.regions[c], img, "eng", defer_tesseract=True) for c, img in self.crops]
        self.assertTrue(all(isinstance(j, self.ocr.TessJob) for j in jobs))
        return self.ocr.run_tesseract_jobs(jobs)

    def test_batched_reads_equal_single_runs(self):
        expected = self.singles()
        self.assertEqual(self.batched(), expected)

    def test_two_groups_of_settings_two_runs(self):
        self.batched()
        self.assertEqual(self.runs, 2, "20 fields: lie angles, club and lies share settings; wind speed has its own")

    def test_wrong_page_count_falls_back_to_single_runs(self):
        expected = self.singles()
        self.ocr._tesseract_pages = lambda images, lang, config: None
        self.assertEqual(self.batched(), expected)

    def test_failed_batch_falls_back_to_single_runs(self):
        expected = self.singles()

        def broken(images, lang, config):
            raise OSError("disk full")
        self.ocr._tesseract_pages = broken
        self.assertEqual(self.batched(), expected)

    def test_cross_check_disagreement_keeps_the_matched_read(self):
        import digit_reader
        store = digit_reader.TemplateStore(HERE / "digit_templates.seed.json", None)
        self.ocr.DIGITS = store
        store.needs_audit = lambda cell: True
        saved_read = self.ocr.tesseract_read
        self.ocr.tesseract_read = lambda region, img, lang, method: ("21.4", img)
        try:
            img = Image.open(FIX / "O1_r2_13_04_26.png").convert("RGB")
            matched = store.read("O1", img)
            text, _ = self.ocr.ocr_image(self.regions["O1"], img, "eng")
        finally:
            self.ocr.tesseract_read = saved_read
        self.assertIsNotNone(matched)
        self.assertEqual(text, matched, "Tesseract's misread does not replace the matched read")
        self.assertEqual(store._audits["panel"], [1, 1], "but the disagreement still counts")


class LastShotPanel(unittest.TestCase):
    """T-08 (v3.8.10): GSPro's LAST SHOT panel — speed and spin kept."""

    def setUp(self):
        import ocr_reader
        import pytesseract
        self.ocr, self.pt = ocr_reader, pytesseract
        self.cfg = {"regions": [], "last_shot_panel": {"speed": [3702, 604, 100, 44], "spin": [3702, 686, 100, 44]}}
        self.regions = {r["cell"]: r for r in ocr_reader.last_shot_regions(self.cfg)}
        self.saved = (ocr_reader.DIGITS, pytesseract.pytesseract.run_tesseract)
        ocr_reader.DIGITS = None
        self.runs = 0
        real = pytesseract.pytesseract.run_tesseract

        def counting(*a, **k):
            self.runs += 1
            return real(*a, **k)
        pytesseract.pytesseract.run_tesseract = counting

    def tearDown(self):
        self.ocr.DIGITS, self.pt.pytesseract.run_tesseract = self.saved

    def read(self, cell, tag):
        return self.ocr.ocr_image(self.regions[cell], Image.open(FIX / f"{cell}_{tag}.png").convert("RGB"), "eng")[0]

    def test_reads_the_percentages_when_showing(self):
        self.assertEqual((self.read("X1", "last_shot"), self.read("Y1", "last_shot")), ("80%", "53%"))

    def test_nothing_read_and_no_tesseract_when_not_showing(self):
        self.assertEqual((self.read("X1", "no_panel"), self.read("Y1", "no_panel")), ("", ""))
        self.assertEqual(self.runs, 0, "the navy check skips Tesseract entirely")

    def test_read_frame_includes_the_panel(self):
        frame = Image.new("RGB", (1920, 1080), (30, 60, 30))
        for c in ("X1", "Y1"):
            r = self.regions[c]
            frame.paste(Image.open(FIX / f"{c}_last_shot.png").convert("RGB"), (r["x"] - 1920, r["y"]))
        reads, ends, errors, _, _ = self.ocr.read_frame(self.cfg, frame, 1920, 0, 4, "eng")
        self.assertEqual({c: t for _, c, t, _ in reads}, {"X1": "80%", "Y1": "53%"})
        self.assertEqual(errors, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
