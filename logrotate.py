#!/usr/bin/env python3
"""
logrotate.py — Size-based log rotation that never loses replay data.

The log is the one complete record of every capture: rebuild_from_log.py
replays it to regenerate reports. Python's RotatingFileHandler deletes the
oldest file once backupCount is reached, which would silently destroy that
record. This handler instead ARCHIVES: when the log reaches max_bytes it is
renamed into a logs/ folder beside it with a timestamp, and a fresh log begins.

    ocr_daemon.log                         ← current
    logs/ocr_daemon.20261003-142233.log    ← archived, oldest first by name

Nothing is deleted unless keep_archives > 0, in which case only the oldest
archives beyond that count are removed.
"""

from __future__ import annotations

import logging.handlers
import os
from datetime import datetime
from pathlib import Path

ARCHIVE_DIR = "logs"


class ArchivingFileHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, filename, max_bytes: int = 5 * 1024 * 1024,
                 keep_archives: int = 0, encoding: str = "utf-8") -> None:
        # backupCount=1 only enables rollover; doRollover below replaces the
        # numbered-chain-and-delete behaviour entirely.
        super().__init__(filename, maxBytes=max_bytes, backupCount=1,
                         encoding=encoding, delay=False)
        self.keep_archives = keep_archives

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        src = Path(self.baseFilename)
        folder = src.parent / ARCHIVE_DIR
        folder.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = folder / f"{src.stem}.{stamp}{src.suffix}"
        n = 1
        while dest.exists():                      # two rollovers in one second
            dest = folder / f"{src.stem}.{stamp}-{n}{src.suffix}"
            n += 1
        if src.exists():
            os.replace(src, dest)
        if self.keep_archives > 0:
            for old in archived_logs(src)[:-self.keep_archives]:
                try:
                    old.unlink()
                except OSError:
                    pass
        self.stream = self._open()


def archived_logs(log_path: Path) -> list[Path]:
    """Archived copies of *log_path*, oldest first."""
    log_path = Path(log_path)
    folder = log_path.parent / ARCHIVE_DIR
    if not folder.exists():
        return []
    return sorted(folder.glob(f"{log_path.stem}.*{log_path.suffix}"))
