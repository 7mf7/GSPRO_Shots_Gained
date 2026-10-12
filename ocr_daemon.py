#!/usr/bin/env python3
"""
ocr_daemon.py — Background OCR daemon + Strokes Gained calculator for GSPro.

Grabbing the screen lives in screen_capture.py and reading the HUD regions in
ocr_reader.py (split out in v3.8.5); this file runs the capture pipeline, the
hotkeys, the GSPro connection, screenshots, reports and the command line.

Every hotkey press (Ctrl+Shift+F9):
  1. Captures all configured screen regions via Tesseract OCR.
  2. Writes raw OCR text to Sheet1 of the ODS file (one row per press,
     row auto-increments as before).
  3. Runs the GSPro strokes-gained pipeline:
       • First press on a new hole  → sets up pre-shot state (tee, hole yardage).
         No SG is calculated yet; the shot hasn't been hit.
       • Every subsequent press     → calculates SG for the shot just completed
         and writes to SG_Shots / SG_Holes / SG_Summary sheets.
       • Remaining distance ≈ 0 → ball holed, hole is finalised.
         A ball inside the gimmie circle is NOT holed; it is scored on the
         distance left and the conceded putt is counted separately.

State is persisted in ocr_state.json so the daemon survives restarts mid-round.

CLI flags
---------
  --test        Dry-run: print OCR + SG output, save debug PNGs. No file writes.
  --once        Run pipeline once, write, exit.
  --reset       Reset row counter + round state back to zero, then exit.
  --new-round   Reset round state only (keeps raw-OCR row counter), then exit.
  --config      Path to config file (default: config.json).

Dependencies
------------
  pip install pytesseract Pillow mss pynput odfpy
  Tesseract binary: https://github.com/UB-Mannheim/tesseract/wiki
  strokes_gained.py must be in the same folder as this script.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
import re
from pathlib import Path
from datetime import datetime
from typing import Any

# ── third-party imports ────────────────────────────────────────────────────────

def _require(package: str, pip_name: str | None = None) -> Any:
    import importlib
    try:
        return importlib.import_module(package)
    except ImportError:
        sys.exit(f"Missing dependency — install with:  pip install {pip_name or package}")

_require("mss")
_require("pytesseract")
_require("PIL.Image",        "Pillow")
_require("pynput.keyboard",  "pynput")
_require("odf.opendocument", "odfpy")

import pytesseract
from PIL import Image
from pynput import keyboard

# ── local module ───────────────────────────────────────────────────────────────
try:
    import strokes_gained as sg_mod
except ImportError:
    sys.exit("strokes_gained.py not found — place it in the same folder as ocr_daemon.py")

try:
    import arrow_detector as _arrow
    _ARROW_OK = True
except ImportError:
    _ARROW_OK = False   # warned at startup; affected regions fall back to Tesseract

try:
    from shot_watcher import ShotWatcher
    _WATCHER_OK = True
except ImportError:
    _WATCHER_OK = False   # auto-capture unavailable; manual hotkey still works

try:
    from console_follow import ConsoleFollower
    _FOLLOW_OK = True
except ImportError:
    _FOLLOW_OK = False

import digit_reader
import ocr_reader
import screen_capture
import field_cache
import logrotate
import report_worker
import webbrowser
import screenshots
import db
import storage
import ods_upgrade
import parsing
import scoring
from scoring import _SG_SHOTS_HEADERS, _SG_HOLES_HEADERS, _fix_round, _zero_cats

try:
    import ocr_vocab as vocab
    _VOCAB_OK = True
except ImportError:
    _VOCAB_OK = False   # falls back to region tesseract_config, no snapping



_follower: "ConsoleFollower | None" = None

# Module-level watcher — created in main(), toggled by hotkey
_watcher: "ShotWatcher | None" = None

# Manual force-capture handshake.  When a manual capture is refused for lack of
# progression, pressing the hotkey again within FORCE_WINDOW_SEC records it
# anyway.  This keeps the guard strict while never locking the user out.
FORCE_WINDOW_SEC   = 8.0

# This many unreadable captures in a row suggests the regions no longer line up
# with GSPro's HUD (an update, window mode, display scaling) — warn once.
INVALID_STREAK_WARN = 8
_consecutive_invalid = 0
_force_next_capture = False
_force_offered_at   = 0.0

try:
    from gspro_api import GSProConnection
    _GSPRO_AVAILABLE = True
except ImportError:
    _GSPRO_AVAILABLE = False
    log_placeholder = None   # log not yet set up; warn later in main()

# Module-level connection object — created once, reused across hotkey presses
_gspro: "GSProConnection | None" = None


# Screenshots (see screenshots.py). _screenshots_on = save the frame of every
# capture, plus screens the watcher skipped. The manual hotkey works regardless.
_screenshots_on = False

_saver: "screenshots.ScreenshotSaver | None" = None

# ── paths (always relative to this script, not the CWD) ───────────────────────

from version import CHANGELOG, VERSION_STR


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE   = SCRIPT_DIR / "ocr_daemon.log"
STATE_FILE = SCRIPT_DIR / "ocr_state.json"
CONFIG_PATH: Path = SCRIPT_DIR / "config.json"   # may be overridden by --config

# ── logging ────────────────────────────────────────────────────────────────────

# Archives the log at 5 MB instead of deleting old entries: the log is the
# record rebuild_from_log.py replays. Size and retention come from config.json.
_log_handlers: list[logging.Handler] = [logrotate.ArchivingFileHandler(LOG_FILE)]
if sys.stdout is not None:
    _log_handlers.insert(0, logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",   # dated, so logs replay exactly across days
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)

# ── default config ─────────────────────────────────────────────────────────────

from config_defaults import DEFAULT_CONFIG    # noqa: E402  (v3.8.7)

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        log.info("Created default config.json")
    user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Settings added in later versions are filled in from the defaults, so an
    # existing config.json picks them up; anything the user set (regions
    # included) is left exactly as it is (v3.8.2).
    missing = [k for k in DEFAULT_CONFIG if k not in user]
    if missing:
        log.info("config.json: using defaults for newer settings: %s", ", ".join(missing))
    return {**DEFAULT_CONFIG, **user}

# ── round state defaults ───────────────────────────────────────────────────────


# ── state I/O ──────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()


# ── Database (the master record since v3.2.0) ─────────────────────────────────

_DB: "db.Database | None" = None


def _db_path(config: dict | None = None) -> Path:
    p = Path((config or {}).get("db_file", "gspro_stats.db"))
    return p if p.is_absolute() else SCRIPT_DIR / p


def _open_database(config: dict) -> "db.Database":
    """Open (or create) the database named in config, recovering from damage."""
    global _DB
    path = _db_path(config)
    if _DB is None or _DB.path != path:
        if _DB is not None:
            _DB.close()
        _DB = db.open_database(path)
    return _DB


def _db() -> "db.Database":
    """The open database; opens the default one if main() has not yet."""
    global _DB
    if _DB is None:
        _DB = db.open_database(_db_path())
    return _DB


class DbSink(db.DatabaseSink):
    """The live engine sink: writes into the capture's database transaction."""

    def __init__(self) -> None:
        super().__init__(_db())

    def archive_session(self, round_state: dict) -> list:
        self.archived = True
        return _archive_round(round_state)


def export_report(config: dict) -> bool:
    """Regenerate the ODS report and the HTML dashboard from the database."""
    ok = _export_ods(config)
    write_dashboard(config)          # written even when the ODS is open in LibreOffice
    return ok


# Fields re-read only when their pixels change (T-13, v3.8.4); created on first use.
_FIELDS: "field_cache.FieldCache | None" = None
_FIELDS_LOCK = threading.Lock()


def _field_cache(config: dict) -> "field_cache.FieldCache | None":
    global _FIELDS
    if not config.get("ocr_cache", True):
        return None
    with _FIELDS_LOCK:
        if _FIELDS is None:
            _FIELDS = field_cache.FieldCache(config.get("ocr_cache_cells", ["A1", "B1", "C1", "D1"]))
        return _FIELDS


# The background report worker (T-10, v3.8.3); created on first use.
_REPORTS: "report_worker.ReportWorker | None" = None
_REPORTS_LOCK = threading.Lock()


def _request_report(config: dict, reason: str = "") -> None:
    """
    Refresh the report and dashboard. In the background by default, so the
    capture that asked carries on at once; refreshes that arrive close together
    are combined. "report_background": false restores the old synchronous refresh.
    """
    global _REPORTS
    if not config.get("report_background", True):
        export_report(config)
        return
    with _REPORTS_LOCK:
        if _REPORTS is None:
            _REPORTS = report_worker.ReportWorker(
                lambda: export_report(config),
                debounce=float(config.get("report_debounce_sec", 2.0)))
    _REPORTS.request(reason)


def _stop_report_worker(timeout: float = 120.0) -> None:
    """Let a refresh in progress (and any still requested) finish, then stop."""
    global _REPORTS
    with _REPORTS_LOCK:
        worker, _REPORTS = _REPORTS, None
    if worker is None:
        return
    if worker.busy:
        log.info("Finishing the background report refresh…")
    if not worker.stop(timeout):
        log.warning("Background report refresh still running after %.0fs — the "
                    "final report is written anyway", timeout)
    log.info("Report: %s", worker.summary())


def _dashboard_path(config: dict) -> Path:
    p = Path(config.get("dashboard_file", "gspro_dashboard.html"))
    return p if p.is_absolute() else SCRIPT_DIR / p


def write_dashboard(config: dict) -> Path | None:
    """Write the HTML dashboard (dashboard.py) from the database. Never raises."""
    if not config.get("dashboard", True):
        return None
    path = _dashboard_path(config)
    try:
        page = db.dashboard_html(_db(), load_state()["round"], VERSION_STR)
        storage.atomic_write_bytes(path, page.encode("utf-8"), backups=0)
        log.info("Dashboard refreshed → %s", path.name)
        return path
    except Exception as exc:
        log.error("Dashboard not refreshed: %s", exc)
        return None


def _export_ods(config: dict) -> bool:
    """
    Regenerate the ODS report from the database. Safe at any time: if the file
    is open in LibreOffice the refresh is skipped and nothing is lost — the
    data is in the database and the next refresh writes it.
    """
    path = _ods_path(config)
    try:
        sheets = db.report_sheets(_db(), load_state()["round"], VERSION_STR)
        _ods_save(path, sheets)
        log.info("Report refreshed from the database → %s", path.name)
        return True
    except storage.FileLockedError:
        log.warning("Report not refreshed: %s is open in another program. Your data "
                    "is safe in the database; it refreshes next time.", path.name)
    except Exception as exc:
        log.error("Report not refreshed: %s", exc)
    return False


def _migrate_legacy_files(config: dict) -> None:
    """First launch on v3.2: move the JSON files and report rows into the database."""
    rep = db.migrate_legacy(_db(), STATE_FILE, HISTORY_FILE, _ods_path(config),
                            _upgrade_ctx())
    if rep["status"] == "migrated":
        log.warning("━━━  Data moved into the database (%s)  ━━━━━━━━━━━━━━━━━━━━",
                    _db().path.name)
        log.info("  report rows %s, %d session(s), state %s", rep["rows"],
                 rep["sessions"], "imported" if rep["state"] else "none")
        log.info("  ocr_state.json / round_history.json renamed *.migrated "
                 "(copies in backups/); the report is now generated from the database")


def _default_state() -> dict:
    return {"row_offset": 0, "round": _fix_round({}),
            "window_x_offset": 0, "last_capture": None}


def load_state() -> dict:
    """
    The daemon state, from the database. Inside a capture this sees the
    capture's own uncommitted changes, so the engine reads one consistent
    state, and a crash mid-capture rolls state and rows back together.
    """
    raw = _db().get_state()
    if raw is None:
        return _default_state()
    raw.setdefault("row_offset", 0)
    raw.setdefault("window_x_offset", 0)
    raw.setdefault("last_capture", None)
    raw["round"] = _fix_round(raw.get("round", {}))
    return raw


def save_state(state: dict) -> None:
    """
    Write the state. Inside a capture's transaction it commits with the rows,
    and a failure rolls the whole capture back. Outside one (e.g. the window
    toggle) a failure is logged.

    No separate state lock: the database lock serialises everything. Taking a
    state lock here as well would invert lock order against a capture (which
    holds the database lock and then saves state) and could deadlock.
    """
    d = _db()
    if d.in_transaction:
        d.set_state(state)
        return
    try:
        d.set_state(state)
    except sqlite3.Error as exc:
        log.error("State not saved: %s", exc)


def reset_state() -> None:
    """Reset everything: row counter + round state."""
    save_state({"row_offset": 0, "round": _fix_round({})})
    log.info("Full state reset — round state cleared. Row positions are re-derived "
             "from the report at next launch, so no existing rows are overwritten. "
             "To start a fresh report, rename or delete the .ods file.")


def reset_round_only() -> None:
    """Reset round state but keep raw-OCR row counter."""
    state = load_state()
    state["round"] = _fix_round({})
    state["last_capture"] = None      # a new round starts with no prior state
    save_state(state)
    log.info("Round state reset — raw-OCR row counter preserved at %d.", state["row_offset"])

# ── hotkey normaliser ──────────────────────────────────────────────────────────

_FKEY_RE = re.compile(r"^[Ff](\d{1,2})$")

def normalize_hotkey(hotkey: str) -> str:
    parts = []
    for part in hotkey.split("+"):
        part = part.strip()
        if _FKEY_RE.match(part):
            part = f"<{part.lower()}>"
            log.warning("Hotkey part %r auto-corrected — update config.json to silence this", part)
        parts.append(part)
    return "+".join(parts)

# ── cell address helpers ───────────────────────────────────────────────────────


def col_letter(col: int) -> str:
    result, n = "", col + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def cell_label(row: int, col: int) -> str:
    return f"{col_letter(col)}{row + 1}"

# ── ODS helpers ────────────────────────────────────────────────────────────────

_ods_lock = threading.Lock()


# All spreadsheet I/O goes through storage.py, which writes atomically, keeps
# rotating backups in backups/, and recovers from a damaged file instead of
# silently starting a fresh one.
ODS_BACKUPS = 2


def _ods_path(config: dict) -> Path:
    """The report file, resolved the same way everywhere."""
    p = Path(config.get("ods_file", "ocr_output.ods"))
    return p if p.is_absolute() else SCRIPT_DIR / p


def _ods_read(fp: Path) -> dict[str, list[list[str]]]:
    """Read the report; {} for a file that does not exist yet. Raises
    storage.StorageError if it is locked or unrecoverably damaged."""
    return storage.ods_read(fp, backups=ODS_BACKUPS) or {}


def _ods_save(fp: Path, data: dict[str, list[list[str]]]) -> None:
    storage.ods_save(fp, data, backups=ODS_BACKUPS)


def _archive_round(r: dict) -> list[dict]:
    """Archive a finished session into the database; return every session."""
    sessions = db.archive_round(_db(), r)
    if r.get("holes_completed", 0) > 0:
        log.info("Session archived (%d session(s) on file)", len(sessions))
    return sessions


# ── SG sheet helpers ───────────────────────────────────────────────────────────

# ── Timestamps ─────────────────────────────────────────────────────────────────
# Every row written to every sheet carries the date and time it was acquired.
# Date and time are kept in SEPARATE columns so spreadsheet software can group,
# filter and pivot on either one without string parsing.


# ── Round history (persists across daemon sessions) ────────────────────────────
# Completed rounds are archived here so SG_Summary can report week-to-week
# trends and Round Stats can list one row per round.  Kept separate from
# ocr_state.json because state is reset by --new-round while history is not.

HISTORY_FILE = SCRIPT_DIR / "round_history.json"


def load_history() -> list[dict]:
    """Archived sessions, from the database."""
    return _db().load_sessions()


def save_history(rounds: list[dict]) -> None:
    _db().save_sessions(rounds)


# ── OCR field parser ───────────────────────────────────────────────────────────


# ── Shot state machine ─────────────────────────────────────────────────────────


# ── Round statistics sheet ─────────────────────────────────────────────────────

def _score_label(vs_par: int) -> str:
    if vs_par == 0:  return "Even par"
    if vs_par == -1: return "1 under par"
    if vs_par <  0:  return f"{abs(vs_par)} under par"
    if vs_par ==  1: return "1 over par"
    return f"{vs_par} over par"


def _screenshot_dir(config: dict) -> Path:
    p = Path(config.get("screenshot_dir", "screenshots"))
    return p if p.is_absolute() else SCRIPT_DIR / p


def _get_saver(config: dict) -> "screenshots.ScreenshotSaver":
    global _saver
    if _saver is None:
        _saver = screenshots.ScreenshotSaver(config.get("screenshot_format", "png"))
    return _saver


def _save_screenshot(config: dict, image: Image.Image, label: str) -> None:
    """Queue *image* for saving under a timestamped name and log the path."""
    saver = _get_saver(config)
    path = screenshots.screenshot_path(_screenshot_dir(config), datetime.now(), label, saver.fmt)
    if saver.submit(image, path):
        shown = path.relative_to(SCRIPT_DIR) if path.is_relative_to(SCRIPT_DIR) else path
        log.info("  📷  screenshot → %s", shown)


def _save_capture_screenshot(config: dict, frame, source: str, by_cell: dict,
                             outcome: str, dry_run: bool) -> None:
    """In screenshot mode, save the frame a capture just read, named by its result."""
    if not _screenshots_on or dry_run or frame is None:
        return
    hole = "".join(ch for ch in by_cell.get("B1", "") if ch.isdigit()) or "x"
    stroke = (by_cell.get("V1", "") or "x").strip() or "x"
    _save_screenshot(config, frame, f"{source}_h{hole}_s{stroke}_{outcome}")


def _do_screenshot(config: dict) -> None:
    """The screenshot hotkey: save the GSPro screen now."""
    try:
        image, _, _ = screen_capture.grab_monitor(config.get("regions", []))
        _save_screenshot(config, image, "manual")
    except Exception as exc:
        log.error("Screenshot failed: %s", exc)
    finally:
        screen_capture.close_sct()           # this thread is short-lived; release its capture context


def _set_screenshot_mode(config: dict, on: bool) -> None:
    global _screenshots_on
    _screenshots_on = on
    if on:
        log.warning("📷  Screenshot every capture: ON — frames saved to %s "
                    "(about 2–4 MB each as PNG)", _screenshot_dir(config))
    else:
        log.warning("📷  Screenshot every capture: OFF")


def _do_screenshot_toggle(config: dict) -> None:
    _set_screenshot_mode(config, not _screenshots_on)


def _on_watcher_settle(config: dict, decision: str) -> None:
    """Save screens the watcher settled on but did not capture — where missed shots hide."""
    if not _screenshots_on:
        return
    try:
        image, _, _ = screen_capture.grab_monitor(config.get("regions", []))
        _save_screenshot(config, image, f"settle_{decision}")
    except Exception as exc:
        log.debug("Settle screenshot failed: %s", exc)


def _then_release(fn, *args) -> None:
    """Run fn in a short-lived hotkey thread, then release the thread's capture
    context. Without this every manual capture left one behind."""
    try:
        fn(*args)
    finally:
        screen_capture.close_sct()


# ── pipeline lock ──────────────────────────────────────────────────────────────

_pipeline_lock = threading.Lock()

# Set when the daemon starts shutting down: no capture may start after it, from
# the watcher, a hotkey or anything else (v3.8.11).
_SHUTTING_DOWN = threading.Event()


def _quiesce_captures(timeout: float = 30.0) -> bool:
    """
    Make sure no capture is running, and none can start, before the exit
    reports are written: refuse new captures, stop auto-capture and wait for a
    capture it has already started, then wait for a manual capture in progress
    by taking the pipeline lock — which the caller then holds until exit.

    Before v3.8.11 the exit reports were written while a capture could still be
    running, and Python could shut down underneath it: the capture was lost
    ("cannot schedule new futures after interpreter shutdown"), its log message
    could abort Python ("Fatal Python error … stdout … daemon threads"), and
    the capture and the exit reports could both write the round state.
    Returns True if the pipeline lock is now held.
    """
    _SHUTTING_DOWN.set()
    t0 = time.monotonic()
    if _watcher is not None:
        if not _watcher.stop(wait=True, timeout=timeout):
            log.warning("Auto-capture did not stop within %.0fs", timeout)
    left = max(0.1, timeout - (time.monotonic() - t0))
    if _pipeline_lock.locked():
        log.info("Finishing the capture in progress…")
    if _pipeline_lock.acquire(timeout=left):
        return True
    log.warning("A capture is still running after %.0fs — writing the exit reports "
                "anyway", timeout)
    return False

# ── full pipeline ──────────────────────────────────────────────────────────────


def run_pipeline(config: dict, dry_run: bool = False, source: str = "manual") -> None:
    """
    Run one full capture.

    source="auto"   — fired by ShotWatcher.  Progression is enforced strictly;
                      a replay or camera move is discarded silently.
    source="manual" — fired by the OCR hotkey.  A capture that shows no
                      progression is refused once, but pressing the hotkey
                      again within FORCE_WINDOW_SEC overrides the check, so a
                      deliberate re-capture is always possible.
    """
    global _force_next_capture, _force_offered_at, _consecutive_invalid
    if _force_next_capture and (time.monotonic() - _force_offered_at) > FORCE_WINDOW_SEC:
        _force_next_capture = False   # offer expired
    if _SHUTTING_DOWN.is_set():
        log.info("Shutting down — capture not started")
        return
    if not _pipeline_lock.acquire(blocking=False):
        log.warning("Pipeline already running — hotkey press ignored")
        return

    try:
        state    = load_state()
        offset   = state["row_offset"]
        lang     = config.get("tesseract_lang", "eng")

        log.info("━━━  OCR pipeline  (%s)  raw-row=%d  ━━━━━━━━━━━━━━━━━━━━━━━━━",
                 datetime.now().strftime("%H:%M:%S"), offset)

        # ── 1. OCR all regions into memory (NO writes yet) ───────────────────
        # Collecting first, then validating, then writing ONE row is a
        # deliberate change from the old write-as-you-go loop:
        #   * a rejected capture leaves no partial row behind
        #   * one ODS read/save instead of 24 (write_cell reloads the whole
        #     document every call) — roughly 20x faster per capture
        # by_cell keyed by cell address; by_name kept for name-addressed fields.
        by_cell: dict[str, str] = {}
        by_name: dict[str, str] = {}
        errors = 0
        regions = config.get("regions", [])

        # ONE grab for the whole capture, so every field is from the same instant.
        t0 = time.perf_counter()
        try:
            frame, f_left, f_top = (screen_capture.grab_monitor(regions) if _screenshots_on
                                    else screen_capture.grab_frame(regions))
        except Exception as exc:
            log.error("  Frame capture failed: %s", exc)
            return
        t_grab = time.perf_counter() - t0
        if dry_run:
            frame.save(SCRIPT_DIR / "debug_frame.png")
            log.info("  Debug full frame → %s", SCRIPT_DIR / "debug_frame.png")

        hole_par = state["round"].get("hole_par", 5)
        cache = None if dry_run else _field_cache(config)
        # The same reading code rebuild_from_screenshots.py uses (v3.8.7).
        reads, ends, errors, t_ocr, workers = ocr_reader.read_frame(
            config, frame, f_left, f_top, hole_par, lang, cache=cache, debug=dry_run)
        ocr_reader.log_reads(reads)
        for name, cell, text, _ in reads:
            by_cell[cell] = text
            by_name.setdefault(name.lower(), text)

        log.info("  timing: grab %.0f ms, OCR %.0f ms across %d regions "
                 "(%d workers)", t_grab * 1000, t_ocr * 1000, len(regions), workers)

        # GSPro's end-of-round screen (T-28): logged like any region, so a
        # rebuild from the log sees exactly what the live run saw.
        ocr_reader.log_reads(ends)
        for name, cell, text, _ in ends:
            by_cell[cell] = text

        # ── 2. Decide and apply — the SAME engine rebuild_from_log.py uses ────
        # One transaction: raw row, shot rows, hole rows and state commit
        # together or not at all. State is re-read inside it so the
        # read-modify-write is atomic.
        forced = _force_next_capture
        sink = DbSink()
        try:
            with _db().transaction():
                state = load_state()
                holes_before = state["round"].get("holes_completed", 0)
                state, outcome, reason = scoring.handle_capture(
                    state, by_cell, by_name, sink, config,
                    forced=forced, dry_run=dry_run)
                if not dry_run:
                    save_state(state)
        except Exception as exc:
            log.error("  ✗  Capture not recorded — %s. Nothing was saved; it is still "
                      "in the log for rebuild_from_log.py.", exc)
            _save_capture_screenshot(config, frame, source, by_cell, "error", dry_run)
            return
        _save_capture_screenshot(config, frame, source, by_cell, outcome, dry_run)
        if outcome == "invalid":
            _consecutive_invalid += 1
            if _consecutive_invalid == INVALID_STREAK_WARN:
                log.warning("  ⚠️  %d unreadable captures in a row. Normal on menus; "
                            "if you are playing a hole, the capture regions may have "
                            "moved. Run  python ocr_daemon.py --check-regions",
                            INVALID_STREAK_WARN)
        else:
            _consecutive_invalid = 0
            _force_next_capture = False      # consumed once a capture passes validation
        if outcome == "invalid":
            return
        if outcome == "no_progression":
            if source == "manual":
                _force_offered_at = time.monotonic()
                _force_next_capture = True
                log.warning("  ✗  Capture refused: %s", reason)
                log.warning("     Press the OCR hotkey again within %.0fs to "
                            "record it anyway.", FORCE_WINDOW_SEC)
            else:
                log.info("  ↩  Skipped: %s", reason)
            return
        # Refresh the report when a hole finished or a session was archived —
        # after the commit, so it never shows uncommitted data.
        if not dry_run and config.get("report_refresh", "hole") == "hole" and (
                sink.archived or state["round"].get("holes_completed", 0) != holes_before):
            _request_report(config, "hole finished")     # background: never delays a capture

        status = "✅ done" if errors == 0 else f"⚠️  done ({errors} OCR error(s))"
        log.info("━━━  %s  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", status)

    finally:
        _pipeline_lock.release()

# ── GSPro API helpers ──────────────────────────────────────────────────────────

def _do_window_toggle(config: dict) -> None:
    """Toggle capture coordinates between Window 2 (GSPro, right) and Window 1 (left)."""
    shift = int(config.get("window_x_shift", 1920))
    if screen_capture.window_x_offset == 0:
        screen_capture.window_x_offset = -shift                          # shift LEFT to Window 1
        label = f"Window 1  (x offset −{shift} px, left screen)"
    else:
        screen_capture.window_x_offset = 0                               # back to Window 2 / GSPro
        label = "Window 2  (config coordinates, right screen — GSPro)"
    log.info("━━━  Window switched → %s  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", label)
    state = load_state()
    state["window_x_offset"] = screen_capture.window_x_offset
    save_state(state)


def _auto_precheck(config: dict) -> bool:
    """
    Cheap gate run by ShotWatcher before the full pipeline.

    Reads only two regions — the stroke ticker (OpenCV, no Tesseract) and the
    hole number (one Tesseract call) — and compares them to the last accepted
    capture.  A replay leaves both unchanged, so it is discarded here for a few
    milliseconds instead of after a full 22-region OCR pass.

    Returns True when the full pipeline should run.  Errs on the side of True:
    if anything cannot be read, the pipeline runs and applies the full check.
    """
    try:
        state = load_state()
        last  = state.get("last_capture")
        if not last:
            return True                      # nothing to compare against yet

        regions = {str(r.get("cell", "")).upper(): r for r in config.get("regions", [])}
        lang    = config.get("tesseract_lang", "eng")

        # Hole number — a change here is always progression
        hole = 0
        if "B1" in regions:
            txt, _ = ocr_reader.capture_and_ocr(regions["B1"], lang)
            hole   = sg_mod.parse_int(txt, 0)

        def _ticker():                       # read only if the hole is unchanged
            if "V1" not in regions or not _ARROW_OK:
                return None
            img = screen_capture.grab_region(regions["V1"])
            return _arrow.detect_stroke_ticker(img, hole_par=state["round"].get("hole_par", 5))

        if ocr_reader.precheck_says_capture(last, hole, _ticker):
            return True
        log.info("  ↩  Auto-capture skipped: hole %s stroke %s unchanged "
                 "(replay or camera move)", hole, last.get("ticker", 0) or 0)
        return False
    except Exception as exc:
        log.debug("Auto pre-check failed (%s) — running full pipeline", exc)
        return True


def _check_regions(config: dict, save_crops: bool = False) -> bool:
    """
    Read hole, par and yardage from the screen once to confirm the regions line
    up with GSPro's HUD. With save_crops, every region's image is saved to
    region_check/ and its OCR text printed — the tool for after a GSPro update.
    Returns True if hole and par were read.
    """
    regions = config.get("regions", [])
    try:
        frame, left, top = screen_capture.grab_frame(regions)
    except Exception as exc:
        log.warning("Region check: screen capture failed — %s", exc)
        return False
    lang = config.get("tesseract_lang", "eng")
    key = {"A1", "B1", "C1", "D1"}
    outdir = SCRIPT_DIR / "region_check"
    by_cell: dict[str, str] = {}
    for rg in regions:
        cell = str(rg.get("cell", "")).upper()
        if not save_crops and cell not in key:
            continue
        img = screen_capture.crop_region(frame, left, top, rg)
        name = rg.get("name", cell)
        if save_crops:
            outdir.mkdir(exist_ok=True)
            safe = "".join(ch if ch.isalnum() else "_" for ch in name)
            img.save(outdir / f"{cell}_{safe}.png")
        if cell == "V1":
            continue
        try:
            text, _ = ocr_reader.ocr_image(rg, img, lang)
        except Exception as exc:
            text = f"<error: {exc}>"
        by_cell[cell] = text
        if save_crops:
            print(f"  {cell:<4} {name:<28} {text!r}")
    p = parsing.parse_ocr_fields(by_cell, {})
    ok = p["hole"] > 0 and 3 <= p["par"] <= 5
    if ok:
        log.info("Region check: hole %d, par %d, %.0f yd read from the screen ✓",
                 p["hole"], p["par"], p["hole_yardage"])
    else:
        log.warning("Region check: hole/par not readable (read %r / %r). Fine if GSPro "
                    "is on a menu. If you are on a hole, the regions may have moved — "
                    "a GSPro update, window mode or display scaling. Run  "
                    "python ocr_daemon.py --check-regions  to see what each region "
                    "captures.", by_cell.get("B1", ""), by_cell.get("C1", ""))
    if save_crops:
        print(f"\nRegion images saved to {outdir}")
    return ok


def _write_exit_reports(config: dict) -> None:
    """
    End of run: settle the open hole (finalise it if over, discard it if still
    in play), archive the session — one database transaction — then regenerate
    the report from the database.
    """
    with _db().transaction():
        final_state = load_state()
        r = final_state["round"]
        scoring.close_open_hole(
            r, DbSink(),
            discard_incomplete=config.get("discard_incomplete_hole_on_exit", True))
        if r.get("holes_completed", 0) > 0:
            _archive_round(r)
        final_state["round"] = r
        save_state(final_state)
    _stop_report_worker()            # any background refresh finishes first
    export_report(config)


def _upgrade_ctx() -> dict:
    return {"headers": {"SG_Shots": list(_SG_SHOTS_HEADERS),
                        "SG_Holes": list(_SG_HOLES_HEADERS)},
            "writer": VERSION_STR}


def _upgrade_report(path: Path) -> dict:
    """Upgrade one report file and log the outcome. Returns the upgrade report."""
    try:
        rep = ods_upgrade.upgrade_file(path, _upgrade_ctx())
    except storage.StorageError as exc:
        log.error("Report upgrade failed for %s: %s", path, exc)
        return {"status": "error", "notes": [str(exc)]}
    st = rep["status"]
    if st == "upgraded":
        log.warning("━━━  Report upgraded: format %s → %s  ━━━━━━━━━━━━━━━━━━━━━",
                    rep["from"], rep["to"])
        for n in rep["notes"]:
            log.info("  • %s", n)
        log.info("  Original kept at %s", rep.get("backup"))
    elif st == "current":
        log.info("Report %s is already format %s.", path.name, rep["to"])
    elif st == "newer":
        log.warning("Report %s: %s", path.name, rep["notes"][0])
    elif st == "missing":
        log.info("No report at %s yet — one will be created.", path)
    return rep


def _reconcile_row_counters(config: dict) -> None:
    """
    Take the next-free-row positions from the database's rows. State and rows
    commit together since v3.2, so they cannot drift during play; this remains
    a cheap guard, and settles positions straight after a migration.
    """
    d = _db()
    with d.transaction():
        state = load_state()
        r = state["round"]
        want = {"row_offset":   d.sheet_len("Sheet1"),
                "sg_shots_row": max(1, d.sheet_len("SG_Shots")),
                "sg_holes_row": max(1, d.sheet_len("SG_Holes"))}
        have = {"row_offset":   state.get("row_offset", 0),
                "sg_shots_row": r.get("sg_shots_row", 1),
                "sg_holes_row": r.get("sg_holes_row", 1)}
        changed = {k: (have[k], want[k]) for k in want if have[k] != want[k]}
        if changed:
            state["row_offset"] = want["row_offset"]
            r["sg_shots_row"]   = want["sg_shots_row"]
            r["sg_holes_row"]   = want["sg_holes_row"]
            save_state(state)
    for k, (a, b) in changed.items():
        log.warning("Row position %s corrected %d → %d (taken from the database)", k, a, b)


def _startup_storage_checks(config: dict) -> None:
    """Run once at launch, before any capture can write."""
    for f in storage.find_quarantined(SCRIPT_DIR):
        log.warning("UNRESOLVED damaged file preserved at %s — it was replaced by "
                    "a backup or a fresh file. Delete it once you have checked it.", f)
    _db().snapshot("session-start", keep=5)
    # Stored rows must match the current layout before any capture writes.
    for note in db.upgrade_report_rows(_db(), _upgrade_ctx()):
        log.warning("Database rows upgraded: %s", note)
    _reconcile_row_counters(config)
    # The report is generated from the database, so it is always written in the
    # current format — the live report never needs an upgrade step. In the
    # background, so capturing can start straight away on a large database.
    _request_report(config, "start-up")


def _do_follow_toggle(config: dict) -> None:
    """Turn console-follow on or off."""
    global _follower
    if not _FOLLOW_OK:
        log.error("console_follow.py not found — window following unavailable")
        return
    if _follower is None:
        _follower = ConsoleFollower(config)
    _follower.toggle()


def _do_auto_toggle(config: dict) -> None:
    """Turn automatic shot capture on or off."""
    global _watcher
    if not _WATCHER_OK:
        log.error("shot_watcher.py not found — auto-capture unavailable")
        return
    if _watcher is None:
        _watcher = ShotWatcher(
            config,
            pipeline_fn=lambda: run_pipeline(config, source="auto"),
            grab_fn=screen_capture.grab_region,
            state_fn=load_state,
            pre_check_fn=lambda: _auto_precheck(config),
            cleanup_fn=screen_capture.close_sct,
            on_settle=lambda decision: _on_watcher_settle(config, decision),
        )
    if not _watcher.available():
        log.error("Auto-capture needs a region with cell %s in config.json",
                  config.get("auto_trigger_cell", "V1"))
        return
    _watcher.toggle()


def _gspro_connect(config: dict) -> None:
    """Establish (or re-establish) the TCP connection to GSPro."""
    global _gspro
    if not _GSPRO_AVAILABLE:
        log.error("gspro_api.py not found — cannot connect to GSPro")
        return
    if _gspro is None:
        _gspro = GSProConnection(
            host=config.get("gspro_host", "127.0.0.1"),
            port=int(config.get("gspro_port", 921)),
        )
    if _gspro.is_connected():
        log.info("GSPro: already connected — press Ctrl+Shift+F8 to fire a debug shot")
        return
    _gspro.connect()


def _gspro_debug_shot() -> None:
    """Send the predefined debug shot to GSPro (must be connected first)."""
    if not _GSPRO_AVAILABLE:
        log.error("gspro_api.py not found — cannot send shot")
        return
    if _gspro is None or not _gspro.is_connected():
        log.warning("GSPro: not connected — press Ctrl+Shift+F7 to connect first")
        return
    _gspro.send_debug_shot()


# ── hotkey listener ────────────────────────────────────────────────────────────

def build_listener(config: dict) -> tuple[keyboard.GlobalHotKeys, threading.Event]:
    stop_evt = threading.Event()

    def on_ocr() -> None:
        threading.Thread(target=_then_release, args=(run_pipeline, config), daemon=True).start()

    def on_screenshot() -> None:
        threading.Thread(target=_do_screenshot, args=(config,), daemon=True).start()

    def on_screenshot_toggle() -> None:
        threading.Thread(target=_do_screenshot_toggle, args=(config,), daemon=True).start()

    def on_exit() -> None:
        log.info("Exit hotkey pressed — shutting down…")
        stop_evt.set()

    def on_window_toggle() -> None:
        threading.Thread(target=_do_window_toggle, args=(config,), daemon=True).start()

    def on_follow_toggle() -> None:
        threading.Thread(target=_do_follow_toggle, args=(config,), daemon=True).start()

    def on_auto_toggle() -> None:
        threading.Thread(target=_do_auto_toggle, args=(config,), daemon=True).start()

    def on_gspro_connect() -> None:
        threading.Thread(target=_gspro_connect, args=(config,), daemon=True).start()

    def on_gspro_shot() -> None:
        threading.Thread(target=_gspro_debug_shot, daemon=True).start()

    mappings = {
        normalize_hotkey(config["hotkey"]):                                               on_ocr,
        normalize_hotkey(config.get("exit_hotkey",         "<ctrl>+<shift>+<f10>")): on_exit,
        normalize_hotkey(config.get("window_toggle_hotkey","<ctrl>+<shift>+<f6>")): on_window_toggle,
        normalize_hotkey(config.get("auto_capture_hotkey",  "<ctrl>+<shift>+<f11>")): on_auto_toggle,
        normalize_hotkey(config.get("console_follow_hotkey","<ctrl>+<shift>+<f5>")): on_follow_toggle,
        normalize_hotkey(config.get("gspro_connect_hotkey","<ctrl>+<shift>+<f7>")): on_gspro_connect,
        normalize_hotkey(config.get("gspro_shot_hotkey",  "<ctrl>+<shift>+<f8>")): on_gspro_shot,
        normalize_hotkey(config.get("screenshot_hotkey",  "<ctrl>+<shift>+<f12>")): on_screenshot,
        normalize_hotkey(config.get("screenshot_toggle_hotkey", "<ctrl>+<shift>+<f4>")): on_screenshot_toggle,
    }
    return keyboard.GlobalHotKeys(mappings), stop_evt

# ── banner ─────────────────────────────────────────────────────────────────────

def print_banner(config: dict) -> None:
    state = load_state()
    r     = state["round"]
    bar   = "─" * 56
    sg_r  = r.get("sg_round") or _zero_cats()

    log.info("╭%s╮", bar)
    log.info("│  GSPro OCR + Strokes Gained Daemon%s│", " " * 21)
    log.info("│  %-53s│", VERSION_STR)
    log.info("├%s┤", bar)
    log.info("│  OCR hotkey    :  %-37s│", config["hotkey"])
    log.info("│  Exit hotkey   :  %-37s│", config.get("exit_hotkey", ""))
    log.info("├%s┤", bar)
    log.info("│  GSPro API v1 Connect%s│", " " * 34)
    log.info("│  Connect hotkey:  %-37s│", config.get("gspro_connect_hotkey", "<ctrl>+<shift>+<f7>"))
    log.info("│  Shot hotkey   :  %-37s│", config.get("gspro_shot_hotkey",    "<ctrl>+<shift>+<f8>"))
    log.info("│  Host/Port     :  %s:%s%s│",
             config.get("gspro_host", "127.0.0.1"),
             config.get("gspro_port", 921),
             " " * (36 - len(str(config.get("gspro_host","127.0.0.1"))) - len(str(config.get("gspro_port",921)))))
    shift        = config.get("window_x_shift", 1920)
    window_label = f"Window 1  (x −{shift} px, left)" if screen_capture.window_x_offset else "Window 2  (GSPro, right)"
    gspro_status = "connected ✔" if (_gspro and _gspro.is_connected()) else "not connected"
    auto_status  = ("ON — watching " + str(config.get("auto_trigger_cell", "V1"))) \
                   if (_watcher and _watcher.is_running()) else "OFF — manual hotkey"
    log.info("│  Auto-capture  :  %-37s│", auto_status)
    log.info("│  Auto hotkey   :  %-37s│",
             config.get("auto_capture_hotkey", "<ctrl>+<shift>+<f11>"))
    _fm = (config.get("console_follow", {}) or {}).get("mode", "opposite")
    log.info("│  Console follow:  %-37s│",
             f"{_fm}  ({config.get('console_follow_hotkey', '<ctrl>+<shift>+<f5>')})")
    log.info("│  Active window :  %-37s│", window_label)
    log.info("│  Toggle hotkey :  %-37s│", config.get("window_toggle_hotkey", "<ctrl>+<shift>+<f6>"))
    log.info("│  Screenshot    :  %-37s│", config.get("screenshot_hotkey", "<ctrl>+<shift>+<f12>"))
    log.info("│  Every capture :  %-37s│",
             f"{'ON' if _screenshots_on else 'off'}  ({config.get('screenshot_toggle_hotkey', '<ctrl>+<shift>+<f4>')})")
    log.info("│  Status        :  %-37s│", gspro_status)
    log.info("├%s┤", bar)
    log.info("│  ODS file      :  %-37s│", config["ods_file"])
    log.info("│  Raw-OCR row   :  %-37s│", state["row_offset"])
    log.info("│  Regions       :  %-37s│", len(config.get("regions", [])))
    log.info("├%s┤", bar)
    log.info("│  Round state ─────────────────────────────────────────│")
    hole_str = f"Hole {r['hole_number']}" if r["hole_number"] else "No hole yet"
    log.info("│    %-52s│", hole_str)
    log.info("│    SG Off Tee  %+.3f  Approach %+.3f%s│",
             sg_r.get(sg_mod.CAT_OTT, 0), sg_r.get(sg_mod.CAT_APP, 0), " " * 19)
    log.info("│    SG Arg Grn  %+.3f  Putting  %+.3f%s│",
             sg_r.get(sg_mod.CAT_ATG, 0), sg_r.get(sg_mod.CAT_PUTT, 0), " " * 19)
    log.info("│    Holes: %-3d   Score vs Par: %+d%s│",
             r["holes_completed"], r["score_vs_par"], " " * 22)
    log.info("╰%s╯", bar)

# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GSPro OCR daemon + Strokes Gained calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Workflow per F9 press:\n"
            "  First press on a new hole  → sets up tee state (no SG yet)\n"
            "  Every subsequent press     → calculates SG for the shot just played\n"
            "  Remaining ≈ 0 (in the cup) → hole finalised automatically\n"
            "  Inside gimmie circle      → scored on distance left; conceded\n"
            "                              putt counted as its own stroke\n\n"
            "Examples:\n"
            "  python ocr_daemon.py\n"
            "  python ocr_daemon.py --test\n"
            "  python ocr_daemon.py --new-round\n"
            "  python ocr_daemon.py --reset\n"
        ),
    )
    p.add_argument("--test",      action="store_true",
                   help="Dry-run: print OCR + SG, save debug PNGs, no file writes")
    p.add_argument("--once",      action="store_true",
                   help="Run pipeline once, write results, exit")
    p.add_argument("--upgrade-ods", nargs="?", const="", default=None, metavar="FILE",
                   help="Upgrade a report to the current format and exit. "
                        "Uses the configured report when FILE is omitted. A "
                        "permanent copy of the original is kept in backups/.")
    p.add_argument("--export-report", action="store_true",
                   help="Regenerate the ODS report from the database and exit.")
    p.add_argument("--dashboard", action="store_true",
                   help="Write the HTML dashboard from the database, open it in your "
                        "browser, and exit.")
    p.add_argument("--screenshots", action="store_true",
                   help="Start with screenshot-every-capture mode on: each capture's "
                        "frame, and each screen auto-capture skipped, is saved to "
                        "screenshots/<date>/ and named in the log.")
    p.add_argument("--check-regions", action="store_true",
                   help="Save what every capture region sees to region_check/, print "
                        "its OCR text, and exit. Use after a GSPro update.")
    p.add_argument("--reset",     action="store_true",
                   help="Reset raw-OCR row counter AND round state, then exit")
    p.add_argument("--new-round", action="store_true",
                   help="Reset round state only (keeps raw-OCR row counter), then exit")
    p.add_argument("--version", action="store_true",
                   help="Print version and changelog, then exit")
    p.add_argument("--gspro-test", action="store_true",
                   help="Connect to GSPro, send the debug shot, then exit (no OCR)")
    p.add_argument("--config",    default="config.json",
                   help="Config file path (default: config.json)")
    return p.parse_args()


def main() -> None:
    # Both must happen before any screen capture or subprocess is created,
    # otherwise GSPro can be knocked out of exclusive fullscreen on first use.
    screen_capture.enable_dpi_awareness()
    ocr_reader.suppress_subprocess_windows()
    log.info("═══  GSPro OCR Daemon %s  ═══", VERSION_STR)

    args = parse_args()

    if args.version:
        print(f"GSPro OCR + Strokes Gained Daemon  {VERSION_STR}\n")
        for v, note in CHANGELOG:
            print(f"  {v:<8} {note}")
        return

    global CONFIG_PATH
    cfg_path = Path(args.config)
    CONFIG_PATH = cfg_path if cfg_path.is_absolute() else SCRIPT_DIR / cfg_path

    config = load_config()
    screen_capture.RUN_CONFIG = config
    _open_database(config)

    if args.dashboard:
        path = write_dashboard(dict(config, dashboard=True))
        if path:
            print(f"Dashboard written: {path}")
            try:
                webbrowser.open(path.resolve().as_uri())
            except Exception:
                pass
        return
    _migrate_legacy_files(config)

    if args.screenshots or config.get("screenshot_every_capture", False):
        _set_screenshot_mode(config, True)

    if args.reset:
        reset_state()
        return
    if args.new_round:
        reset_round_only()
        return
    if args.export_report:
        export_report(config)
        return

    for h in logging.getLogger().handlers:
        if isinstance(h, logrotate.ArchivingFileHandler):
            h.maxBytes = int(float(config.get("log_max_mb", 5)) * 1024 * 1024)
            h.keep_archives = int(config.get("log_keep_archives", 0))

    # Scoring settings (gimmie radius, validation bounds) — the same call the
    # rebuild tool makes, so a replay is scored under identical rules.
    scoring.configure(config)

    if args.upgrade_ods is not None:
        target = Path(args.upgrade_ods) if args.upgrade_ods else _ods_path(config)
        rep = _upgrade_report(target)
        print(f"{target}: {rep['status']}")
        for n in rep.get("notes", []):
            print(f"  - {n}")
        if rep.get("backup"):
            print(f"  original kept at {rep['backup']}")
        return
    ocr_reader.OCR_OVERRIDES = config.get("ocr_profiles", {}) or {}
    if config.get("digit_templates", True):
        ocr_reader.DIGITS = digit_reader.TemplateStore(SCRIPT_DIR / "digit_templates.seed.json",
                                             SCRIPT_DIR / "digit_templates.json")
        if _ARROW_OK:
            _arrow.DIGIT_READER = ocr_reader.DIGITS
            _arrow.WIND_ARROW_POINTS = config.get("wind_arrow_points", "to")   # T-06
        for font in ("panel", "hud", "score"):
            gap = ocr_reader.DIGITS.missing_digits(font)
            log.info("Character matching — %s font: %s", font,
                     "ready" if not gap else
                     f"learning; still needs {' '.join(gap)} (Tesseract until then)")
    if _VOCAB_OK:
        parsing.COURSE_BOOK = vocab.CourseBook(SCRIPT_DIR / "known_courses.json")
        log.info("OCR: per-field profiles on; %d known course(s); %d worker(s)",
                 len(parsing.COURSE_BOOK.established()), int(config.get("ocr_workers", 6)))
    else:
        log.warning("ocr_vocab.py not found — per-field OCR profiles and "
                    "club/lie/course snapping are disabled")

    log.info("Gimmie zone: %.1f ft — shots inside this are scored on the "
             "distance remaining, not as hole-outs",
             scoring.GIMMIE_ZONE_YD * sg_mod.FT_PER_YD)

    if config.get("tesseract_cmd"):
        pytesseract.pytesseract.tesseract_cmd = config["tesseract_cmd"]

    if not _GSPRO_AVAILABLE:
        log.warning("gspro_api.py not found — GSPro hotkeys will be inert. "
                    "Place gspro_api.py in the same folder as ocr_daemon.py.")

    if sys.platform == "win32":
        log.info("Display mode: if GSPro drops out of fullscreen, set it to "
                 "Borderless Windowed — see README. Capture works in both, but "
                 "borderless cannot mode-switch.")

    if not _ARROW_OK:
        log.warning("arrow_detector.py not found or opencv-python not installed. "
                    "Wind direction and height-delta regions will use Tesseract as fallback. "
                    "Install with:  pip install opencv-python")

    # Restore window offset from last session
    screen_capture.window_x_offset = load_state().get("window_x_offset", 0)
    if screen_capture.window_x_offset:
        log.info("Window offset restored: %d px (Window 1, left screen)", screen_capture.window_x_offset)

    # ── --gspro-test mode ─────────────────────────────────────────────────────
    if args.check_regions:
        _check_regions(config, save_crops=True)
        return

    # Crash-safety checks before anything can write: damaged-file warnings,
    # start-of-session snapshots, report upgrade, and row-position repair.
    if not (args.test or args.gspro_test):
        _startup_storage_checks(config)
        _check_regions(config)

    if args.gspro_test:
        if not _GSPRO_AVAILABLE:
            log.error("Cannot run --gspro-test: gspro_api.py is missing")
            return
        log.info("--gspro-test: connecting to GSPro and sending debug shot")
        _gspro_connect(config)
        import time as _t; _t.sleep(0.5)   # brief pause for handshake
        _gspro_debug_shot()
        _t.sleep(1.0)                       # wait for response to arrive
        if _gspro:
            _gspro.disconnect()
        return

    if args.test:
        log.info("TEST MODE — dry-run, debug PNGs saved, no ODS writes")
        run_pipeline(config, dry_run=True)
        return

    if args.once:
        log.info("--once mode")
        run_pipeline(config, dry_run=False)
        return

    # Start console following (no-op without a console, e.g. under pythonw)
    global _follower
    if _FOLLOW_OK:
        _follower = ConsoleFollower(config)
        if _follower.available():
            _follower.start()

    # Start auto-capture immediately if enabled in config
    if config.get("auto_capture") and _WATCHER_OK:
        _do_auto_toggle(config)

    print_banner(config)
    listener, stop_evt = build_listener(config)
    listener.start()
    try:
        stop_evt.wait()
    except KeyboardInterrupt:
        log.info("Ctrl-C — shutting down…")
    finally:
        _finish_run(config, listener)


def _finish_run(config: dict, listener) -> None:
    """
    The exit sequence (v3.8.11): first make sure no capture is running or can
    start — no hotkeys, auto-capture stopped after finishing a capture already
    under way, a manual capture finished — and only then write the exit
    reports. Every background thread that logs is ended before Python shuts down.
    """
    _SHUTTING_DOWN.set()
    try:
        listener.stop()                      # no more hotkeys
    except Exception as exc:
        log.debug("Hotkey listener stop failed: %s", exc)
    if _follower:
        _follower.stop(wait=True)
    held = _quiesce_captures()
    try:
        screen_capture.close_sct()
        if ocr_reader.DIGITS is not None:
            log.info("Character matching: %s", ocr_reader.DIGITS.summary())
        if _FIELDS is not None:
            log.info("Field cache: %s", _FIELDS.summary())
        if _saver is not None:
            log.info("Screenshots: %d saved, %d dropped, %d failed — writing any "
                     "still queued…", _saver.saved, _saver.dropped, _saver.failed)
            _saver.close()
        if _gspro and _gspro.is_connected():
            _gspro.disconnect()
        # Write round statistics before closing
        try:
            _write_exit_reports(config)
        except Exception as exc:
            log.error("Round Stats not written: %s", exc)
    finally:
        if held:
            _pipeline_lock.release()
    log.info("Daemon stopped.  Log: %s", LOG_FILE)


if __name__ == "__main__":
    main()
