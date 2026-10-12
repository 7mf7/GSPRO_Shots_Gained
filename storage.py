#!/usr/bin/env python3
"""
storage.py — Crash-safe persistence for the daemon's files.

Why this module exists
----------------------
Every file the daemon keeps — ocr_state.json, round_history.json and the ODS
report — used to be written by truncating the file and then rewriting it. If
the process died between those two steps the file was left half-written, and
each loader treated an unreadable file exactly like a missing one and started
fresh. The next write then built on that empty start:

    round_history.json  → the next session was saved alone; all earlier
                          sessions were erased
    ocr_state.json      → row counters reset to zero, so the next capture
                          overwrote Sheet1 and SG_Shots from the top
    ocr_output.ods      → the next write kept only the sheet it touched;
                          every other sheet was erased

Three guarantees replace that behaviour:

1. ATOMIC WRITES. Data is written to a temporary file in the same folder,
   flushed to disk, and swapped into place with os.replace(). At every instant
   the file is either the complete old version or the complete new one.

2. ROTATING BACKUPS. Before each replace, the current good file is copied to
   backups/<name>.bak1, with older copies shuffled down to .bak2, .bak3 …

3. NEVER DESTROY, NEVER SILENTLY START OVER. A file that exists but cannot be
   read is moved aside to backups/<name>.corrupt-<timestamp> — never deleted —
   and the newest backup that does read is restored. Only if no backup reads
   does the caller get a DataCorruptError, and it must not overwrite anything.

A file that is simply MISSING is treated as new and is not restored from
backup: our own writes never leave the target missing, so a missing file means
you deleted it on purpose — for example to start a fresh spreadsheet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"


# ── Errors ─────────────────────────────────────────────────────────────────────

class StorageError(Exception):
    """Base class for storage failures."""


class FileLockedError(StorageError):
    """Another program holds the file (usually the ODS open in LibreOffice)."""


class DataCorruptError(StorageError):
    """The file is unreadable and no backup could be restored."""


# ── Paths ──────────────────────────────────────────────────────────────────────

def backup_dir(path: Path) -> Path:
    d = Path(path).parent / BACKUP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_path(path: Path, n: int) -> Path:
    path = Path(path)
    return backup_dir(path) / f"{path.name}.bak{n}"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def _replace(src: Path, dst: Path, attempts: int = 6, delay: float = 0.05) -> None:
    """
    os.replace with a short retry.

    On Windows, antivirus scanners and the search indexer briefly open new files,
    which makes a replace fail with PermissionError for a few milliseconds. A
    lock that persists past the retries is reported as FileLockedError.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if i == attempts - 1:
                raise FileLockedError(
                    f"{Path(dst).name} is locked by another program "
                    f"(is it open in LibreOffice?)") from exc
            time.sleep(delay * (i + 1))


def _rotate_backups(path: Path, keep: int) -> None:
    """Copy the current file to .bak1, shuffling older backups down."""
    path = Path(path)
    if keep <= 0 or not path.exists():
        return
    oldest = backup_path(path, keep)
    if oldest.exists():
        oldest.unlink()
    for n in range(keep - 1, 0, -1):
        src = backup_path(path, n)
        if src.exists():
            os.replace(src, backup_path(path, n + 1))
    shutil.copy2(path, backup_path(path, 1))


def _quarantine(path: Path) -> Path:
    """Move an unreadable file aside. It is preserved, never deleted."""
    dest = backup_dir(path) / f"{Path(path).name}.corrupt-{_timestamp()}"
    try:
        os.replace(path, dest)
    except OSError:
        shutil.copy2(path, dest)
    return dest


# ── Writing ────────────────────────────────────────────────────────────────────

def atomic_write_with(path: Path, writer: Callable[[Path], None],
                      backups: int = 3) -> None:
    """
    Write a file atomically using *writer*, which receives a temporary path and
    must write the complete file there. The temp file lives in the same folder
    so os.replace() is an atomic rename on the same volume.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        writer(tmp)
        _fsync_file(tmp)
        _rotate_backups(path, backups)
        _replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes, backups: int = 3) -> None:
    atomic_write_with(path, lambda p: p.write_bytes(data), backups)


# ── Reading with recovery ──────────────────────────────────────────────────────

def read_with_recovery(path: Path, parse: Callable[[Path], Any],
                       backups: int = 3, what: str = "file") -> Any:
    """
    Read and parse *path*, recovering from corruption.

    Returns
        the parsed value, or None if the file does not exist (a new file).
    Raises
        FileLockedError   the file exists but another program holds it
        DataCorruptError  the file is unreadable and no backup could be restored;
                          the unreadable file has been preserved in backups/
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return parse(path)
    except PermissionError as exc:
        raise FileLockedError(f"{path.name} is locked by another program") from exc
    except Exception as exc:
        reason = exc

    kept = _quarantine(path)
    log.error("%s %s could not be read (%s). The damaged file has been kept as "
              "%s and will not be deleted.", what, path.name, reason, kept)

    for n in range(1, backups + 1):
        b = backup_path(path, n)
        if not b.exists():
            continue
        try:
            value = parse(b)
        except Exception:
            continue
        shutil.copy2(b, path)
        log.warning("%s %s restored from %s.", what, path.name, b.name)
        return value

    raise DataCorruptError(
        f"{what} {path.name} is unreadable and no usable backup exists. "
        f"The damaged file is preserved at {kept}.")


# ── JSON ───────────────────────────────────────────────────────────────────────

def load_json(path: Path, expect: type | None = None, backups: int = 3,
              what: str = "file") -> Any:
    """Load JSON with recovery. *expect* (dict/list) makes a wrong shape count
    as corruption, so a file holding e.g. 'null' is not silently accepted."""
    def parse(p: Path) -> Any:
        value = json.loads(p.read_text(encoding="utf-8"))
        if expect is not None and not isinstance(value, expect):
            raise ValueError(f"expected {expect.__name__}, found {type(value).__name__}")
        return value
    return read_with_recovery(path, parse, backups, what)


def save_json(path: Path, obj: Any, backups: int = 3) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=2).encode("utf-8"), backups)


# ── ODS ────────────────────────────────────────────────────────────────────────

def _cell_text(cell_el: Any) -> str:
    from odf.text import P
    ps = cell_el.getElementsByType(P)
    if not ps:
        return ""
    fc = ps[0].firstChild
    if fc is None:
        return ""
    return fc.data if hasattr(fc, "data") else str(fc)


def _parse_ods(path: Path) -> dict[str, list[list[str]]]:
    """Load an ODS into {sheet: [[cell, ...], ...]}, collapsing repeated blanks."""
    from odf.opendocument import load as odf_load
    from odf.table import Table, TableRow
    from odf.namespaces import TABLENS

    doc = odf_load(str(path))
    data: dict[str, list[list[str]]] = {}
    for table in doc.spreadsheet.getElementsByType(Table):
        name: str = table.getAttribute("name")
        rows: list[list[str]] = []
        for row_el in table.getElementsByType(TableRow):
            row_repeat = int(row_el.getAttrNS(TABLENS, "number-rows-repeated") or 1)
            cells: list[str] = []
            for cell_el in row_el.childNodes:
                local = getattr(cell_el, "qname", (None, None))[1]
                if local not in ("table-cell", "covered-table-cell"):
                    continue
                col_repeat = int(cell_el.getAttrNS(TABLENS, "number-columns-repeated") or 1)
                val = _cell_text(cell_el)
                if not val and col_repeat > 1:
                    cells.append("")
                else:
                    cells.extend([val] * col_repeat)
            while cells and not cells[-1]:
                cells.pop()
            if not any(cells) and row_repeat > 1:
                rows.append([])
            else:
                for _ in range(row_repeat):
                    rows.append(list(cells))
        while rows and not any(rows[-1]):
            rows.pop()
        data[name] = rows
    return data


# Sheets written as plain text. Sheet1 is the raw OCR audit trail — typing it
# would change what it records ("0.20" becoming the number 0.2) — and _meta is
# bookkeeping. Every other sheet gets real number, date and time cells.
TEXT_ONLY_SHEETS = frozenset({"Sheet1", "_meta"})

_NUM_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")


def _typed_cell(text: str) -> Any:
    """
    A cell typed by its content, so the report can be summed, sorted and
    charted without conversion. The original text is kept as the cell's display
    text, so reading the file back returns exactly what was written.
    """
    from odf.table import TableCell
    from odf.text import P
    if _NUM_RE.match(text):
        cell = TableCell(valuetype="float", value=repr(float(text)))
    elif _DATE_RE.match(text):
        cell = TableCell(valuetype="date", datevalue=text)
    elif (m := _TIME_RE.match(text)):
        cell = TableCell(valuetype="time",
                         timevalue=f"PT{int(m[1])}H{int(m[2])}M{int(m[3])}S")
    else:
        cell = TableCell(valuetype="string")
    cell.addElement(P(text=text))
    return cell


def _write_ods(path: Path, data: dict[str, list[list[str]]]) -> None:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P

    doc = OpenDocumentSpreadsheet()
    for sheet_name, rows in data.items():
        table = Table(name=sheet_name)
        typed = sheet_name not in TEXT_ONLY_SHEETS
        for row_data in rows:
            row_el = TableRow()
            for val in row_data:
                if val in (None, ""):
                    row_el.addElement(TableCell())
                elif typed:
                    row_el.addElement(_typed_cell(str(val)))
                else:
                    cell = TableCell()
                    cell.addElement(P(text=str(val)))
                    row_el.addElement(cell)
            table.addElement(row_el)
        doc.spreadsheet.addElement(table)
    doc.save(str(path))


def ods_read(path: Path, backups: int = 2) -> dict[str, list[list[str]]] | None:
    """Read an ODS with recovery. None means the file does not exist yet."""
    return read_with_recovery(path, _parse_ods, backups, what="Spreadsheet")


def ods_save(path: Path, data: dict[str, list[list[str]]], backups: int = 2) -> None:
    """Write an ODS atomically, keeping rotating backups."""
    atomic_write_with(path, lambda p: _write_ods(p, data), backups)


# ── Snapshots and housekeeping ─────────────────────────────────────────────────

def snapshot(path: Path, label: str, keep: int = 5) -> Path | None:
    """
    Keep a labelled point-in-time copy, e.g. one per daemon start or before an
    upgrade. Only snapshots with the same label are pruned, oldest first.
    keep=0 means never prune (used for pre-upgrade copies).
    """
    path = Path(path)
    if not path.exists():
        return None
    d = backup_dir(path)
    dest = d / f"{path.stem}.{label}-{_timestamp()}{path.suffix}"
    shutil.copy2(path, dest)
    if keep > 0:
        same = sorted(d.glob(f"{path.stem}.{label}-*{path.suffix}"))
        for old in same[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return dest


def find_quarantined(folder: Path) -> list[Path]:
    """Damaged files preserved by read_with_recovery and not yet dealt with."""
    d = Path(folder) / BACKUP_DIR_NAME
    return sorted(d.glob("*.corrupt-*")) if d.exists() else []
