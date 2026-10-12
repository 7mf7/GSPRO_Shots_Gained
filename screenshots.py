#!/usr/bin/env python3
"""
screenshots.py — Timestamped full-screen screenshots, for reviewing a session
against the log.

Two ways in (see ocr_daemon.py):
  * a hotkey that saves the GSPro screen whenever it is pressed;
  * "every capture" mode, which saves the exact frame each capture read —
    auto-capture fires when the screen settles after a shot, which is the
    screen BEFORE the next shot — plus the screens the auto-capture watcher
    skipped, which is where missed shots hide.

Files go to screenshots/<date>/<date>_<time-with-ms>_<label>.png. The label
records what happened (e.g. "auto_h2_s2_accepted", "settle_skipped") and the
log names every file, so frames and log entries line up one to one.

Saving happens on a background thread so a capture never waits for the disk.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def pick_monitor(monitors: list[dict], regions: list[dict], x_offset: int = 0) -> dict | None:
    """
    The monitor showing the capture regions. *monitors* is mss-style: dicts
    with left/top/width/height, where entry 0 is the union of all monitors when
    there is more than one. The regions' centre, shifted by the current window
    offset, decides; the nearest monitor wins if it falls between them.
    """
    if not monitors:
        return None
    cands = monitors[1:] if len(monitors) > 1 else monitors
    if not regions:
        return cands[0]
    cx = sum(r["x"] + x_offset + r["width"] / 2 for r in regions) / len(regions)
    cy = sum(r["y"] + r["height"] / 2 for r in regions) / len(regions)
    for m in cands:
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            return m
    return min(cands, key=lambda m: abs(m["left"] + m["width"] / 2 - cx)
                                    + abs(m["top"] + m["height"] / 2 - cy))


_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]+")


def screenshot_path(base_dir: str | Path, when: datetime, label: str,
                    ext: str = "png") -> Path:
    """screenshots/2026-10-04/2026-10-04_09-22-30-123_<label>.png"""
    label = _UNSAFE.sub("-", label).strip("-") or "screenshot"
    stamp = when.strftime("%Y-%m-%d_%H-%M-%S-") + f"{when.microsecond // 1000:03d}"
    return Path(base_dir) / when.strftime("%Y-%m-%d") / f"{stamp}_{label}.{ext}"


class ScreenshotSaver:
    """Writes screenshots on a background thread; the caller never waits."""

    def __init__(self, fmt: str = "png", max_queue: int = 16) -> None:
        # PNG is lossless, so a saved frame can be re-read exactly by the OCR
        # later; JPEG is about a tenth of the size.
        self.fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self.saved = self.dropped = self.failed = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="screenshots")
        self._thread.start()

    def submit(self, image, path: str | Path) -> bool:
        """Queue an image for saving. False if the queue is full (disk too slow)."""
        try:
            self._q.put_nowait((image, Path(path)))
            return True
        except queue.Full:
            self.dropped += 1
            log.warning("Screenshot dropped — %d already waiting to be written: %s",
                        self._q.maxsize, Path(path).name)
            return False

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                return
            image, path = item
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self.fmt == "jpg":
                    image.convert("RGB").save(path, "JPEG", quality=92)
                else:
                    image.save(path, "PNG", compress_level=3)
                self.saved += 1
            except Exception as exc:
                self.failed += 1
                log.error("Screenshot not saved (%s): %s", path.name, exc)
            finally:
                self._q.task_done()

    def close(self, timeout: float = 15.0) -> None:
        """Write everything still queued, then stop."""
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)
