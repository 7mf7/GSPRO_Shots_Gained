#!/usr/bin/env python3
"""
test_report_worker.py — Background report refresh (report_worker.py, T-10).

    python test_report_worker.py
"""
import logging
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)
import report_worker  # noqa: E402


class Recorder:
    """An export that takes `seconds` and counts how often it ran."""

    def __init__(self, seconds=0.0, fail_first=False):
        self.seconds, self.fail_first = seconds, fail_first
        self.calls, self.lock = 0, threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
            n = self.calls
        time.sleep(self.seconds)
        if self.fail_first and n == 1:
            raise OSError("disk full")


class Worker(unittest.TestCase):
    def make(self, rec, debounce=0.05, max_wait=10.0):
        w = report_worker.ReportWorker(rec, debounce=debounce, max_wait=max_wait)
        self.addCleanup(w.stop, 5)
        return w

    def test_request_returns_at_once_even_while_a_refresh_runs(self):
        rec = Recorder(seconds=0.5)
        w = self.make(rec, debounce=0.0)
        w.request()
        time.sleep(0.05)                                     # the refresh is now running
        t0 = time.perf_counter()
        w.request()
        self.assertLess(time.perf_counter() - t0, 0.01, "a capture never waits for the report")

    def test_requests_close_together_are_combined(self):
        rec = Recorder()
        w = self.make(rec, debounce=0.2)
        for _ in range(10):
            w.request()
            time.sleep(0.01)
        self.assertTrue(w.flush(5))
        self.assertEqual(rec.calls, 1)
        self.assertEqual(w.stats["combined"], 9)

    def test_request_during_a_refresh_gets_exactly_one_follow_up(self):
        rec = Recorder(seconds=0.3)
        w = self.make(rec, debounce=0.0)
        w.request()
        time.sleep(0.1)                                      # mid-refresh
        w.request()
        w.request()
        self.assertTrue(w.flush(5))
        self.assertEqual(rec.calls, 2, "the report ends up current, without a refresh per request")

    def test_a_steady_stream_cannot_postpone_the_report(self):
        rec = Recorder()
        w = self.make(rec, debounce=0.3, max_wait=0.5)
        end = time.monotonic() + 1.6
        while time.monotonic() < end:                        # never quiet for 0.3 s
            w.request()
            time.sleep(0.05)
        self.assertGreaterEqual(rec.calls, 2, "max_wait forces refreshes during the stream")

    def test_a_failed_refresh_does_not_stop_the_worker(self):
        rec = Recorder(fail_first=True)
        w = self.make(rec, debounce=0.0)
        w.request()
        self.assertTrue(w.flush(5))
        w.request()
        self.assertTrue(w.flush(5))
        self.assertEqual((rec.calls, w.stats["failed"]), (2, 1))

    def test_flush_skips_the_debounce(self):
        rec = Recorder()
        w = self.make(rec, debounce=30.0, max_wait=60.0)
        w.request()
        t0 = time.perf_counter()
        self.assertTrue(w.flush(5))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(rec.calls, 1)

    def test_stop_finishes_what_was_asked_then_ignores_more(self):
        rec = Recorder(seconds=0.1)
        w = report_worker.ReportWorker(rec, debounce=30.0)
        w.request()
        self.assertTrue(w.stop(5))
        w.request()
        time.sleep(0.1)
        self.assertEqual(rec.calls, 1)


class InTheDaemon(unittest.TestCase):
    def setUp(self):
        import test_capture as tc
        self.od = tc._import_daemon()
        self.saved = (self.od.export_report, self.od._REPORTS)
        self.rec = Recorder(seconds=0.5)
        self.od.export_report = lambda config: self.rec()
        self.od._REPORTS = None

    def tearDown(self):
        self.od._stop_report_worker(5)
        self.od.export_report, self.od._REPORTS = self.saved

    def test_capture_does_not_wait_for_the_report(self):
        t0 = time.perf_counter()
        self.od._request_report({"report_debounce_sec": 0.0}, "hole finished")
        self.assertLess(time.perf_counter() - t0, 0.05)
        self.assertTrue(self.od._REPORTS.flush(5))
        self.assertEqual(self.rec.calls, 1)

    def test_synchronous_when_background_is_off(self):
        t0 = time.perf_counter()
        self.od._request_report({"report_background": False})
        self.assertGreaterEqual(time.perf_counter() - t0, 0.45)
        self.assertEqual(self.rec.calls, 1)
        self.assertIsNone(self.od._REPORTS)

    def test_exit_finishes_a_pending_refresh(self):
        self.od._request_report({"report_debounce_sec": 30.0})
        self.od._stop_report_worker(5)
        self.assertEqual(self.rec.calls, 1, "requested, so written before the final report")
        self.assertIsNone(self.od._REPORTS)


if __name__ == "__main__":
    unittest.main(verbosity=1)
