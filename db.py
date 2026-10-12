#!/usr/bin/env python3
"""
db.py — SQLite master record.

Since v3.2.0 the database, not the spreadsheet, is the record of everything
the daemon has captured. The ODS report is regenerated from it.

Why
---
* ONE TRANSACTION PER CAPTURE. A capture's raw row, shot rows, hole rows and
  the daemon state commit together or not at all. Before, the report was
  written row by row and the state file saved separately, so a crash between
  them left the two out of step until the next launch repaired it.
* NO MORE WHOLE-SPREADSHEET REWRITES. Each accepted shot used to read and
  rewrite the entire ODS about three times. Now it is a few small inserts; the
  report is regenerated when a hole finishes, at exit, and on demand.
* THE REPORT IS DISPOSABLE. If it is open in LibreOffice the refresh fails
  harmlessly and happens next time — nothing is lost, because the data lives
  here.

Layout
------
  rows      every report row, exactly as it appears in the report:
            (sheet, idx) → JSON list of cell texts. Sheet1, SG_Shots, SG_Holes.
  sessions  one row per archived session, with typed columns for querying
            (date, week, course, holes, shots, par, vs_par, sg_total,
            coverage) plus the full entry as JSON.
  state     the daemon state (current round, row positions, last capture).
  meta      schema version and migration markers.

SQLite's json functions work on the stored rows, e.g.
  SELECT json_extract(data, '$[12]') FROM rows WHERE sheet = 'SG_Shots';
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import storage

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REPORT_SHEETS = ("Sheet1", "SG_Shots", "SG_Holes")      # data sheets kept as rows

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rows     (sheet TEXT NOT NULL, idx INTEGER NOT NULL,
                                     data TEXT NOT NULL, PRIMARY KEY (sheet, idx));
CREATE TABLE IF NOT EXISTS sessions (round_id TEXT PRIMARY KEY, date TEXT, week TEXT,
                                     course TEXT, holes INTEGER, shots INTEGER,
                                     par INTEGER, vs_par INTEGER, sg_total REAL,
                                     coverage REAL, entry TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state    (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Future schema changes: {from_version: function(conn)}. Applied in order.
_SCHEMA_MIGRATIONS: dict[int, Any] = {}


class Database:
    """A thread-safe handle on the SQLite file. All access goes through it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.created = not self.path.exists()
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    isolation_level=None)      # explicit transactions
        try:
            self._init()
        except BaseException:
            # Close before re-raising: on Windows an open handle would stop
            # open_database() from moving a damaged file aside.
            self.conn.close()
            raise

    def _init(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            # Statements run one at a time: executescript() can issue an
            # implicit COMMIT, which would break the surrounding transaction.
            for stmt in (s.strip() for s in _SCHEMA.split(";")):
                if stmt:
                    self.conn.execute(stmt)
            v = self.get_meta("schema_version")
            if v is None:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
                self.set_meta("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            else:
                for frm in range(int(v), SCHEMA_VERSION):
                    _SCHEMA_MIGRATIONS[frm](self.conn)
                self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ── transactions ──────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """All-or-nothing. Nestable: only the outermost level commits."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator["Database"]:
        """
        A consistent read on its own connection, for the background report
        (v3.8.3). In WAL mode a reader sees one committed snapshot without the
        writer's lock, so captures keep committing while the report reads —
        through transaction() a season's report held them up for ~2 s.
        """
        if str(self.path) == ":memory:":                  # no shared file to snapshot
            with self.transaction():
                yield self
            return
        reader = object.__new__(Database)
        reader.path, reader.created = self.path, False
        reader._lock, reader._depth = threading.RLock(), 1   # already "in" a transaction
        reader.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                      isolation_level=None, timeout=30)
        try:
            reader.conn.execute("BEGIN")                  # snapshot taken at the first read
            yield reader
        finally:
            try:
                reader.conn.execute("ROLLBACK")
            finally:
                reader.conn.close()

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def check(self) -> None:
        """Raise sqlite3.DatabaseError if the file fails an integrity check."""
        with self._lock:
            res = self.conn.execute("PRAGMA quick_check").fetchone()[0]
        if res != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed: {res}")

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # ── meta / state ──────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def get_state(self) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM state WHERE key='daemon'").fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, state: dict) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO state VALUES ('daemon', ?)",
                              (json.dumps(state),))

    # ── report rows ───────────────────────────────────────────────────────────

    def write_row(self, sheet: str, idx: int, values: list) -> None:
        cells = [str(v) if v is not None else "" for v in values]
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO rows VALUES (?, ?, ?)",
                              (sheet, int(idx), json.dumps(cells)))

    def truncate(self, sheet: str, keep_rows: int) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM rows WHERE sheet=? AND idx>=?",
                                    (sheet, int(keep_rows)))
        return cur.rowcount

    def sheet_len(self, sheet: str) -> int:
        with self._lock:
            m = self.conn.execute("SELECT MAX(idx) FROM rows WHERE sheet=?", (sheet,)).fetchone()[0]
        return 0 if m is None else m + 1

    def sheet_rows(self, sheet: str) -> list[list[str]]:
        """The sheet as the report shows it; positions never written are empty."""
        with self._lock:
            got = self.conn.execute("SELECT idx, data FROM rows WHERE sheet=? ORDER BY idx",
                                    (sheet,)).fetchall()
        out: list[list[str]] = []
        for idx, data in got:
            while len(out) < idx:
                out.append([])
            out.append(json.loads(data))
        return out

    # ── sessions ──────────────────────────────────────────────────────────────

    def load_sessions(self) -> list[dict]:
        with self._lock:
            got = self.conn.execute("SELECT entry FROM sessions ORDER BY round_id").fetchall()
        return [json.loads(e) for (e,) in got]

    def save_sessions(self, sessions: list[dict]) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM sessions")
            for s in sessions:
                q = s.get("quality") or {}
                cap, con, est = (q.get("strokes_captured", 0), q.get("strokes_conceded", 0),
                                 q.get("strokes_uncaptured", 0))
                tot = cap + con + est
                self.conn.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (s.get("round_id"), s.get("date"), s.get("week"), s.get("course"),
                     s.get("holes"), s.get("shots"), s.get("par"), s.get("vs_par"),
                     s.get("sg_total"), round((cap + con) / tot, 4) if tot else None,
                     json.dumps(s)))

    # ── bulk import (migration, rebuild) ──────────────────────────────────────

    def import_snapshot(self, sheets: dict[str, list[list[str]]],
                        sessions: list[dict], state: dict | None) -> None:
        with self.transaction():
            for name in REPORT_SHEETS:
                for idx, row in enumerate(sheets.get(name) or []):
                    if row:
                        self.write_row(name, idx, row)
            self.save_sessions(sessions or [])
            if state is not None:
                self.set_state(state)

    # ── backups ───────────────────────────────────────────────────────────────

    def snapshot(self, label: str, keep: int = 5) -> Path:
        """A consistent copy via SQLite's backup API (safe while in use)."""
        d = storage.backup_dir(self.path)
        dest = d / f"{self.path.stem}.{label}-{storage._timestamp()}{self.path.suffix}"
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        if keep > 0:
            for old in sorted(d.glob(f"{self.path.stem}.{label}-*{self.path.suffix}"))[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return dest


def _snapshots(path: Path) -> list[Path]:
    d = Path(path).parent / storage.BACKUP_DIR_NAME
    return sorted(d.glob(f"{path.stem}.*{path.suffix}"), key=lambda p: p.stat().st_mtime) \
        if d.exists() else []


def open_database(path: str | Path) -> Database:
    """
    Open the database, recovering from damage. A file that fails to open or
    fails its integrity check is moved into backups/ (never deleted) and the
    newest snapshot that passes is restored. Only if none does is a fresh
    database created — the damaged file stays preserved either way.
    """
    path = Path(path)
    try:
        d = Database(path)
        d.check()
        return d
    except sqlite3.DatabaseError as exc:
        reason = exc
    try:
        d.close()                                     # type: ignore[possibly-undefined]
    except Exception:
        pass
    kept = storage._quarantine(path)
    for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if side.exists():
            storage._quarantine(side)
    log.error("Database %s could not be opened (%s). Kept as %s.", path.name, reason, kept)
    for snap in reversed(_snapshots(path)):
        if ".corrupt-" in snap.name:
            continue
        try:
            shutil.copy2(snap, path)
            d = Database(path)
            d.check()
            log.warning("Database restored from %s.", snap.name)
            return d
        except sqlite3.DatabaseError:
            try:
                d.close()
            except Exception:
                pass
            path.unlink(missing_ok=True)
    log.critical("No usable database snapshot — starting a fresh database. The "
                 "damaged file is preserved at %s; rebuild_from_log.py can "
                 "recover your data from the log.", kept)
    return Database(path)


# ── the engine's sink, backed by the database ─────────────────────────────────

def archive_round(database: Database, r: dict) -> list[dict]:
    """Archive a finished session; return all sessions."""
    import scoring
    entry = scoring.build_session_entry(r)
    if entry is None:
        return database.load_sessions()
    sessions = scoring.merge_session(database.load_sessions(), entry)
    database.save_sessions(sessions)
    return sessions


class DatabaseSink:
    """scoring.py's sink, writing to the database. See scoring.Sink."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self.archived = False

    def write_row(self, sheet: str, row: int, values: list) -> bool:
        self.db.write_row(sheet, row, values)
        return True

    def truncate(self, sheet: str, keep_rows: int) -> int:
        return self.db.truncate(sheet, keep_rows)

    def summary_changed(self, round_state: dict) -> None:
        pass                       # the report is regenerated after the commit

    def archive_session(self, round_state: dict) -> list:
        self.archived = True
        return archive_round(self.db, round_state)


# ── the report, generated from the database ───────────────────────────────────

def report_sheets(database: Database, round_state: dict, version: str) -> dict:
    """Every sheet of the ODS report, built from the database."""
    import ods_upgrade
    import reports
    with database.reading() as rd:                   # one consistent read, no writer lock
        sheets = {n: rd.sheet_rows(n) for n in REPORT_SHEETS if rd.sheet_len(n)}
        sessions = rd.load_sessions()
    sheets.update(reports.build_summary_sheets(round_state, sessions,
                                               sheets.get("SG_Shots", []), version))
    ods_upgrade.write_meta(sheets, ods_upgrade.FORMAT_VERSION, version,
                           generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           source=f"database {database.path.name}")
    return sheets


def dashboard_html(database: Database, round_state: dict, version: str) -> str:
    """The HTML dashboard (dashboard.py), from one consistent read of the database."""
    import dashboard
    with database.reading() as rd:
        shot_rows = rd.sheet_rows("SG_Shots") if rd.sheet_len("SG_Shots") else []
        sessions = rd.load_sessions()
    return dashboard.build_dashboard(round_state, sessions, shot_rows, version)


# ── keeping stored rows in the current report layout ──────────────────────────

def upgrade_report_rows(database: Database, upgrade_ctx: dict) -> list[str]:
    """
    Bring the stored report rows up to the current report format, using the
    same migration steps as an ODS upgrade. Must run before any capture writes
    rows in the new layout, or old and new rows would sit under different
    columns. Databases created by v3.2 carry no marker and hold format 3.
    """
    import ods_upgrade
    frm = int(database.get_meta("report_format") or 3)
    if frm >= ods_upgrade.FORMAT_VERSION:
        return []
    data = {n: database.sheet_rows(n) for n in REPORT_SHEETS}
    notes: list[str] = []
    for v in range(frm, ods_upgrade.FORMAT_VERSION):
        notes += ods_upgrade.MIGRATIONS[v](data, upgrade_ctx)
    with database.transaction():
        for n in REPORT_SHEETS:
            database.truncate(n, 0)
            for idx, row in enumerate(data.get(n) or []):
                if row:
                    database.write_row(n, idx, row)
        database.set_meta("report_format", str(ods_upgrade.FORMAT_VERSION))
    return notes


# ── one-time migration from the pre-database files ────────────────────────────

def migrate_legacy(database: Database, state_file: Path, history_file: Path,
                   ods_file: Path, upgrade_ctx: dict) -> dict:
    """
    Import the state file, round history and report rows into a database that
    has not been migrated yet. Returns a summary dict.

    All of it goes in one transaction: a failure leaves the database untouched
    and the next launch tries again. Afterwards the two JSON files are renamed
    with a ".migrated" suffix (contents intact, and also copied into backups/),
    so they cannot be imported twice. The report itself is left in place; it is
    regenerated from the database from now on.
    """
    if database.get_meta("migrated"):
        return {"status": "already"}
    found: dict[str, Any] = {}
    for key, loader in (
        ("state",   lambda: storage.load_json(state_file, expect=dict, backups=3,
                                              what="State file")),
        ("history", lambda: storage.load_json(history_file, expect=list, backups=5,
                                              what="Round history")),
        ("report",  lambda: storage.ods_read(ods_file, backups=2)),
    ):
        try:
            found[key] = loader()
        except storage.StorageError as exc:
            log.warning("Migration: %s could not be read (%s) — skipped", key, exc)
            found[key] = None

    if not any(found.values()):
        import ods_upgrade
        database.set_meta("migrated", "nothing to migrate")
        database.set_meta("report_format", str(ods_upgrade.FORMAT_VERSION))
        return {"status": "nothing"}

    report = found["report"] or {}
    if report:
        import ods_upgrade
        ods_upgrade.upgrade_data(report, upgrade_ctx)  # older layouts → current columns
    sheets = {n: report[n] for n in REPORT_SHEETS if report.get(n)}
    with database.transaction():
        database.import_snapshot(sheets, found["history"] or [], found["state"])
        database.set_meta("migrated", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        import ods_upgrade
        database.set_meta("report_format", str(ods_upgrade.FORMAT_VERSION))

    for f in (state_file, history_file):
        if Path(f).exists():
            storage.snapshot(Path(f), "pre-database", keep=0)
            Path(f).replace(Path(f).with_name(Path(f).name + ".migrated"))
    return {"status": "migrated",
            "rows": {n: len(v) for n, v in sheets.items()},
            "sessions": len(found["history"] or []),
            "state": found["state"] is not None}
