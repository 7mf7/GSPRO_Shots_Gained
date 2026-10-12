#!/usr/bin/env python3
"""
test_shutdown.py — Exiting while a capture is running (v3.8.11).

On 8 Oct the daemon was told to exit while auto-capture was capturing a shot.
The exit sequence did not wait for it: the exit reports were written and Python
began shutting down underneath the capture, which then failed ("cannot schedule
new futures after interpreter shutdown") and, writing that to the console as
Python finalised, aborted Python ("Fatal Python error … stdout … daemon threads").

    python test_shutdown.py
"""
import logging
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
from PIL import Image                     # noqa: E402
import shot_watcher                       # noqa: E402

CFG = {"regions": [{"cell": "V1", "x": 0, "y": 0, "width": 40, "height": 20}],
       "auto_trigger_cell": "V1", "auto_poll_interval": 0.02, "auto_stable_polls": 2,
       "auto_cooldown_sec": 0}


def frames():
    """The trigger region: one change, then a settled screen."""
    yield Image.new("RGB", (40, 20), 0)
    while True:
        yield Image.new("RGB", (40, 20), (255, 255, 255))


class Watcher(unittest.TestCase):
    def watcher(self, pipeline, pre_check=None):
        f = frames()
        w = shot_watcher.ShotWatcher(CFG, pipeline_fn=pipeline, grab_fn=lambda r: next(f),
                                     state_fn=dict, pre_check_fn=pre_check)
        self.addCleanup(w.stop, True, 5)
        return w

    def test_stop_and_wait_finishes_a_capture_in_progress(self):
        started, done = threading.Event(), threading.Event()

        def pipeline():
            started.set()
            time.sleep(0.4)
            done.set()
        w = self.watcher(pipeline)
        w.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(w.stop(wait=True, timeout=5))
        self.assertTrue(done.is_set(), "the capture was finished, not cut off")
        self.assertFalse(w._thread.is_alive())

    def test_stop_during_the_pre_check_starts_no_capture(self):
        ran = []
        w = None

        def pre_check():
            w.stop()                          # the exit hotkey, while the pre-check runs
            return True
        w = self.watcher(lambda: ran.append(1), pre_check)
        w.start()
        self.assertTrue(w.stop(wait=True, timeout=5))
        self.assertEqual(ran, [])

    def test_stop_after_a_plain_stop_still_waits(self):
        started, done = threading.Event(), threading.Event()

        def pipeline():
            started.set()
            time.sleep(0.3)
            done.set()
        w = self.watcher(pipeline)
        w.start()
        self.assertTrue(started.wait(5))
        w.stop()                              # e.g. auto-capture toggled off just before exit
        self.assertTrue(w.stop(wait=True, timeout=5))
        self.assertTrue(done.is_set())


class ExitSequence(unittest.TestCase):
    def setUp(self):
        import test_capture
        od = self.od = test_capture._import_daemon()
        self.saved = (od._watcher, od._follower, od._saver, od._gspro, od._write_exit_reports)
        od._watcher = od._follower = od._saver = od._gspro = None
        self.events = []
        od._write_exit_reports = lambda config: self.events.append("exit reports")
        od._SHUTTING_DOWN.clear()

    def tearDown(self):
        od = self.od
        if od._watcher is not None:
            od._watcher.stop(wait=True, timeout=5)
        (od._watcher, od._follower, od._saver, od._gspro, od._write_exit_reports) = self.saved
        od._SHUTTING_DOWN.clear()
        if od._pipeline_lock.locked():
            od._pipeline_lock.release()

    class Listener:
        def stop(self):
            pass

    def capture(self, seconds):
        """Stands in for run_pipeline: holds the pipeline lock while it runs."""
        def run():
            with self.od._pipeline_lock:
                self.events.append("capture started")
                time.sleep(seconds)
                self.events.append("capture recorded")
        return run

    def test_auto_capture_in_progress_finishes_before_the_exit_reports(self):
        started = threading.Event()
        cap = self.capture(0.4)
        f = frames()
        w = shot_watcher.ShotWatcher(CFG, pipeline_fn=lambda: (started.set(), cap()),
                                     grab_fn=lambda r: next(f), state_fn=dict)
        self.od._watcher = w
        w.start()
        self.assertTrue(started.wait(5))
        self.od._finish_run({}, self.Listener())
        self.assertEqual(self.events, ["capture started", "capture recorded", "exit reports"])
        self.assertFalse(self.od._pipeline_lock.locked(), "released after the exit reports")

    def test_manual_capture_in_progress_finishes_first(self):
        t = threading.Thread(target=self.capture(0.4))
        t.start()
        while not self.events:
            time.sleep(0.01)
        self.od._finish_run({}, self.Listener())
        t.join()
        self.assertEqual(self.events, ["capture started", "capture recorded", "exit reports"])

    def test_no_capture_starts_once_shutting_down(self):
        self.od._SHUTTING_DOWN.set()
        calls = []
        saved = self.od.load_state
        self.od.load_state = lambda: calls.append(1)
        try:
            self.od.run_pipeline({}, source="auto")
        finally:
            self.od.load_state = saved
        self.assertEqual(calls, [], "refused before reading any state")


class RealExit(unittest.TestCase):
    """The 8 Oct crash, end to end in a fresh Python: a capture with a parallel
    OCR phase is running when the daemon exits, and Python takes a moment to
    tear down afterwards (as on Windows)."""

    SCRIPT = textwrap.dedent('''
        import atexit, logging, sys, threading, time
        from concurrent.futures import ThreadPoolExecutor
        sys.path.insert(0, sys.argv[1])
        import test_capture, shot_watcher
        from PIL import Image
        od = test_capture._import_daemon()
        logging.disable(logging.NOTSET)                           # test_capture silences logging
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s", force=True)
        def frames():
            yield Image.new("RGB", (40, 20), 0)
            while True:
                yield Image.new("RGB", (40, 20), (255, 255, 255))
        f, started = frames(), threading.Event()
        def capture():
            with od._pipeline_lock:
                started.set()
                time.sleep(0.6)                                   # grab, detectors, matching
                with ThreadPoolExecutor(max_workers=2) as pool:   # the shared Tesseract runs
                    pool.submit(time.sleep, 0.05).result()
                logging.info("capture recorded")
        cfg = {"regions": [{"cell": "V1", "x": 0, "y": 0, "width": 40, "height": 20}],
               "auto_trigger_cell": "V1", "auto_poll_interval": 0.02, "auto_stable_polls": 2,
               "auto_cooldown_sec": 0}
        od._watcher = shot_watcher.ShotWatcher(cfg, pipeline_fn=capture, grab_fn=lambda r: next(f),
                                               state_fn=dict)
        od._follower = od._saver = od._gspro = None
        od._write_exit_reports = lambda config: logging.info("exit reports written")
        od._watcher.start()
        started.wait(5)
        class L:
            def stop(self): pass
        od._finish_run({}, L())
        atexit.register(time.sleep, 0.8)
    ''')

    def test_exit_during_a_capture(self):
        p = subprocess.run([sys.executable, "-c", self.SCRIPT, str(HERE)],
                           capture_output=True, text=True, timeout=60)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("cannot schedule new futures", out)
        self.assertNotIn("Fatal Python error", out)
        lines = [l for l in p.stdout.splitlines() if l in ("capture recorded", "exit reports written")]
        self.assertEqual(lines, ["capture recorded", "exit reports written"], out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
