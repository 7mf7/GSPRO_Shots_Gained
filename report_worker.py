#!/usr/bin/env python3
"""
report_worker.py — Refresh the report in the background (T-10, v3.8.3).

Writing the ODS report and dashboard takes seconds — about 2 s at 1,000 shots,
12–15 s at a season's 6,000 — and it used to run on the capture thread after
every hole, so the next capture waited for it. A capture now only *asks* for a
refresh (request(), which returns at once) and this worker does the writing.

  * Requests that arrive close together are combined into one refresh: the
    worker waits until requests have stopped for `debounce` seconds (but never
    longer than `max_wait` after the first one, so a steady stream of requests
    cannot postpone the report indefinitely).
  * A request that arrives while a refresh is running is not lost: exactly one
    more refresh follows, so the report always ends up current.
  * A failing refresh is logged and the worker carries on.
  * flush() waits until everything requested so far has been written, skipping
    the debounce; stop() does that and then ends the thread. The daemon calls
    stop() before its final, synchronous report at exit.

The report is read from the database in one short transaction and built and
written outside it (db.report_sheets), so a capture waits at most for that
read — never for the writing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class ReportWorker:
    def __init__(self, export_fn: Callable[[], object], debounce: float = 2.0,
                 max_wait: float = 10.0, name: str = "report-worker") -> None:
        self._export = export_fn
        self.debounce = max(0.0, float(debounce))
        self.max_wait = max(self.debounce, float(max_wait))
        self._cv = threading.Condition()
        self._pending = 0                 # requests not yet covered by a started refresh
        self._first_at: float | None = None
        self._last_at: float | None = None
        self._running = False
        self._hurry = False               # flush(): skip the debounce
        self._stop = False
        self.stats = {"requests": 0, "refreshes": 0, "combined": 0, "failed": 0,
                      "last_seconds": None, "longest_seconds": 0.0}
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    # ── called from capture threads ──────────────────────────────────────────
    def request(self, reason: str = "") -> None:
        """Ask for a refresh. Returns immediately."""
        with self._cv:
            if self._stop:
                return
            now = time.monotonic()
            self.stats["requests"] += 1
            self._pending += 1
            self._first_at = self._first_at if self._first_at is not None else now
            self._last_at = now
            self._cv.notify_all()
        if reason:
            log.debug("Report refresh requested (%s)", reason)

    @property
    def busy(self) -> bool:
        with self._cv:
            return self._running or self._pending > 0

    # ── the worker ───────────────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if not self._pending:                 # stopping, nothing left
                    return
                while not (self._stop or self._hurry):
                    now = time.monotonic()
                    quiet_left = self.debounce - (now - self._last_at)
                    cap_left = self.max_wait - (now - self._first_at)
                    wait = min(quiet_left, cap_left)
                    if wait <= 0:
                        break
                    self._cv.wait(timeout=wait)
                n, self._pending = self._pending, 0
                self._first_at = self._last_at = None
                self._running = True
            t0 = time.monotonic()
            ok = True
            try:
                self._export()
            except Exception as exc:                  # never let the worker die
                ok = False
                log.error("Background report refresh failed: %s", exc)
            dt = time.monotonic() - t0
            with self._cv:
                self._running = False
                self.stats["refreshes"] += 1
                self.stats["combined"] += n - 1
                self.stats["failed"] += 0 if ok else 1
                self.stats["last_seconds"] = round(dt, 2)
                self.stats["longest_seconds"] = round(max(self.stats["longest_seconds"], dt), 2)
                if not self._pending:
                    self._hurry = False
                self._cv.notify_all()
            if ok:
                log.info("Report refreshed in the background in %.1fs%s", dt,
                         f" ({n} requests combined)" if n > 1 else "")

    # ── waiting and stopping ─────────────────────────────────────────────────
    def flush(self, timeout: float | None = None) -> bool:
        """Write everything requested so far, without the debounce. False on timeout."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            self._hurry = True
            self._cv.notify_all()
            while self._pending or self._running:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(timeout=left)
            self._hurry = False
            return True

    def stop(self, timeout: float | None = 60.0) -> bool:
        """Finish anything requested, then end the worker. False if it timed out."""
        done = self.flush(timeout)
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._thread.join(timeout if timeout is not None else None)
        return done and not self._thread.is_alive()

    def summary(self) -> str:
        s = self.stats
        return (f"{s['refreshes']} background refresh(es) for {s['requests']} request(s) "
                f"({s['combined']} combined, {s['failed']} failed); longest "
                f"{s['longest_seconds']:.1f}s")
