#!/usr/bin/env python3
"""
config_defaults.py — The daemon's default settings (moved out of ocr_daemon.py
in v3.8.7, so tools such as rebuild_from_screenshots.py can use them without
starting the daemon). Settings missing from config.json are taken from here.
"""

from __future__ import annotations

from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "_help": "Run python region_selector.py to draw regions interactively.",
    "hotkey":       "<ctrl>+<shift>+<f9>",
    "exit_hotkey":  "<ctrl>+<shift>+<f10>",
    "ods_file":     "ocr_output.ods",
    "tesseract_cmd":  "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
    "tesseract_lang": "eng",
    # ── Window switching ────────────────────────────────────────────────────────
    # Toggles all region x-coordinates between Window 2 (config coords, right)
    # and Window 1 (config coords − window_x_shift, left).
    # GSPro runs on Window 2; Window 1 is 1920 px to the LEFT of Window 2.
    "window_toggle_hotkey": "<ctrl>+<shift>+<f6>",
    "window_x_shift":       1920,
    # ── Automatic shot capture ──────────────────────────────────────────────────
    # When enabled the daemon watches auto_trigger_cell (the stroke ticker) and
    # fires the OCR pipeline itself once the screen changes and then settles.
    # The OCR hotkey still works at any time as a manual override.
    "auto_capture":           False,   # start with auto-capture on?
    "auto_capture_hotkey":    "<ctrl>+<shift>+<f11>",
    # Log every auto-capture decision at INFO — turn on for one session to see
    # why captures fire or are skipped, then off again.
    "auto_capture_verbose":   False,
    # Screenshots: the hotkey saves the GSPro screen whenever pressed; "every
    # capture" mode (toggle hotkey, --screenshots, or this setting) saves the
    # frame each capture read plus screens auto-capture skipped. png keeps the
    # exact pixels the OCR saw; jpg is about a tenth of the size.
    # Read the numeric HUD fields by matching GSPro's fixed fonts (~100x faster
    # than Tesseract). A font is matched only once all ten digits are learned;
    # until then, and whenever unsure, Tesseract is used exactly as before.
    "digit_templates":          True,
    # GSPro's end-of-round screen (T-28), in region coordinates (window 2), as
    # [x, y, width, height]: a white header band and the score box's dark top
    # edge identify it; "score" and "label" are the total and "SCORE" under it.
    "end_screen": {'band': [2140, 205, 720, 65], 'box_top': [2120, 750, 480, 20], 'score': [2303, 772, 112, 68], 'label': [2303, 842, 112, 24]},
    # Course, hole, par and yardage are re-read only when their pixels change
    # (T-13); any changed character always means a fresh read.
    # GSPro's "LAST SHOT" panel (T-08): after a shot from a penalising lie it
    # shows the ball speed and spin kept, e.g. 80% / 53% from deep rough.
    # [x, y, width, height] of each percentage, window-2 coordinates.
    "last_shot_panel":          {"speed": [3702, 604, 100, 44],
                                 "spin":  [3702, 686, 100, 44]},
    # Which way GSPro's wind arrow points: "to" = where the wind blows, "from" =
    # where it comes from. Not yet confirmed on a windy frame (see TODO).
    "wind_arrow_points":        "to",
    # Fields read by Tesseract share one run per group of settings (T-37);
    # false reads them one at a time, as before v3.8.9.
    "ocr_batch":                True,
    "ocr_cache":                True,
    "ocr_cache_cells":          ["A1", "B1", "C1", "D1"],
    # Report refreshes run in the background (T-10): a capture never waits for
    # the report. Requests within report_debounce_sec of each other are combined.
    "report_background":        True,
    "report_debounce_sec":      2.0,
    # HTML dashboard (dashboard.py), rewritten with every report refresh.
    "dashboard":                True,
    "dashboard_file":           "gspro_dashboard.html",
    "screenshot_hotkey":        "<ctrl>+<shift>+<f12>",
    "screenshot_toggle_hotkey": "<ctrl>+<shift>+<f4>",
    "screenshot_every_capture": False,
    "screenshot_dir":           "screenshots",
    "screenshot_format":        "png",
    # Follow GSPro to whichever monitor it is on. calibrated_monitor_left is the
    # left edge of the monitor the regions were drawn on.
    "auto_window_offset":     True,
    "calibrated_monitor_left": 1920,
    "console_follow_hotkey":  "<ctrl>+<shift>+<f5>",
    "auto_trigger_cell":      "V1",    # stroke ticker: increments once per shot
    "auto_poll_interval":     0.25,    # seconds between polls
    "auto_stable_polls":      6,       # identical polls required before firing
    "auto_cooldown_sec":      4.0,     # minimum seconds between auto-captures
    "auto_change_threshold":  6.0,     # mean pixel diff counted as a change
    # ── Exit behaviour ──────────────────────────────────────────────────────────
    # Some game modes advance to the next target distance as soon as a round
    # ends. If you quit before playing that hole out, its shots are not a real
    # result — discard them from the round totals rather than banking them.
    # Set false to keep partial holes (raw Sheet1 rows are kept either way).
    "discard_incomplete_hole_on_exit": True,
    # Minutes of inactivity that end a session. Round state survives daemon
    # restarts, so without this a break of hours merges into one "round".
    "session_gap_minutes": 90,
    # Upgrade an older-format report automatically at launch (a permanent copy
    # of the original is kept in backups/). If false, a warning is logged and
    # you run  python ocr_daemon.py --upgrade-ods  yourself.
    "auto_upgrade_ods": True,
    # The master record (v3.2+). The ODS report is generated from it.
    "db_file": "gspro_stats.db",
    # When to regenerate the report during play: "hole" (each finished hole)
    # or "exit" (only when the daemon stops). It is always written at exit.
    "report_refresh": "hole",
    # ── OCR ─────────────────────────────────────────────────────────────────────
    # Regions are OCR'd in parallel from a single frame. Each Tesseract call is
    # its own process, so more workers help up to roughly your CPU core count.
    "ocr_workers": 6,
    # Per-cell overrides for the built-in OCR profiles in ocr_vocab.py, e.g.
    #   "ocr_profiles": {"K1": {"psm": 7, "whitelist": "0123456789.ftyd"}}
    # Set "use_profile": false on a region to use its own tesseract_config.
    "ocr_profiles": {},
    # ── Console window following ────────────────────────────────────────────────
    # Keeps the log console positioned relative to the GSPro window so it can be
    # watched while playing. Repositioning never activates the window, so it
    # cannot pull focus away from a fullscreen game.
    #   mode: "opposite" (other monitor, safest) | "beside" | "corner" | "off"
    "console_follow": {
        "mode":           "opposite",
        "poll_seconds":   2.0,
        "margin_px":      8,
        "title_patterns": ["gspro", "gs pro"],
    },
    # Radius of GSPro's gimmie circle, in FEET — the unit GSPro itself uses.
    # A ball finishing inside it is conceded but has NOT holed: it is scored on
    # the distance remaining and the conceded putt is counted as its own stroke.
    "gimmie_zone_ft": 1.5,
    # Most strokes the round score may add to one hole at hole close. A larger
    # jump is treated as a misread or a reset and ignored.
    "max_reconcile_strokes": 5,
    # Log archiving: at log_max_mb the log moves into logs/ with a timestamp.
    # log_keep_archives = 0 keeps every archive (they are what rebuild replays).
    "log_max_mb": 5,
    "log_keep_archives": 0,
    # ── Capture validation bounds ───────────────────────────────────────────────
    # Lower the yardage floor further for chipping/putting practice modes; raise
    # max_hole_number for formats that number past 18.
    "validation": {
        "max_hole_number":  18,
        "min_hole_yardage": 5,
        "max_hole_yardage": 750,
    },
    # ── GSPro API v1 Connect ────────────────────────────────────────────────────
    "gspro_connect_hotkey": "<ctrl>+<shift>+<f7>",
    "gspro_shot_hotkey":    "<ctrl>+<shift>+<f8>",
    "gspro_host":           "127.0.0.1",
    "gspro_port":           921,
    "regions": [],
}
