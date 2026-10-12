#!/usr/bin/env python3
"""
test_db.py — Tests for the SQLite master record (db.py).

Run:  python test_db.py

Self-contained: temporary folders only; your real database is never touched.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db          # noqa: E402
import scoring     # noqa: E402
import storage     # noqa: E402
from test_engine import ONE_HOLE, capture          # noqa: E402
from test_storage import LEGACY_SHOTS               # noqa: E402

logging.disable(logging.CRITICAL)


class TempDB(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gspro_db_"))
        self.path = self.dir / "gspro_stats.db"
        self.db = db.Database(self.path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Basics(TempDB):
    def test_rows_keep_positions_and_truncate(self):
        self.db.write_row("SG_Shots", 0, ["Hole", "SG"])
        self.db.write_row("SG_Shots", 3, ["2", "+0.5"])
        self.assertEqual(self.db.sheet_rows("SG_Shots"), [["Hole", "SG"], [], [], ["2", "+0.5"]])
        self.assertEqual(self.db.sheet_len("SG_Shots"), 4)
        self.assertEqual(self.db.truncate("SG_Shots", 1), 1)
        self.assertEqual(self.db.sheet_len("SG_Shots"), 1)

    def test_sessions_round_trip_with_typed_columns(self):
        s = {"round_id": "2026-09-05 10:00 | Rock Bay", "date": "2026-09-05", "week": "2026-W36",
             "course": "Rock Bay", "holes": 9, "shots": 30, "par": 36, "vs_par": -6,
             "sg_total": 1.25, "quality": {"strokes_captured": 20, "strokes_conceded": 2,
                                           "strokes_uncaptured": 8}}
        self.db.save_sessions([s])
        self.assertEqual(self.db.load_sessions(), [s])
        holes, cov = self.db.conn.execute("SELECT holes, coverage FROM sessions").fetchone()
        self.assertEqual((holes, cov), (9, round(22 / 30, 4)), "queryable without parsing JSON")

    def test_state_round_trip(self):
        self.db.set_state({"row_offset": 5, "round": {"hole_number": 3}})
        self.assertEqual(self.db.get_state()["round"]["hole_number"], 3)


class Transactions(TempDB):
    def test_crash_mid_capture_commits_nothing(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.write_row("Sheet1", 0, ["raw"])
                self.db.set_state({"row_offset": 1})
                raise RuntimeError("power cut")
        self.assertEqual(self.db.sheet_len("Sheet1"), 0)
        self.assertIsNone(self.db.get_state(), "rows and state roll back together")

    def test_inner_failure_rolls_back_the_whole_capture(self):
        with self.assertRaises(ValueError):
            with self.db.transaction():
                self.db.write_row("Sheet1", 0, ["raw"])
                with self.db.transaction():
                    raise ValueError("engine error")
        self.assertEqual(self.db.sheet_len("Sheet1"), 0)

    def test_threads_do_not_lose_writes(self):
        def worker(base):
            for i in range(50):
                with self.db.transaction():
                    self.db.write_row("Sheet1", base + i, [str(base + i)])
        ts = [threading.Thread(target=worker, args=(k * 100,)) for k in range(4)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(sum(1 for r in self.db.sheet_rows("Sheet1") if r), 200)

    def test_survives_reopen(self):
        with self.db.transaction():
            self.db.write_row("SG_Holes", 0, ["Hole"])
        self.db.close()
        self.db = db.Database(self.path)
        self.assertFalse(self.db.created)
        self.assertEqual(self.db.sheet_rows("SG_Holes"), [["Hole"]])


class Recovery(TempDB):
    def test_damaged_database_restored_from_snapshot(self):
        self.db.set_state({"row_offset": 42})
        self.db.snapshot("session-start")
        self.db.close()
        self.path.write_bytes(b"this is not a database")
        self.db = db.open_database(self.path)
        self.assertEqual(self.db.get_state(), {"row_offset": 42})
        self.assertEqual(len(storage.find_quarantined(self.dir)), 1, "damaged file kept")

    def test_damaged_without_snapshot_starts_fresh_and_keeps_the_damage(self):
        self.db.close()
        self.path.write_bytes(b"garbage")
        self.db = db.open_database(self.path)
        self.assertIsNone(self.db.get_state())
        self.assertEqual(len(storage.find_quarantined(self.dir)), 1)


class Migration(TempDB):
    def legacy(self, history=True, state=True, report=True):
        st, hi, od = (self.dir / "ocr_state.json", self.dir / "round_history.json",
                      self.dir / "ocr_output.ods")
        if state:
            storage.save_json(st, {"row_offset": 3, "round": {"hole_number": 4}})
        if history:
            storage.save_json(hi, [{"round_id": "a", "holes": 9, "shots": 33}])
        if report:
            storage.ods_save(od, {"Sheet1": [["r0"], ["r1"], ["r2"]], "SG_Shots": LEGACY_SHOTS,
                                  "Round Stats": [["old summary"]]})
        return st, hi, od

    def ctx(self):
        return {"headers": {"SG_Shots": list(scoring._SG_SHOTS_HEADERS),
                            "SG_Holes": list(scoring._SG_HOLES_HEADERS)}, "writer": "t"}

    def test_everything_imported_and_files_retired(self):
        st, hi, od = self.legacy()
        rep = db.migrate_legacy(self.db, st, hi, od, self.ctx())
        self.assertEqual(rep["status"], "migrated")
        self.assertEqual(self.db.get_state()["round"]["hole_number"], 4)
        self.assertEqual(self.db.load_sessions()[0]["shots"], 33)
        self.assertEqual(self.db.sheet_len("Sheet1"), 3)
        self.assertEqual(self.db.sheet_rows("SG_Shots")[0], list(scoring._SG_SHOTS_HEADERS),
                         "legacy report rows upgraded to the current columns on import")
        self.assertFalse(st.exists() or hi.exists(), "JSON files renamed *.migrated")
        self.assertTrue((self.dir / "ocr_state.json.migrated").exists())
        self.assertTrue(od.exists(), "the report stays; it is regenerated from now on")
        self.assertEqual(db.migrate_legacy(self.db, st, hi, od, self.ctx())["status"], "already")

    def test_nothing_to_migrate(self):
        rep = db.migrate_legacy(self.db, self.dir / "x.json", self.dir / "y.json",
                                self.dir / "z.ods", self.ctx())
        self.assertEqual(rep["status"], "nothing")

    def test_damaged_history_does_not_block_the_rest(self):
        st, hi, od = self.legacy(history=False)
        hi.write_text('[{"round_id": "a", "hol')
        rep = db.migrate_legacy(self.db, st, hi, od, self.ctx())
        self.assertEqual(rep["status"], "migrated")
        self.assertEqual(self.db.get_state()["round"]["hole_number"], 4)
        self.assertEqual(len(storage.find_quarantined(self.dir)), 1, "damaged history preserved")


class EngineOnDatabase(TempDB):
    """The database sink must give exactly what the in-memory sink gives."""

    CAPTURES = list(ONE_HOLE) + [capture(2, "150.0yd", "Tee", 1),
                                 capture(2, "0.3yd", "Green", 2)]

    def run_engine(self, sink):
        state = {"row_offset": 0, "round": scoring._fix_round({}), "last_capture": None}
        for i, c in enumerate(self.CAPTURES):
            ts = datetime(2026, 9, 1, 10, i * 2)
            scoring.set_clock(lambda ts=ts: ts)
            state, _, _ = scoring.handle_capture(state, c, {}, sink, {"session_gap_minutes": 90})
        scoring.close_open_hole(state["round"], sink)
        scoring._close_session(state, sink, "end of test")
        scoring.set_clock(None)
        return state

    def test_database_sink_matches_memory_sink(self):
        mem = scoring.MemorySink()
        self.run_engine(mem)
        dbs = db.DatabaseSink(self.db)
        with self.db.transaction():
            self.run_engine(dbs)
        for name in db.REPORT_SHEETS:
            self.assertEqual(self.db.sheet_rows(name), mem.sheets.get(name, []), name)
        self.assertEqual(json.dumps(self.db.load_sessions(), sort_keys=True),
                         json.dumps(mem.history, sort_keys=True))
        self.assertTrue(dbs.archived)

    def test_report_generated_from_database(self):
        dbs = db.DatabaseSink(self.db)
        state = self.run_engine(dbs)
        sheets = db.report_sheets(self.db, state["round"], "vtest")
        self.assertEqual(list(sheets)[-1], "_meta")
        self.assertIn("SG_Summary", sheets)
        self.assertIn("Round Stats", sheets)
        self.assertEqual(sheets["SG_Holes"], self.db.sheet_rows("SG_Holes"))
        self.assertIn("Clubs", sheets)

    def test_stored_rows_upgraded_to_current_layout(self):
        """A v3.2 database holds format-3 rows; they must gain the Club column."""
        new_h = list(scoring._SG_SHOTS_HEADERS)
        old_h = [c for c in new_h if c != "Club"]
        r1 = {"Date": "2026-09-05", "Hole": "1", "Shot#": "1", "Region9": "S Wedge"}
        r2 = {"Date": "2026-09-05", "Hole": "1", "Shot#": "2", "Region9": "Putter"}
        for i, row in enumerate((old_h, [r1.get(c, "") for c in old_h], [r2.get(c, "") for c in old_h])):
            self.db.write_row("SG_Shots", i, row)
        ctx = {"headers": {"SG_Shots": new_h, "SG_Holes": list(scoring._SG_HOLES_HEADERS)}, "writer": "t"}
        notes = db.upgrade_report_rows(self.db, ctx)
        rows = self.db.sheet_rows("SG_Shots")
        self.assertEqual(rows[0], new_h)
        self.assertEqual(rows[2][new_h.index("Club")], "S Wedge")
        import ods_upgrade
        self.assertEqual(self.db.get_meta("report_format"), str(ods_upgrade.FORMAT_VERSION))
        self.assertTrue(notes)
        self.assertEqual(db.upgrade_report_rows(self.db, ctx), [], "second run is a no-op")


class SnapshotReads(unittest.TestCase):
    """v3.8.3 — the background report reads a snapshot without the writer's lock."""

    def test_writers_carry_on_and_the_reader_sees_one_moment(self):
        import tempfile, time
        tmp = Path(tempfile.mkdtemp(prefix="gspro_snap_"))
        d = db.Database(tmp / "t.db")
        with d.transaction():
            d.write_row("SG_Shots", 0, ["Hole"])
            d.write_row("SG_Shots", 1, ["1"])
        with d.reading() as rd:
            first = rd.sheet_rows("SG_Shots")                # the snapshot starts here
            t0 = time.perf_counter()
            with d.transaction():                            # a capture commits meanwhile
                d.write_row("SG_Shots", 2, ["2"])
            self.assertLess(time.perf_counter() - t0, 1.0, "the write was not held up")
            self.assertEqual(rd.sheet_rows("SG_Shots"), first, "still the same moment")
        self.assertEqual(len(d.sheet_rows("SG_Shots")), 3, "the write is there afterwards")
        d.conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
