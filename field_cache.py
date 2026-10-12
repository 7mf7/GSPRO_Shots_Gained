#!/usr/bin/env python3
"""
field_cache.py — Re-read a HUD field only when its pixels change (T-13, v3.8.4).

The course name, hole, par and yardage change at most once per hole, yet every
capture read them again — the course name with Tesseract (~100 ms). This cache
keeps each field's last reading together with the pixels it came from, and
reuses the reading while the pixels are unchanged.

"Unchanged" is strict: no pixel may differ by more than `max_pixel_diff`
(24 of 255) and the average difference must stay under `max_mean_diff`. A
change of even one character moves pixels by ~200, so it always means a fresh
read. Measured on a real 36-hole session, these regions were pixel-identical
between every two captures of the same hole (the panel is opaque), and the
hole number's pixels changed at every new hole.

Only the listed cells are cached; an empty reading is never cached (it is
retried); entries expire after `max_age` seconds as a backstop.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

import numpy as np


class FieldCache:
    def __init__(self, cells: Iterable[str], max_pixel_diff: int = 24,
                 max_mean_diff: float = 1.0, max_age: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cells = {c.upper() for c in cells}
        self.max_pixel_diff = int(max_pixel_diff)
        self.max_mean_diff = float(max_mean_diff)
        self.max_age = float(max_age)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[np.ndarray, str, float]] = {}
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    @staticmethod
    def _pixels(img) -> np.ndarray:
        return np.asarray(img.convert("RGB"), dtype=np.int16)

    def lookup(self, cell: str, img) -> str | None:
        """The cached reading if this region's pixels are unchanged, else None."""
        cell = cell.upper()
        if cell not in self.cells:
            return None
        px = self._pixels(img)
        with self._lock:
            entry = self._entries.get(cell)
            if entry is not None:
                old, text, at = entry
                if (old.shape == px.shape and self._clock() - at <= self.max_age):
                    diff = np.abs(px - old)
                    if diff.max() <= self.max_pixel_diff and diff.mean() <= self.max_mean_diff:
                        self.hits[cell] = self.hits.get(cell, 0) + 1
                        return text
            self.misses[cell] = self.misses.get(cell, 0) + 1
        return None

    def store(self, cell: str, img, text: str) -> None:
        """Remember a fresh reading and the pixels it came from."""
        cell = cell.upper()
        if cell not in self.cells or not (text or "").strip():
            return
        with self._lock:
            self._entries[cell] = (self._pixels(img), text, self._clock())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def summary(self) -> str:
        with self._lock:
            parts = []
            for c in sorted(self.cells):
                h, m = self.hits.get(c, 0), self.misses.get(c, 0)
                if h + m:
                    parts.append(f"{c} {h}/{h + m}")
        return ("reused unchanged readings: " + ", ".join(parts)) if parts else "no lookups"
