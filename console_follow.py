#!/usr/bin/env python3
"""
console_follow.py — Keep the daemon's console window positioned relative to GSPro.

The point is to be able to watch the log while playing without alt-tabbing.

── The constraint that shapes this whole module ──────────────────────────────
An exclusive-fullscreen Direct3D application drops to windowed mode the moment
another window takes foreground focus. Moving a window is precisely the kind of
operation that does that if done carelessly. So every call here passes
SWP_NOACTIVATE, and nothing in this module ever calls SetForegroundWindow,
BringWindowToTop, or ShowWindow with an activating flag.

If GSPro still leaves fullscreen while this is running, set GSPro to Borderless
Windowed (see README) — borderless has no exclusive mode to lose.

── Placement modes ───────────────────────────────────────────────────────────
  "opposite"  Console sits on the monitor GSPro is NOT on. Safest and the
              default: it can never cover the game, and on a two-monitor setup
              it is exactly where you want it.
  "beside"    Console is docked immediately to the left or right of the GSPro
              window, on whichever side has room.
  "corner"    Console overlays a corner of the GSPro window. Most convenient
              for a single monitor, but it sits above the game — only sensible
              in borderless windowed.
  "off"       Disabled.

Windows only. Every function is a no-op elsewhere.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ── Win32 constants ────────────────────────────────────────────────────────────

SWP_NOSIZE       = 0x0001
SWP_NOZORDER     = 0x0004
SWP_NOACTIVATE   = 0x0010   # the important one — never take focus
SWP_ASYNCWINDOWPOS = 0x4000

HWND_TOP       = 0
HWND_NOTOPMOST = -2

MONITOR_DEFAULTTONEAREST = 2

DEFAULT_TITLE_PATTERNS = ("gspro", "gs pro")


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


if IS_WINDOWS:
    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:                                   # pragma: no cover
    _user32 = _kernel32 = None


# ── window discovery ───────────────────────────────────────────────────────────

def get_console_hwnd() -> int:
    """
    Handle of this process's console window, or 0.

    Returns 0 when launched via pythonw / run_background.vbs, which have no
    console — the follower then simply does nothing.
    """
    if not IS_WINDOWS:
        return 0
    try:
        return int(_kernel32.GetConsoleWindow())
    except Exception:
        return 0


def find_window_by_title(patterns: tuple[str, ...]) -> int:
    """
    First visible top-level window whose title contains any pattern — never
    this daemon's own console. The console can be titled "GSPro OCR Daemon",
    which matches "gspro"; following or measuring it instead of the game would
    put the capture on the wrong monitor.
    """
    if not IS_WINDOWS:
        return 0
    found: list[int] = []
    pats = tuple(p.lower() for p in patterns)
    own = get_console_hwnd()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.lower()
        if int(hwnd) == own or "daemon" in title:
            return True
        if any(p in title for p in pats):
            found.append(int(hwnd))
            return False
        return True

    try:
        _user32.EnumWindows(_enum, 0)
    except Exception as exc:
        log.debug("console_follow: EnumWindows failed — %s", exc)
    return found[0] if found else 0


def get_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not IS_WINDOWS or not hwnd:
        return None
    r = RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def get_monitor_work_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """Work area (excludes taskbar) of the monitor a window is mostly on."""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        mon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)
    except Exception:
        return None


def get_monitor_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Full rectangle (not work area) of the monitor a window is mostly on."""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        mon = _user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        return (m.left, m.top, m.right, m.bottom)
    except Exception:
        return None


def list_monitor_work_areas() -> list[tuple[int, int, int, int]]:
    """Work areas of every monitor, left to right."""
    if not IS_WINDOWS:
        return []
    areas: list[tuple[int, int, int, int]] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM)

    @MONITORENUMPROC
    def _enum(hmon, _hdc, _lprc, _data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            w = mi.rcWork
            areas.append((w.left, w.top, w.right, w.bottom))
        return True

    try:
        _user32.EnumDisplayMonitors(None, None, _enum, 0)
    except Exception as exc:
        log.debug("console_follow: EnumDisplayMonitors failed — %s", exc)
    areas.sort(key=lambda a: a[0])
    return areas


# ── positioning ────────────────────────────────────────────────────────────────

def move_window_no_focus(hwnd: int, x: int, y: int,
                         w: int | None = None, h: int | None = None) -> bool:
    """
    Move (and optionally resize) a window WITHOUT activating it.

    SWP_NOACTIVATE is what makes this safe to run while a fullscreen game has
    focus. SWP_NOZORDER leaves the stacking order alone, so the console cannot
    jump above the game and provoke a mode change.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    flags = SWP_NOACTIVATE | SWP_NOZORDER | SWP_ASYNCWINDOWPOS
    if w is None or h is None:
        flags |= SWP_NOSIZE
        w = h = 0
    try:
        return bool(_user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(HWND_TOP),
            int(x), int(y), int(w), int(h), flags))
    except Exception as exc:
        log.debug("console_follow: SetWindowPos failed — %s", exc)
        return False


def compute_position(mode: str,
                     game_rect: tuple[int, int, int, int],
                     console_rect: tuple[int, int, int, int],
                     margin: int = 8) -> tuple[int, int] | None:
    """Where the console should sit, given the game's rect and the mode."""
    gl, gt, gr, gb = game_rect
    cl, ct, cr, cb = console_rect
    cw, ch = cr - cl, cb - ct

    if mode == "opposite":
        monitors = list_monitor_work_areas()
        if len(monitors) < 2:
            mode = "beside"            # single monitor: fall through
        else:
            gcx = (gl + gr) // 2
            # The monitor whose centre is furthest from the game's centre
            def dist(m):
                return abs(((m[0] + m[2]) // 2) - gcx)
            target = max(monitors, key=dist)
            tl, tt, tr, tb = target
            x = tl + max(0, ((tr - tl) - cw) // 2)
            y = tt + max(0, ((tb - tt) - ch) // 2)
            return (x, y)

    if mode == "beside":
        monitors = list_monitor_work_areas()
        bounds = monitors[0] if monitors else (0, 0, gr + cw + margin, gb)
        left_edge  = min(m[0] for m in monitors) if monitors else 0
        right_edge = max(m[2] for m in monitors) if monitors else bounds[2]
        if gr + margin + cw <= right_edge:
            return (gr + margin, gt)               # to the right of the game
        if gl - margin - cw >= left_edge:
            return (gl - margin - cw, gt)          # to the left
        return (max(left_edge, right_edge - cw), gt)

    if mode == "corner":
        return (gr - cw - margin, gt + margin)     # top-right inside the game

    return None


# ── follower thread ────────────────────────────────────────────────────────────

class ConsoleFollower:
    """
    Polls for the GSPro window and keeps the console positioned relative to it.

    Repositioning happens only when the game's rect actually changes, so a
    stationary window costs one cheap GetWindowRect per poll and nothing else.
    """

    def __init__(self, config: dict) -> None:
        cf = config.get("console_follow", {}) or {}
        self.mode      = str(cf.get("mode", "opposite")).lower()
        self.interval  = float(cf.get("poll_seconds", 2.0))
        self.margin    = int(cf.get("margin_px", 8))
        self.patterns  = tuple(cf.get("title_patterns", DEFAULT_TITLE_PATTERNS))
        self.size      = cf.get("size")     # optional [w, h]

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._console = get_console_hwnd()
        self._last_game_rect: tuple | None = None
        self._warned_missing = False

    def available(self) -> bool:
        if not IS_WINDOWS:
            return False
        if self.mode in ("off", "none", "disabled"):
            return False
        return bool(self._console)

    def start(self) -> bool:
        if not IS_WINDOWS:
            log.debug("console_follow: not Windows — skipped")
            return False
        if self.mode in ("off", "none", "disabled"):
            return False
        if not self._console:
            log.info("Console follow: no console window (launched via pythonw?) "
                     "— skipped. Use run_console.bat to see the log.")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="console-follow")
        self._thread.start()
        log.info("Console follow: ON — mode=%s, tracking %s every %.1fs "
                 "(never takes focus)",
                 self.mode, "/".join(self.patterns), self.interval)
        return True

    def stop(self, wait: bool = False, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if wait and t is not None and t is not threading.current_thread() and t.is_alive():
            t.join(timeout)

    def toggle(self) -> bool:
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            self.stop()
            log.info("Console follow: OFF")
            return False
        self.start()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.debug("console_follow: tick failed — %s", exc)
            self._stop.wait(self.interval)
        log.debug("console_follow: thread exited")

    def _tick(self) -> None:
        game = find_window_by_title(self.patterns)
        if not game:
            if not self._warned_missing:
                log.info("Console follow: GSPro window not found yet — waiting")
                self._warned_missing = True
            return
        self._warned_missing = False

        grect = get_rect(game)
        if not grect:
            return
        if grect == self._last_game_rect:
            return                              # nothing moved
        self._last_game_rect = grect

        crect = get_rect(self._console)
        if not crect:
            return
        if self.size and len(self.size) == 2:
            crect = (crect[0], crect[1],
                     crect[0] + int(self.size[0]), crect[1] + int(self.size[1]))

        pos = compute_position(self.mode, grect, crect, self.margin)
        if not pos:
            return
        w = h = None
        if self.size and len(self.size) == 2:
            w, h = int(self.size[0]), int(self.size[1])
        if move_window_no_focus(self._console, pos[0], pos[1], w, h):
            log.debug("console_follow: console → (%d, %d)", *pos)
