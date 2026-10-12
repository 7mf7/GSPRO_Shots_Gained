#!/usr/bin/env python3
"""
screen_capture.py — Grabbing GSPro's screen. Split out of ocr_daemon.py (T-22, v3.8.5).

  * one persistent screen-capture context per thread (get_sct, close_sct)
  * one frame per capture covering every region (grab_frame), cut per region
    (crop_region); the whole GSPro monitor (grab_monitor); one region (grab_region)
  * which monitor GSPro is on: the x offset added to region coordinates
    (window_x_offset), kept current from GSPro's window (refresh_window_offset)
  * Windows DPI awareness, so region coordinates are physical pixels

State owned here: window_x_offset (set by monitor detection, the window-toggle
hotkey and the daemon at start-up) and RUN_CONFIG (the daemon's config).
"""

from __future__ import annotations

import logging
import sys
import threading
import time

try:
    import mss                        # only needed to grab the screen — reading saved
except ImportError:                   # screenshots (rebuild_from_screenshots.py) needs none
    mss = None
from PIL import Image

import screenshots

try:
    import console_follow as _cf      # finding GSPro's window (Windows only)
    _FOLLOW_OK = True
except ImportError:
    _cf = None
    _FOLLOW_OK = False

log = logging.getLogger(__name__)

# Window offset — 0  = Window 2 (config coordinates, right screen — GSPro default)
#                 -N = Window 1 (shifted left by window_x_shift pixels)
# Toggled by the window_toggle_hotkey; persisted in ocr_state.json.
window_x_offset: int = 0

# Automatic monitor selection (see refresh_window_offset). RUN_CONFIG is set
# in main() so the screen-grab path, which has no config argument, can use it.
RUN_CONFIG: dict = {}
_offset_checked_at = 0.0
_offset_missing_logged = False
# ── Screen capture backend ─────────────────────────────────────────────────────
# One persistent mss instance PER THREAD, created once and reused.
#
# Why this matters: mss.mss() acquires a Windows device context (GetWindowDC +
# CreateCompatibleDC + CreateCompatibleBitmap) on construction and releases it
# on close.  Building one per region meant 22 acquire/release cycles per
# capture, plus several per second once auto-capture is polling.  That churn
# against the desktop DC is a well-known trigger for the Desktop Window Manager
# dropping an exclusive-fullscreen Direct3D application into windowed mode —
# which shifts every configured region and breaks the capture.
#
# Reusing one context per thread removes the churn entirely and is also
# materially faster.  Thread-local because mss instances are NOT thread-safe
# and the watcher thread grabs concurrently with pipeline threads.

_capture_local = threading.local()
def get_sct():
    """Return this thread's persistent mss instance, creating it on first use."""
    sct = getattr(_capture_local, "sct", None)
    if sct is None:
        if mss is None:
            raise RuntimeError("the mss package is needed to grab the screen (pip install mss)")
        sct = mss.mss()
        _capture_local.sct = sct
        log.debug("Screen capture context created for thread %s",
                  threading.current_thread().name)
    return sct
def close_sct() -> None:
    """Release this thread's capture context, if it has one."""
    sct = getattr(_capture_local, "sct", None)
    if sct is not None:
        try:
            sct.close()
        except Exception:
            pass
        _capture_local.sct = None
def enable_dpi_awareness() -> None:
    """
    Declare the process DPI-aware before any capture happens.

    Without this, Windows silently virtualises coordinates for a non-aware
    process on a scaled display (125%, 150%, …).  Regions calibrated at the
    real resolution then land in the wrong place, and the mismatch between the
    reported and physical desktop size can itself provoke a fullscreen mode
    change.  Falls through three API generations, newest first, and is a no-op
    off Windows.
    """
    if sys.platform != "win32":
        return
    import ctypes
    # Per-Monitor-Aware v2 (Windows 10 1703+) — best behaviour
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
            log.debug("DPI awareness: per-monitor v2")
            return
    except (AttributeError, OSError):
        pass
    # Per-Monitor-Aware (Windows 8.1+)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        log.debug("DPI awareness: per-monitor v1")
        return
    except (AttributeError, OSError):
        pass
    # System-DPI-Aware (Vista+)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        log.debug("DPI awareness: system")
    except (AttributeError, OSError):
        log.debug("DPI awareness: unavailable")
def grab_frame(regions: list[dict]) -> tuple[Image.Image, int, int]:
    """
    refresh_window_offset(RUN_CONFIG)
    Capture ONE image covering every region, and return (image, left, top).

    Grabbing the regions one at a time spread a single "capture" across about
    two seconds. If GSPro updated its HUD partway through, one capture could mix
    values from two different moments — remaining distance from before a shot
    lands with the stroke counter from after it. One grab guarantees every
    field comes from the same instant, and is cheaper than 22 grabs.

    The grab covers the union of the regions rather than the whole monitor:
    still a single instant, with less memory to copy.
    """
    xs  = [r["x"] + window_x_offset for r in regions]
    ys  = [r["y"] for r in regions]
    x2s = [r["x"] + window_x_offset + r["width"]  for r in regions]
    y2s = [r["y"] + r["height"] for r in regions]
    left, top = min(xs), min(ys)
    width, height = max(x2s) - left, max(y2s) - top
    raw = get_sct().grab({"left": left, "top": top,
                           "width": width, "height": height})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"), left, top
def crop_region(frame: Image.Image, left: int, top: int, region: dict) -> Image.Image:
    """Cut one region out of a frame from grab_frame()."""
    x = region["x"] + window_x_offset - left
    y = region["y"] - top
    return frame.crop((x, y, x + region["width"], y + region["height"]))
def compute_window_offset(monitor_left: int, calibrated_left: int) -> int:
    """Capture x offset for a game on the monitor starting at monitor_left."""
    return int(monitor_left) - int(calibrated_left)
def refresh_window_offset(config: dict) -> None:
    """
    Point the capture at whichever monitor GSPro is on.

    The regions were drawn on one monitor (calibrated_monitor_left, by default
    window_x_shift). If GSPro is found on another, the offset follows it — so
    the daemon can no longer sit capturing the wrong screen. Checked at most
    every 2 s (finding the window takes about a millisecond). When GSPro is not
    found, the manual setting (Ctrl+Shift+F6) stays in force. Windows only.
    """
    global window_x_offset, _offset_checked_at, _offset_missing_logged
    if not config or not config.get("auto_window_offset", True) or not _FOLLOW_OK:
        return
    now = time.monotonic()
    if now - _offset_checked_at < 2.0:
        return
    _offset_checked_at = now
    cf = _cf
    pats = tuple((config.get("console_follow", {}) or {}).get(
        "title_patterns", cf.DEFAULT_TITLE_PATTERNS))
    hwnd = cf.find_window_by_title(pats)
    rect = cf.get_monitor_rect(hwnd) if hwnd else None
    if not rect:
        if not _offset_missing_logged and cf.IS_WINDOWS:
            log.info("Auto monitor: GSPro window not found — using the manual "
                     "setting (Ctrl+Shift+F6)")
            _offset_missing_logged = True
        return
    _offset_missing_logged = False
    calibrated = int(config.get("calibrated_monitor_left",
                                config.get("window_x_shift", 1920)))
    new = compute_window_offset(rect[0], calibrated)
    if new != window_x_offset:
        window_x_offset = new
        log.info("Auto monitor: GSPro is on the monitor at x=%d — capture offset "
                 "%+d px", rect[0], new)
def grab_monitor(regions: list[dict]) -> tuple[Image.Image, int, int]:
    """
    Capture the WHOLE monitor GSPro is on; return (image, left, top). In
    screenshot mode the pipeline uses this as its single frame, so the saved
    screenshot is exactly the image the OCR read, not a second grab.
    """
    refresh_window_offset(RUN_CONFIG)
    sct = get_sct()
    mon = screenshots.pick_monitor(list(sct.monitors), regions, window_x_offset)
    raw = sct.grab({"left": mon["left"], "top": mon["top"],
                    "width": mon["width"], "height": mon["height"]})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"), mon["left"], mon["top"]
def grab_region(region: dict) -> Image.Image:
    """
    refresh_window_offset(RUN_CONFIG)Capture a screen rectangle and return a PIL RGB image.

    All x-coordinates are shifted by window_x_offset so the same config
    works for both Window 1 (offset = -window_x_shift) and
    Window 2 (offset = 0, the default / config coordinates).

    Uses the thread's persistent capture context — see get_sct above for why
    that is important to GSPro staying in fullscreen.
    """
    x = region["x"] + window_x_offset
    y, w, h = region["y"], region["width"], region["height"]
    sct = get_sct()
    raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
