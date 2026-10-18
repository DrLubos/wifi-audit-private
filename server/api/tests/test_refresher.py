"""The refresher's dirty-mark handling (app/refresher.py) against a fake
connection: a refresh that dies with its connection (Postgres restarting
after a crash) must not lose the mark it took. Standard library only - psycopg
is stubbed."""

import os
import sys
import types
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg")
        stub.Error = type("Error", (Exception,), {})
        stub.errors = types.ModuleType("psycopg.errors")
        stub.errors.UndefinedTable = type("UndefinedTable", (stub.Error,), {})
        stub.connect = None
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = None
        stub.rows = rows
        sys.modules["psycopg"], sys.modules["psycopg.rows"] = stub, rows

import psycopg  # noqa: E402

from app import refresher  # noqa: E402

T1 = datetime(2026, 10, 17, 21, 10, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 17, 21, 20, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    """ingest_state as a dict; refresh_rollups() fails (and closes) on demand."""

    def __init__(self, marks, fail=False):
        self.marks, self.fail, self.closed, self.refreshed, self.runs = dict(marks), fail, False, [], []
        self.no_table = False

    def execute(self, sql, params=(), prepare=None):
        if self.closed:
            raise psycopg.Error("closed")
        if sql.startswith("SELECT i.sensor_id"):
            return Result([{"sensor_id": s, "name": "s%d" % s, "dirty_from": f, "dirty_at": f, "dirty_since": f}
                           for s, f in sorted(self.marks.items()) if f is not None])
        if sql.startswith("UPDATE ingest_state"):
            f = self.marks.get(params[0])
            if f is None:
                return Result([])
            self.marks[params[0]] = None
            return Result([{"dirty_from": f, "dirty_since": f}])
        if sql.startswith("SELECT * FROM refresh_rollups"):
            if self.fail:
                self.closed = True
                raise psycopg.Error("server closed the connection unexpectedly")
            self.refreshed.append(params)
            return Result([{"ap_hours": 1, "sensor_hours": 1}])
        if sql.startswith("INSERT INTO ingest_state"):
            sid, f = params[0], params[1]
            cur = self.marks.get(sid)
            self.marks[sid] = f if cur is None else min(cur, f)
            return Result([])
        if "refresh_contact_periods" in sql:
            return Result([{"sensor_id": 1, "gaps": 0}])
        if sql.startswith("INSERT INTO refresh_runs"):
            if self.no_table:
                raise psycopg.errors.UndefinedTable("relation refresh_runs does not exist")
            cols = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            self.runs.append(dict(zip(cols, params)))
            return Result([])
        raise AssertionError(sql)


class DirtyMarks(unittest.TestCase):
    def setUp(self):
        refresher._carry.clear()
        refresher._recording = True
        self._baselines, refresher.BASELINES = refresher.BASELINES, False

    def tearDown(self):
        refresher.BASELINES = self._baselines
        refresher._carry.clear()

    def test_mark_survives_a_dead_connection(self):
        dead = FakeConn({1: T1}, fail=True)
        with self.assertRaises(psycopg.Error):
            refresher.round_once(dead, {})
        self.assertEqual(refresher._carry, {1: (T1, T1)})
        fresh = FakeConn({1: T2})                     # an ingest marked a later window meanwhile
        refresher.round_once(fresh, {})
        self.assertEqual(fresh.refreshed, [(1, T1)])  # the earlier, lost window is refreshed
        self.assertEqual([(r["step"], r["ok"]) for r in fresh.runs], [("rollups", True), ("contact_periods", True)])
        self.assertEqual(fresh.runs[0]["refresh_from"], T1)
        self.assertEqual(fresh.runs[0]["sensor_id"], 1)
        self.assertEqual(refresher._carry, {})
        self.assertEqual(fresh.marks, {1: None})

    def test_restore_on_a_live_connection(self):
        conn = FakeConn({1: T1}, fail=True)
        conn.closed = False

        def fail_but_stay_open(sql, params=(), prepare=None, _orig=conn.execute):
            if sql.startswith("SELECT * FROM refresh_rollups"):
                raise psycopg.Error("canceling statement")
            return _orig(sql, params, prepare)
        conn.execute = fail_but_stay_open
        with self.assertRaises(psycopg.Error):
            refresher.round_once(conn, {})
        self.assertEqual(conn.marks, {1: T1})
        self.assertEqual(refresher._carry, {})


class Recording(unittest.TestCase):
    def setUp(self):
        refresher._carry.clear()
        refresher._recording = True
        self._baselines, refresher.BASELINES = refresher.BASELINES, False

    def tearDown(self):
        refresher.BASELINES = self._baselines
        refresher._recording = True

    def test_failed_refresh_is_recorded(self):
        conn = FakeConn({1: T1})

        def fail_but_stay_open(sql, params=(), prepare=None, _orig=conn.execute):
            if sql.startswith("SELECT * FROM refresh_rollups"):
                raise psycopg.Error("canceling statement due to statement timeout")
            return _orig(sql, params, prepare)
        conn.execute = fail_but_stay_open
        with self.assertRaises(psycopg.Error):
            refresher.round_once(conn, {})
        self.assertEqual(len(conn.runs), 1)
        self.assertEqual((conn.runs[0]["step"], conn.runs[0]["ok"]), ("rollups", False))
        self.assertIn("statement timeout", conn.runs[0]["error"])

    def test_missing_table_turns_recording_off(self):
        conn = FakeConn({1: T1})
        conn.no_table = True
        refresher.round_once(conn, {})           # the refresh itself goes on
        self.assertEqual(conn.refreshed, [(1, T1)])
        self.assertFalse(refresher._recording)
        conn.no_table = False
        refresher.round_once(conn, {})
        self.assertEqual(conn.runs, [])          # stays off until the next start

    def test_round_row(self):
        conn = FakeConn({})
        refresher.record(conn, "round", T2, 3.14159, next_in_s=56.9)
        self.assertEqual(conn.runs, [{"started_at": T2, "step": "round", "sensor_id": None, "duration_s": 3.142,
                                      "ok": True, "refresh_from": None, "waited_s": None, "ap_hours": None,
                                      "sensor_hours": None, "gaps": None, "baselines": None, "next_in_s": 56.9,
                                      "error": None}])


if __name__ == "__main__":
    unittest.main()
