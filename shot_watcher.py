#!/usr/bin/env python3
"""
shot_watcher.py — Automatic shot detection by screen polling.

Replaces the need to press Ctrl+Shift+F9 after every shot.

── Why polling a tripwire instead of OCR-ing everything ──────────────────────
Running the full 22-region Tesseract pipeline in a loop would cost several
seconds of CPU per pass and thrash the disk with ODS writes.  Instead this
module polls ONE small region (the stroke ticker) several times a second using
a cheap pixel signature — no OCR, no disk — and only fires the expensive
pipeline when that region has changed AND then held still.

── The state machine ─────────────────────────────────────────────────────────

    IDLE ──(signature changed)──> SETTLING ──(N stable polls)──> CAPTURE
      ^                               |                            |
      |                        (changes again:                     |
      |                         reset stability)                   |
      +──────────────(cooldown elapsed)───────────────────────────-+

The SETTLING state is the important part.  After a shot, GSPro plays a ball
flight animation, moves the camera, and only then updates the UI.  Capturing
during that window yields garbage.  Requiring the tripwire to be byte-stable
for several consecutive polls means we capture the settled screen, not the
animation.

── Guards ───────────────────────────────────────────────────────────────────
  * Cooldown        — minimum seconds between automatic captures.
  * Stability       — N consecutive identical polls before firing.
  * Pipeline lock   — the daemon already refuses re-entrant pipeline runs, so
                      a manual F9 during a capture cannot collide.
  * Validation      — run_pipeline rejects implausible frames (menus, replays)
                      via _capture_is_valid, so a false trigger costs nothing
                      but a log line.

Public API
----------
ShotWatcher(config, pipeline_fn, grab_fn, state_fn)
  .start()      — begin polling on a background thread
  .stop()       — stop polling
  .toggle()     — flip on/off, returns the new state
  .is_running() — current state
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Defaults (all overridable from config.json) ────────────────────────────────

DEFAULT_POLL_INTERVAL   = 0.25   # seconds between polls (4 Hz)
DEFAULT_STABLE_POLLS    = 6      # consecutive identical polls before capture
DEFAULT_COOLDOWN        = 4.0    # minimum seconds between auto-captures
DEFAULT_TRIGGER_CELL    = "V1"   # stroke ticker — increments once per shot
DEFAULT_CHANGE_THRESHOLD = 6.0   # mean abs pixel diff to count as "changed"

# Signature resolution.  16x16 is coarse enough to ignore antialiasing jitter
# and animated backgrounds, fine enough to catch a digit changing.
_SIG_SIZE = (16, 16)


def _signature(img: Image.Image) -> "np.ndarray":
    """
    Reduce an image to a small grayscale fingerprint for cheap comparison.

    Downsampling to 16x16 makes the comparison robust to one- or two-pixel
    rendering jitter while still registering a changed digit, and keeps the
    per-poll cost in the microseconds.
    """
    small = img.convert("L").resize(_SIG_SIZE, Image.BILINEAR)
    return np.asarray(small, dtype=np.float32)


def _sig_diff(a: Optional["np.ndarray"], b: "np.ndarray") -> float:
    """Mean absolute difference between two signatures (0 = identical)."""
    if a is None or a.shape != b.shape:
        return float("inf")
    return float(np.abs(a - b).mean())


class ShotWatcher:
    """
    Polls a trigger region and calls *pipeline_fn* when a settled change is seen.

    Parameters
    ----------
    config       : the daemon config dict (read for watcher tuning keys)
    pipeline_fn  : callable invoked with no arguments to run a full capture
    grab_fn      : callable(region_dict) -> PIL.Image, supplied by the daemon so
                   the window x-offset toggle is honoured automatically
    state_fn     : callable() -> state dict, used to read the current hole par
    """

    def __init__(
        self,
        config: dict,
        pipeline_fn: Callable[[], None],
        grab_fn: Callable[[dict], Image.Image],
        state_fn: Callable[[], dict] | None = None,
        pre_check_fn: Callable[[], bool] | None = None,
        cleanup_fn: Callable[[], None] | None = None,
        on_settle: Callable[[str], None] | None = None,
    ) -> None:
        self._config       = config
        self._pipeline_fn  = pipeline_fn
        self._grab_fn      = grab_fn
        self._state_fn     = state_fn
        self._pre_check_fn = pre_check_fn
        self._cleanup_fn   = cleanup_fn
        # Told about every screen that settles without a capture ("cooldown",
        # "skipped") — the screenshot feature saves those, since missed shots
        # hide in exactly those moments.
        self._on_settle    = on_settle

        self.poll_interval = float(config.get("auto_poll_interval", DEFAULT_POLL_INTERVAL))
        self.stable_polls  = int(config.get("auto_stable_polls",   DEFAULT_STABLE_POLLS))
        self.cooldown      = float(config.get("auto_cooldown_sec", DEFAULT_COOLDOWN))
        self.threshold     = float(config.get("auto_change_threshold",
                                              DEFAULT_CHANGE_THRESHOLD))
        trigger_cell       = str(config.get("auto_trigger_cell",
                                            DEFAULT_TRIGGER_CELL)).upper()

        self._region = self._find_region(trigger_cell)
        if self._region is None:
            log.error(
                "ShotWatcher: no region with cell %s in config — auto-capture unavailable",
                trigger_cell,
            )

        self._thread:   threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._running  = False
        self._last_fire = 0.0
        # Log every watcher decision at INFO (otherwise DEBUG) — enable for a
        # session to see exactly why captures fire or are skipped.
        self.verbose = bool(config.get("auto_capture_verbose", False))

    # ── decision ──────────────────────────────────────────────────────────────

    def _notify(self, decision: str) -> None:
        if self._on_settle is not None:
            try:
                self._on_settle(decision)
            except Exception as exc:
                log.debug("ShotWatcher: on_settle raised — %s", exc)

    def _decision(self, msg: str, *args) -> None:
        (log.info if self.verbose else log.debug)("ShotWatcher: " + msg, *args)

    def _on_settled(self, now: float) -> str:
        """
        Decide what to do once the trigger region has settled after a change.
        Returns "cooldown", "skipped", "fired" or "error". Kept apart from the
        polling loop so the timing rules can be tested with a controlled clock.

        The cooldown starts ONLY when a capture actually runs. It used to start
        on a pre-check skip as well: the screen often settles a moment before
        GSPro updates the stroke counter, so the pre-check saw the old stroke and
        skipped — and the counter's real update, a second or two later, then fell
        inside the cooldown and was discarded. A missed shot.
        """
        if self._stop_evt.is_set():
            return "stopped"
        if now - self._last_fire < self.cooldown:
            self._decision("within cooldown (%.1fs) — skipping", self.cooldown)
            self._notify("cooldown")
            return "cooldown"
        if self._pre_check_fn is not None:
            try:
                if not self._pre_check_fn():
                    self._decision("pre-check: nothing advanced — skipped; no cooldown, "
                                   "watching for the next change")
                    self._notify("skipped")
                    return "skipped"
            except Exception as exc:
                log.debug("ShotWatcher: pre-check raised (%s) — proceeding", exc)
        if self._stop_evt.is_set():
            # Stopped (or the daemon is exiting) while the pre-check ran: do not
            # start a capture the exit sequence would then race (v3.8.11).
            self._decision("stopping — capture not started")
            return "stopped"
        self._last_fire = now
        log.info("ShotWatcher: screen settled — auto-capturing")
        try:
            self._pipeline_fn()
        except Exception as exc:
            log.error("ShotWatcher: pipeline raised — %s", exc)
            return "error"
        return "fired"

    # ── public ────────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def available(self) -> bool:
        return self._region is not None

    def start(self) -> bool:
        if not self.available():
            log.error("ShotWatcher: cannot start — trigger region not configured")
            return False
        if self._running:
            return True
        self._stop_evt.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="shot-watcher"
        )
        self._thread.start()
        log.info(
            "━━━  AUTO-CAPTURE ON  —  watching %s every %.2fs, "
            "%d stable polls, %.1fs cooldown  ━━━",
            self._region.get("cell", "?"), self.poll_interval,
            self.stable_polls, self.cooldown,
        )
        return True

    def stop(self, wait: bool = False, timeout: float = 30.0) -> bool:
        """
        Stop watching. With wait=True, also wait for the polling thread to end —
        including a capture it has already started, which is finished rather
        than cut off. The daemon waits at exit, so no capture is still running
        when the exit reports are written and Python shuts down (v3.8.11).
        Returns True once the thread has ended (always True without wait).
        """
        if self._running:
            self._stop_evt.set()
            self._running = False
            log.info("━━━  AUTO-CAPTURE OFF  —  press the OCR hotkey manually  ━━━")
        t = self._thread
        if not wait or t is None or t is threading.current_thread():
            return True
        if t.is_alive():
            t.join(timeout)
        return not t.is_alive()

    def toggle(self) -> bool:
        if self._running:
            self.stop()
        else:
            self.start()
        return self._running

    # ── internal ──────────────────────────────────────────────────────────────

    def _find_region(self, cell: str) -> dict | None:
        for region in self._config.get("regions", []):
            if str(region.get("cell", "")).upper() == cell:
                return region
        return None

    def _loop(self) -> None:
        """
        Poll the trigger region.  Two-phase: wait for a change, then wait for
        the screen to settle before firing the capture.
        """
        last_sig: Optional[np.ndarray] = None
        stable   = 0
        pending  = False   # True once a change has been seen, until we fire

        # Prime the baseline so startup does not immediately count as a change
        try:
            last_sig = _signature(self._grab_fn(self._region))
        except Exception as exc:
            log.error("ShotWatcher: initial grab failed — %s", exc)

        while not self._stop_evt.is_set():
            time.sleep(self.poll_interval)
            if self._stop_evt.is_set():
                break

            try:
                sig = _signature(self._grab_fn(self._region))
            except Exception as exc:
                log.debug("ShotWatcher: grab failed — %s", exc)
                continue

            diff = _sig_diff(last_sig, sig)
            last_sig = sig

            if diff > self.threshold:
                # Screen is moving — (re)start the settle timer
                if not pending:
                    log.debug("ShotWatcher: change detected (diff=%.1f) — settling", diff)
                pending = True
                stable  = 0
                continue

            if not pending:
                continue

            stable += 1
            if stable < self.stable_polls:
                continue

            # ── Settled after a change ────────────────────────────────────────
            pending = False
            stable  = 0
            if self._on_settled(time.monotonic()) == "error":
                # Re-prime the baseline so one failure does not cause a
                # retrigger loop on the very next poll.
                try:
                    last_sig = _signature(self._grab_fn(self._region))
                except Exception:
                    pass

        # Release this thread's screen-capture context.  Without this, toggling
        # auto-capture on and off repeatedly would leak one device context per
        # cycle, and DC pressure is what disturbs fullscreen in the first place.
        if self._cleanup_fn is not None:
            try:
                self._cleanup_fn()
            except Exception as exc:
                log.debug("ShotWatcher: cleanup raised — %s", exc)
        log.debug("ShotWatcher: poll thread exited")
