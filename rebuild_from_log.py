#!/usr/bin/env python3
"""
rebuild_from_log.py — Rebuild the ODS report from an ocr_daemon.log.

The daemon logs every capture's OCR text, field by field, BEFORE deciding
anything about it. The log is therefore a complete record — including captures
that were rejected — and can be replayed when a rule turns out to be wrong.

Since v2.9.0 this tool replays through the SAME engine as the live daemon
(scoring.handle_capture). Validation, progression checks, session boundaries,
gimmie handling, the incomplete-hole discard and the summary sheets are all
shared code, so a rebuilt report is what the current daemon would have
produced live — not an approximation maintained separately. The output uses
the daemon's own layout and can replace a live report directly.

Each capture is replayed at its logged time, so session gaps and row
timestamps are reproduced. Logs written before v2.9.0 carry only HH:MM:SS; the
date is taken from the nearest date in the log (or --date), with midnight
rollovers detected.

Usage
-----
    python rebuild_from_log.py ocr_daemon.log -o rebuilt.ods
    python rebuild_from_log.py ocr_daemon.log --dry-run        # report only
    python rebuild_from_log.py old.log --date 2026-08-20       # undated old log
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db                 # noqa: E402
import logrotate          # noqa: E402
import ods_upgrade        # noqa: E402
import parsing            # noqa: E402
import reports            # noqa: E402
import scoring            # noqa: E402
import storage            # noqa: E402
from version import VERSION_STR   # noqa: E402

try:
    import ocr_vocab as vocab     # noqa: E402
except ImportError:
    vocab = None

log = logging.getLogger("rebuild")

# ── log parsing ────────────────────────────────────────────────────────────────

_LINE_NEW = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d)\s+\w+\s+(.*)$")
_LINE_OLD = re.compile(r"^(\d\d:\d\d:\d\d)\s+\w+\s+(.*)$")
_FIELD    = re.compile(r"^\s*\[(.+?)\s+([A-Z]{1,3}\d+)\]\s+(.*)$")
_STR_LIT  = re.compile(r"""^('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
_CAPTURED = re.compile(r"captured at (\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d)")
_ANY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class Capture:
    ts: datetime
    by_cell: dict[str, str] = field(default_factory=dict)
    by_name: dict[str, str] = field(default_factory=dict)
    live: str = "unknown"          # what the daemon decided at the time


def _decode(rest: str) -> str:
    """
    Recover the exact OCR text from a logged field. The daemon logs repr(text),
    so literal_eval returns precisely what was read — including quotes in
    feet-and-inches values like 12' 4", which a plain regex mangles.
    """
    m = _STR_LIT.match(rest.strip())
    if not m:
        return ""
    try:
        return ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return ""


def parse_log(path: Path, default_date: str | None = None) -> list[Capture]:
    """Return one Capture per OCR pipeline run, in log order."""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()

    first = next((m.group(1) for ln in text for m in [_ANY_DATE.search(ln)] if m), None)
    carried = default_date or first or datetime.fromtimestamp(
        path.stat().st_mtime).strftime("%Y-%m-%d")
    day = datetime.strptime(carried, "%Y-%m-%d")
    prev_t: datetime | None = None

    caps: list[Capture] = []
    cur: Capture | None = None
    for raw in text:
        line = raw.rstrip("\r")
        m = _LINE_NEW.match(line)
        if m:
            line_ts = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
            day = line_ts.replace(hour=0, minute=0, second=0)
            msg = m.group(3)
        else:
            m = _LINE_OLD.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), "%H:%M:%S")
            line_ts = day.replace(hour=t.hour, minute=t.minute, second=t.second)
            if prev_t is not None and (prev_t - line_ts) > timedelta(hours=12):
                day += timedelta(days=1)               # midnight rollover
                line_ts += timedelta(days=1)
            msg = m.group(2)
        prev_t = line_ts

        d = _ANY_DATE.search(msg)
        if d and not _LINE_NEW.match(line):
            day = datetime.strptime(d.group(1), "%Y-%m-%d")

        if "OCR pipeline" in msg:
            if cur:
                caps.append(cur)
            cur = Capture(ts=line_ts)
            continue
        if cur is None:
            continue

        f = _FIELD.match(msg)
        if f:
            name, cell, rest = f.group(1).strip(), f.group(2).upper(), f.group(3)
            val = _decode(rest)
            cur.by_cell[cell] = val
            cur.by_name.setdefault(name.lower(), val)
            continue
        c = _CAPTURED.search(msg)
        if c:
            cur.ts = datetime.strptime(f"{c.group(1)} {c.group(2)}", "%Y-%m-%d %H:%M:%S")
            cur.live = "accepted"
        elif "Capture rejected" in msg:
            cur.live = "invalid"
        elif "Skipped:" in msg or "Capture refused" in msg:
            cur.live = "no_progression"
    if cur:
        caps.append(cur)
    return caps


# ── replay ─────────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"row_offset": 0, "round": scoring._fix_round({}),
            "window_x_offset": 0, "last_capture": None}


def replay(captures: list[Capture], config: dict,
           courses_seed: Path | None = None) -> tuple[dict, list, dict]:
    """
    Replay captures through the shared engine.
    Returns (sheets, history, stats).
    """
    scoring.configure(config)
    sink  = scoring.MemorySink()
    state = _default_state()
    stats = {"captures": len(captures), "accepted": 0, "invalid": 0,
             "no_progression": 0, "end_of_round": 0, "recovered": 0, "newly_skipped": 0,
             "prechecked": 0}

    with tempfile.TemporaryDirectory() as td:
        # A fresh course list, so the result depends only on this log.
        # --courses seeds it from a real known_courses.json without modifying it.
        if vocab is not None:
            book_path = Path(td) / "known_courses.json"
            if courses_seed and courses_seed.exists():
                shutil.copy(courses_seed, book_path)
            parsing.COURSE_BOOK = vocab.CourseBook(book_path)
        try:
            for cap in captures:
                scoring.set_clock(lambda ts=cap.ts: ts)
                # A saved screenshot (rebuild_from_screenshots.py) is read now,
                # against the engine's current state — the pre-check may skip it
                # exactly as the live daemon's would (v3.8.7).
                prepare = getattr(cap, "prepare", None)
                if prepare is not None and not prepare(state):
                    stats["prechecked"] += 1
                    continue
                state, outcome, _ = scoring.handle_capture(
                    state, cap.by_cell, cap.by_name, sink, config)
                stats[outcome] += 1
                if outcome == "accepted" and cap.live in ("invalid", "no_progression", "skipped"):
                    stats["recovered"] += 1
                if outcome != "accepted" and cap.live == "accepted":
                    stats["newly_skipped"] += 1

            # End of log = end of run: the same exit sequence as the daemon.
            if captures:
                scoring.set_clock(lambda ts=captures[-1].ts: ts)
            r = state["round"]
            scoring.close_open_hole(
                r, sink, discard_incomplete=config.get("discard_incomplete_hole_on_exit", True))
            holes  = r.get("holes_completed", 0)
            if holes > 0:
                sink.archive_session(r)    # archives the final session — must stay

            sheets: dict = {}
            for name in ("Sheet1", "SG_Shots", "SG_Holes"):
                if name in sink.sheets:
                    sheets[name] = sink.sheets[name]
            sheets.update(reports.build_summary_sheets(
                r, sink.history, sheets.get("SG_Shots", []), VERSION_STR))
        finally:
            scoring.set_clock(None)
            parsing.COURSE_BOOK = None

    ods_upgrade.write_meta(sheets, ods_upgrade.FORMAT_VERSION,
                           f"rebuild_from_log {VERSION_STR}",
                           rebuilt_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    stats["shots"]    = max(0, len(sheets.get("SG_Shots", [])) - 1)
    stats["holes"]    = max(0, len(sheets.get("SG_Holes", [])) - 1)
    stats["sessions"] = len(sink.history)
    stats["final_state"] = state
    return sheets, sink.history, stats


# ── CLI ────────────────────────────────────────────────────────────────────────

def _load_config(path: Path | None) -> dict:
    """config.json, with settings added in later versions taken from the
    defaults — the same as the daemon does (v3.8.7)."""
    from config_defaults import DEFAULT_CONFIG
    p = path or (HERE / "config.json")
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        user = {}
    return {**DEFAULT_CONFIG, **user}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", type=Path, nargs="+",
                    help="log file(s) to replay, oldest first")
    ap.add_argument("--with-archives", action="store_true",
                    help="also replay the archived logs in logs/ beside the first log, before it")
    ap.add_argument("-o", "--output", type=Path, default=Path("rebuilt.ods"))
    ap.add_argument("--config", type=Path, default=None,
                    help="config.json to score under (default: the one beside this script)")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="date for an old log whose lines carry only HH:MM:SS")
    ap.add_argument("--courses", type=Path, default=None,
                    help="seed course snapping from a known_courses.json (read-only)")
    ap.add_argument("--db-out", type=Path, default=None,
                    help="also write a database (v3.2+ master record) you can adopt "
                         "in place of gspro_stats.db")
    ap.add_argument("--dashboard", type=Path, default=None, metavar="HTML",
                    help="also write the HTML dashboard (dashboard.py) here")
    ap.add_argument("--history-out", type=Path, default=None,
                    help="also write the rebuilt session history as JSON")
    ap.add_argument("--session-gap", type=float, default=None,
                    help="override session_gap_minutes from the config")
    ap.add_argument("--min-yardage", type=float, default=None)
    ap.add_argument("--max-yardage", type=float, default=None)
    ap.add_argument("--max-hole", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the engine's log")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)-8s %(message)s")

    config = _load_config(a.config)
    val = dict(config.get("validation", {}) or {})
    if a.min_yardage is not None: val["min_hole_yardage"] = a.min_yardage
    if a.max_yardage is not None: val["max_hole_yardage"] = a.max_yardage
    if a.max_hole is not None:    val["max_hole_number"]  = a.max_hole
    config["validation"] = val
    if a.session_gap is not None:
        config["session_gap_minutes"] = a.session_gap

    paths = list(a.logs)
    if a.with_archives:
        paths = logrotate.archived_logs(paths[0]) + paths
    caps: list[Capture] = []
    for p in paths:
        caps += parse_log(p, a.date)
    sheets, history, st = replay(caps, config, a.courses)

    print(f"Rebuilt with the shared engine ({VERSION_STR})")
    print(f"  Log files replayed       : {len(paths)}")
    print(f"  Captures in log          : {st['captures']}")
    print(f"    accepted               : {st['accepted']}")
    print(f"    invalid (blank/menu)   : {st['invalid']}")
    print(f"    no progression (replay): {st['no_progression']}")
    print(f"    end-of-round screens   : {st['end_of_round']}")
    print(f"  Recovered vs live run    : {st['recovered']}  (rejected then, accepted now)")
    print(f"  Newly skipped vs live    : {st['newly_skipped']}  (accepted then, skipped now)")
    print(f"  Shots scored             : {st['shots']}")
    print(f"  Holes completed          : {st['holes']}")
    print(f"  Sessions                 : {st['sessions']}")

    if a.dry_run:
        print("\n--dry-run: nothing written")
        return
    write_outputs(a, sheets, history, st, f"rebuild_from_log {VERSION_STR}")


def write_outputs(a, sheets: dict, history: list, st: dict, label: str) -> None:
    """Write the report, and the database, dashboard and history if asked (shared
    with rebuild_from_screenshots.py)."""
    storage.ods_save(a.output, sheets, backups=1)
    print(f"\nWritten: {a.output}")
    if a.db_out:
        if a.db_out.exists():
            sys.exit(f"{a.db_out} already exists — choose a new name; nothing written")
        out = db.Database(a.db_out)
        out.import_snapshot({n: sheets[n] for n in db.REPORT_SHEETS if n in sheets},
                            history, st["final_state"])
        out.set_meta("migrated", label)
        out.set_meta("report_format", str(ods_upgrade.FORMAT_VERSION))
        out.close()
        print(f"Database: {a.db_out}")
    if a.dashboard:
        import dashboard
        page = dashboard.build_dashboard(None, history, sheets.get("SG_Shots", []),
                                         label)
        storage.atomic_write_bytes(a.dashboard, page.encode("utf-8"), backups=0)
        print(f"Dashboard: {a.dashboard}")
    if a.history_out:
        storage.save_json(a.history_out, history, backups=1)
        print(f"History: {a.history_out}")


if __name__ == "__main__":
    main()
