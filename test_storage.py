#!/usr/bin/env python3
"""
test_storage.py — Tests for storage.py (crash safety) and ods_upgrade.py.

Run:  python test_storage.py

Self-contained: uses temporary folders and never touches your real files.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import storage                       # noqa: E402
import ods_upgrade                   # noqa: E402


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_test_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def corrupt(self, path: Path, data: bytes = b'{"half": [1, 2'):
        """Simulate a write that died partway: replace the file with junk."""
        path.write_bytes(data)


# ── Atomic writes and backups ──────────────────────────────────────────────────

class AtomicWrites(TempDirCase):
    def test_writes_and_rotates_backups(self):
        p = self.dir / "s.json"
        for i in range(4):
            storage.save_json(p, {"v": i}, backups=3)
        self.assertEqual(json.loads(p.read_text()), {"v": 3})
        self.assertEqual(json.loads(storage.backup_path(p, 1).read_text()), {"v": 2})
        self.assertEqual(json.loads(storage.backup_path(p, 2).read_text()), {"v": 1})
        self.assertEqual(json.loads(storage.backup_path(p, 3).read_text()), {"v": 0})

    def test_failed_write_leaves_original_intact(self):
        p = self.dir / "s.json"
        storage.save_json(p, {"good": True})

        def explode(tmp: Path):
            tmp.write_text('{"partial')
            raise RuntimeError("process died mid-write")

        with self.assertRaises(RuntimeError):
            storage.atomic_write_with(p, explode)
        self.assertEqual(json.loads(p.read_text()), {"good": True})
        leftovers = [f for f in self.dir.iterdir() if f.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "temp file should be cleaned up")


# ── Recovery ───────────────────────────────────────────────────────────────────

class Recovery(TempDirCase):
    def test_damaged_file_restored_from_backup_and_preserved(self):
        p = self.dir / "h.json"
        storage.save_json(p, ["a"], backups=3)
        storage.save_json(p, ["a", "b"], backups=3)
        self.corrupt(p)
        self.assertEqual(storage.load_json(p, expect=list), ["a"])
        self.assertEqual(json.loads(p.read_text()), ["a"], "file put back on disk")
        self.assertEqual(len(storage.find_quarantined(self.dir)), 1,
                         "damaged original must be kept, not deleted")

    def test_damaged_with_no_backup_raises_and_preserves(self):
        p = self.dir / "h.json"
        self.corrupt(p)
        with self.assertRaises(storage.DataCorruptError):
            storage.load_json(p, expect=list, backups=3)
        self.assertEqual(len(storage.find_quarantined(self.dir)), 1)

    def test_missing_file_is_new_not_resurrected(self):
        p = self.dir / "s.json"
        storage.save_json(p, {"v": 1})
        storage.save_json(p, {"v": 2})
        p.unlink()                      # user deleted it on purpose
        self.assertIsNone(storage.load_json(p, expect=dict))
        self.assertFalse(p.exists(), "a deliberate delete must be respected")

    def test_wrong_shape_counts_as_damage(self):
        p = self.dir / "h.json"
        storage.save_json(p, ["kept"], backups=2)
        storage.save_json(p, ["kept", "x"], backups=2)
        p.write_text("null")
        self.assertEqual(storage.load_json(p, expect=list), ["kept"])

    def test_regression_history_not_erased_by_next_archive(self):
        """
        The original bug: damaged history read as [], so archiving the next
        session saved ONE session over it and erased everything before.
        """
        p = self.dir / "round_history.json"
        sessions = []
        for i in range(3):
            sessions.append({"round_id": f"s{i}"})
            storage.save_json(p, sessions, backups=5)
        self.corrupt(p)
        # what _archive_round does: load, append, save
        hist = storage.load_json(p, expect=list, backups=5) or []
        hist.append({"round_id": "s3"})
        storage.save_json(p, hist, backups=5)
        ids = [s["round_id"] for s in json.loads(p.read_text())]
        self.assertGreaterEqual(len(ids), 3, f"history was erased: {ids}")
        self.assertIn("s3", ids)


# ── ODS ────────────────────────────────────────────────────────────────────────

class OdsSafety(TempDirCase):
    def sample(self):
        return {"Sheet1": [["a", "b"]], "SG_Shots": [["Hole"], ["1"]],
                "Round Stats": [["x"]]}

    def test_round_trip(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, self.sample())
        self.assertEqual(storage.ods_read(p), self.sample())

    def test_damaged_report_restored_with_all_sheets(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, self.sample(), backups=2)
        storage.ods_save(p, self.sample(), backups=2)
        self.corrupt(p, b"PK\x03\x04 not a real zip")
        data = storage.ods_read(p, backups=2)
        self.assertEqual(set(data), {"Sheet1", "SG_Shots", "Round Stats"},
                         "every sheet must survive — the old bug kept only one")

    def test_damaged_report_without_backup_raises(self):
        """Raising (not returning {}) is what stops the next save wiping sheets."""
        p = self.dir / "r.ods"
        self.corrupt(p, b"garbage")
        with self.assertRaises(storage.DataCorruptError):
            storage.ods_read(p, backups=2)


class Snapshots(TempDirCase):
    def test_pruning_and_permanent(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, {"S": [["1"]]})
        import time
        for _ in range(4):
            storage.snapshot(p, "session-start", keep=2)
            time.sleep(1.05)            # timestamps have 1-second resolution
        keep2 = list((self.dir / "backups").glob("r.session-start-*.ods"))
        self.assertEqual(len(keep2), 2)
        storage.snapshot(p, "pre-upgrade-v1", keep=0)
        storage.snapshot(p, "session-start", keep=2)
        self.assertEqual(len(list((self.dir / "backups").glob("r.pre-upgrade-v1-*"))), 1,
                         "pre-upgrade copies are never pruned")


# ── Upgrades ───────────────────────────────────────────────────────────────────

def current_sg_shots_headers() -> list[str]:
    """
    The real SG_Shots header list. Imported from scoring.py, where it has lived
    since v2.9.0; earlier this read ocr_daemon.py's source, which broke when the
    headers moved. Importing the actual constant cannot go stale that way.
    """
    import scoring
    return list(scoring._SG_SHOTS_HEADERS)


LEGACY_SHOTS = [
    ["Hole", "Par", "Shot#", "SG Category", "Pre-Dist (yd)", "Pre-Lie",
     "Post-Dist (yd)", "Post-Lie", "SG", "Total Length (yd)", "Course"],
    ["1", "4", "2", "Approach", "150.0", "Fairway", "4.0", "Green",
     "+0.512", "146.0", "Rock Bay Golf"],
    ["1", "4", "3", "Putting", "4.0", "Green", "0.3", "Green",
     "+0.150", "3.8", "Rock Bay Golf"],
]


class Upgrades(TempDirCase):
    def ctx(self):
        return {"headers": {"SG_Shots": current_sg_shots_headers(),
                            "SG_Holes": ["Date", "Time", "Hole", "Par", "Score"]},
                "writer": "test"}

    def test_detect_format(self):
        self.assertEqual(ods_upgrade.detect_format({}), 0)
        self.assertEqual(ods_upgrade.detect_format({"SG_Shots": LEGACY_SHOTS}), 1)
        d = {"SG_Shots": [["x"]]}
        ods_upgrade.write_meta(d, 2, "t")
        self.assertEqual(ods_upgrade.detect_format(d), 2)

    def test_legacy_rows_relaid_and_green_converted_to_feet(self):
        data = {"SG_Shots": [list(r) for r in LEGACY_SHOTS]}
        frm, to, notes = ods_upgrade.upgrade_data(data, self.ctx())
        self.assertEqual((frm, to), (1, ods_upgrade.FORMAT_VERSION))
        h = data["SG_Shots"][0]
        self.assertEqual(h, current_sg_shots_headers())
        approach = dict(zip(h, data["SG_Shots"][1]))
        putt     = dict(zip(h, data["SG_Shots"][2]))
        self.assertEqual((approach["Pre-Dist"], approach["Pre Unit"]), ("150.0", "yd"))
        self.assertEqual((approach["Post-Dist"], approach["Post Unit"]), ("12.0", "ft"))
        self.assertEqual((putt["Pre-Dist"], putt["Pre Unit"]), ("12.0", "ft"))
        self.assertEqual(putt["Total Length"], "11.4", "a putt's roll in feet")
        self.assertEqual(approach["Total Length"], "146.0")
        self.assertEqual(approach["SG"], "+0.512", "other columns preserved")
        self.assertEqual(approach["Course"], "Rock Bay Golf")

    def test_upgrade_file_backs_up_and_is_idempotent(self):
        p = self.dir / "ocr_output.ods"
        storage.ods_save(p, {"Sheet1": [["raw"]], "SG_Shots": LEGACY_SHOTS})
        rep = ods_upgrade.upgrade_file(p, self.ctx())
        self.assertEqual(rep["status"], "upgraded")
        self.assertTrue(Path(rep["backup"]).exists())
        data = storage.ods_read(p)
        self.assertEqual(data["Sheet1"], [["raw"]], "raw sheet never altered")
        meta = ods_upgrade.read_meta(data)
        self.assertEqual(meta["upgraded_from"], "1")
        self.assertEqual(ods_upgrade.upgrade_file(p, self.ctx())["status"], "current")

    def test_newer_format_left_untouched(self):
        p = self.dir / "r.ods"
        d = {"SG_Shots": [["anything"]]}
        ods_upgrade.write_meta(d, ods_upgrade.FORMAT_VERSION + 1, "future")
        storage.ods_save(p, d)
        before = p.read_bytes()
        self.assertEqual(ods_upgrade.upgrade_file(p, self.ctx())["status"], "newer")
        self.assertEqual(p.read_bytes(), before)

    def test_alias_targets_exist_in_real_headers(self):
        """Catches drift: renaming a header must not silently orphan an alias."""
        real = set(current_sg_shots_headers())
        missing = {t for t in ods_upgrade._SG_SHOTS_ALIASES.values() if t not in real}
        self.assertEqual(missing, set())


# ── Typed cells (format 3) ─────────────────────────────────────────────────────

def cell_types(path: Path, sheet: str) -> list[str]:
    """office:value-type of every non-empty cell in *sheet*, in order."""
    from odf.opendocument import load
    from odf.table import Table, TableCell
    doc = load(str(path))
    for t in doc.spreadsheet.getElementsByType(Table):
        if t.getAttribute("name") == sheet:
            return [c.getAttribute("valuetype") or "" for c in t.getElementsByType(TableCell)
                    if storage._cell_text(c)]
    return []


class TypedCells(TempDirCase):
    SAMPLE = {"Sheet1":   [["150", "Hole 3", "2026-09-05"]],
              "SG_Shots": [["Hole", "SG", "Date", "Time", "Club"],
                           ["3", "+0.512", "2026-09-05", "10:58:20", "L Wedge"]],
              "_meta":    [["key", "value"], ["format_version", "3"]]}

    def test_numbers_dates_times_are_typed(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, self.SAMPLE)
        self.assertEqual(cell_types(p, "SG_Shots")[5:],
                         ["float", "float", "date", "time", "string"])

    def test_raw_audit_sheet_and_meta_stay_text(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, self.SAMPLE)
        self.assertNotIn("float", cell_types(p, "Sheet1"), "raw OCR is never re-typed")
        self.assertNotIn("float", cell_types(p, "_meta"))

    def test_round_trip_text_is_unchanged(self):
        p = self.dir / "r.ods"
        storage.ods_save(p, self.SAMPLE)
        self.assertEqual(storage.ods_read(p), self.SAMPLE, "'+0.512' must read back as '+0.512'")

    def test_upgrade_2_to_3(self):
        p = self.dir / "r.ods"
        d = {"SG_Shots": [["Hole", "SG"], ["3", "+0.5"]]}
        ods_upgrade.write_meta(d, 2, "v2.9")
        storage.ods_save(p, d)
        rep = ods_upgrade.upgrade_file(p, {"headers": {"SG_Shots": ["Hole", "SG"], "SG_Holes": []},
                                           "writer": "t"})
        self.assertEqual((rep["status"], rep["from"], rep["to"]),
                         ("upgraded", 2, ods_upgrade.FORMAT_VERSION))
        self.assertIn("float", cell_types(p, "SG_Shots"))


class ClubColumnUpgrade(TempDirCase):
    """Format 4: each old shot is credited to the club shown BEFORE it."""

    def test_old_rows_credited_from_their_predecessor(self):
        import scoring
        new_h = list(scoring._SG_SHOTS_HEADERS)
        old_h = [c for c in new_h if c != "Club"]                   # the format-3 layout
        def row(shot, after_club, note=""):
            d = {"Date": "2026-09-05", "Hole": "1", "Shot#": str(shot), "Region9": after_club,
                 "Note": note}
            return [d.get(c, "") for c in old_h]
        data = {"SG_Shots": [old_h, row(1, "L Weda"), row(2, "Putter"), row(3, "Driver"),
                             row(4, "", note="uncaptured stroke")]}
        ods_upgrade.write_meta(data, 3, "v3.2")
        _, _, notes = ods_upgrade.upgrade_data(data, {"headers": {"SG_Shots": new_h,
                                                                  "SG_Holes": []}, "writer": "t"})
        ci = new_h.index("Club")
        self.assertEqual([r[ci] for r in data["SG_Shots"][1:]], ["", "L Wedge", "Putter", ""],
                         "shot 1 unknown; shot 2 hit with the club shown after shot 1; "
                         "inferred stroke has no club")
        self.assertTrue(any("2 shot(s) credited" in n for n in notes), notes)


class Format5Upgrade(TempDirCase):
    def test_categories_relabelled_and_wind_column_renamed(self):
        import scoring
        h = [c if c != "Wind Speed (E1)" else "Wind Dir 1 (E1)" for c in scoring._SG_SHOTS_HEADERS]
        def row(**kv):
            return [kv.get(c, "") for c in h]
        data = {"SG_Shots": [h,
            row(**{"SG Category": "Putting", "Note": "uncaptured putt", "Wind Dir 1 (E1)": "SE (Tail+Right)"}),
            row(**{"SG Category": "Off Tee", "Par": "3", "Pre-Dist": "165.0", "Pre Unit": "yd"}),
            row(**{"SG Category": "Off Tee", "Par": "4", "Pre-Dist": "400.0", "Pre Unit": "yd"})]}
        ods_upgrade.write_meta(data, 4, "v3.3")
        ods_upgrade.upgrade_data(data, {"headers": {"SG_Shots": list(scoring._SG_SHOTS_HEADERS),
                                                    "SG_Holes": []}, "writer": "t"})
        nh = data["SG_Shots"][0]
        self.assertIn("Wind Speed (E1)", nh)
        rows = [dict(zip(nh, r)) for r in data["SG_Shots"][1:]]
        self.assertEqual([r["SG Category"] for r in rows], ["Unattributed", "Approach", "Off Tee"])
        self.assertEqual(rows[0]["Wind Speed (E1)"], "", "meaningless old values cleared")


# ── Log archiving ──────────────────────────────────────────────────────────────

class LogArchiving(TempDirCase):
    def logger(self, keep=0):
        import logging, logrotate
        lg = logging.getLogger(f"rot-{id(self)}-{keep}")
        lg.propagate = False
        lg.setLevel(logging.INFO)
        h = logrotate.ArchivingFileHandler(self.dir / "ocr_daemon.log", max_bytes=2000,
                                           keep_archives=keep)
        lg.addHandler(h)
        return lg, h

    def test_rollover_archives_and_loses_nothing(self):
        import logrotate
        lg, h = self.logger()
        for i in range(300):
            lg.info("capture line %04d %s", i, "x" * 40)
        h.close()
        archives = logrotate.archived_logs(self.dir / "ocr_daemon.log")
        self.assertGreater(len(archives), 1, "the log rolled over")
        text = "".join(p.read_text() for p in archives) + (self.dir / "ocr_daemon.log").read_text()
        found = sorted(int(l.split()[2]) for l in text.splitlines() if l.startswith("capture line"))
        self.assertEqual(found, list(range(300)), "every line survives, in archives or the live log")

    def test_keep_archives_prunes_oldest_only(self):
        import logrotate
        lg, h = self.logger(keep=2)
        for i in range(300):
            lg.info("capture line %04d %s", i, "x" * 40)
        h.close()
        self.assertEqual(len(logrotate.archived_logs(self.dir / "ocr_daemon.log")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
