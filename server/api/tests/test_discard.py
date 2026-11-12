"""Discard mode (docs/discard-mode.md) without a database: the body of the
Settings write (app/v2/writes.py parse_discard), what a discarded batch
records (app/ingest/discard.py), the Sensors page's coverage and status with
excluded windows (app/v2/shape.py) and the refresher's auto-expiry step
(app/refresher.py). Standard library only; the SQL side is
tests/sql/discard_test.sql."""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "psycopg" not in sys.modules:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg")
        stub.Error = type("Error", (Exception,), {})
        stub.errors = types.ModuleType("psycopg.errors")
        stub.errors.UndefinedTable = type("UndefinedTable", (stub.Error,), {})
        stub.errors.UndefinedFunction = type("UndefinedFunction", (stub.Error,), {})
        stub.connect = None
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = None
        stub.rows = rows
        sys.modules["psycopg"], sys.modules["psycopg.rows"] = stub, rows

from app import refresher  # noqa: E402
from app.ingest import discard, validate  # noqa: E402
from app.v2 import periods, shape, writes  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


class Body(unittest.TestCase):
    def test_default_six_hours(self):
        self.assertEqual(writes.parse_discard({"note": " PoE power test "}, NOW),
                         {"seconds": 6 * 3600, "until": NOW + timedelta(hours=6), "note": "PoE power test"})

    def test_duration_up_to_24_hours(self):
        self.assertEqual(writes.parse_discard({"note": "x", "duration_h": 24}, NOW)["seconds"], 86400)
        self.assertEqual(writes.parse_discard({"note": "x", "duration_h": 0.5}, NOW)["seconds"], 1800)
        for bad in (24.01, 25, 0, -1, 0.01, "6", True):
            with self.assertRaises(writes.Invalid, msg=repr(bad)):
                writes.parse_discard({"note": "x", "duration_h": bad}, NOW)
        self.assertEqual(writes.parse_discard({"note": "x", "duration_h": None}, NOW)["seconds"], 6 * 3600)

    def test_end_time(self):
        out = writes.parse_discard({"note": "x", "until": "2026-11-12T20:30:00Z"}, NOW)
        self.assertEqual((out["seconds"], out["until"]), (8 * 3600 + 1800, utc(2026, 11, 12, 20, 30)))
        out = writes.parse_discard({"note": "x", "until": "2026-11-13T13:00:00+01:00"}, NOW)
        self.assertEqual(out["seconds"], 86400)                                   # exactly 24 h ahead
        for bad in ("2026-11-12T11:00:00Z",           # in the past
                    "2026-11-12T12:00:30Z",           # less than a minute ahead
                    "2026-11-13T12:00:01Z",           # more than 24 h
                    "2026-11-12T20:00:00",            # no time zone
                    "tomorrow"):
            with self.assertRaises(writes.Invalid, msg=bad):
                writes.parse_discard({"note": "x", "until": bad}, NOW)
        with self.assertRaises(writes.Invalid):
            writes.parse_discard({"note": "x", "until": "2026-11-12T20:00:00Z", "duration_h": 2}, NOW)

    def test_note_required(self):
        for bad in ({}, {"note": ""}, {"note": "   "}, {"note": 5}, {"note": "x" * 501}):
            with self.assertRaises(writes.Invalid, msg=repr(bad)):
                writes.parse_discard(bad, NOW)
        with self.assertRaises(writes.Invalid):
            writes.parse_discard({"note": "x", "sensor": "pi-fri"}, NOW)         # unknown field


def batch(polls, gaps=(), tables=None):
    """A validated batch (validate.batch's shape) with polls [(seq, ts)]."""
    cols = validate.columns("polls")
    rows = []
    for seq, ts in polls:
        r = [None] * len(cols)
        r[cols.index("seq")], r[cols.index("ts")], r[cols.index("ok")] = seq, ts, 1
        rows.append(tuple(r))
    t = dict(tables or {})
    if rows:
        t["polls"] = rows
    return {"tables": t, "gaps": [dict(g, polls=g["seq_to"] - g["seq_from"] + 1) for g in gaps],
            "batch_seq": 9, "buffer_id": "b"}


class Discarded(unittest.TestCase):
    T = utc(2026, 11, 12, 10).timestamp()

    def test_polls_rows_and_ranges(self):
        b = batch([(101, self.T), (102, self.T + 30), (103, self.T + 60)],
                  tables={"observations": [("o",)] * 40, "alerts": [], "devices": [("d",)] * 5})
        s = discard.summary(b)
        self.assertEqual(s, {"polls": 3, "seq_from": 101, "seq_to": 103, "ts_from": self.T, "ts_to": self.T + 60,
                             "rows": {"observations": 40, "devices": 5, "polls": 3}, "unverified_polls": 0})

    def test_unverified_polls_are_part_of_the_window_lost_ones_are_not(self):
        # unverified: polls of the window the uploader could not place in time
        # (a boot without its clock anchor) - excluded with it; lost: pruned on
        # the sensor before the upload, i.e. older than the window - stored as loss
        b = batch([(205, self.T)], gaps=[{"seq_from": 200, "seq_to": 204, "reason": "unverified"},
                                         {"seq_from": 50, "seq_to": 60, "reason": "lost"}])
        s = discard.summary(b)
        self.assertEqual((s["seq_from"], s["seq_to"], s["polls"], s["unverified_polls"]), (200, 205, 1, 5))
        self.assertEqual([g["reason"] for g in discard.stored_gaps(b)], ["lost"])

    def test_a_batch_without_polls(self):
        s = discard.summary(batch([], gaps=[{"seq_from": 7, "seq_to": 9, "reason": "unverified"}]))
        self.assertEqual((s["polls"], s["seq_from"], s["seq_to"], s["ts_from"], s["ts_to"]), (0, 7, 9, None, None))
        s = discard.summary(batch([]))
        self.assertEqual((s["seq_from"], s["seq_to"], s["rows"]), (None, None, {}))

    def test_response_says_nothing_was_stored(self):
        r = discard.response(batch([(1, self.T)]), replay=False, ms=12, delivered=None, until=utc(2026, 11, 12, 18))
        self.assertEqual(r["batch_seq"], 9)
        self.assertEqual((r["staged"], r["inserted"], r["conflicts"]), ({}, {}, {}))
        self.assertEqual(r["discarded"], {"until": "2026-11-12T18:00:00Z", "polls": 1})
        self.assertIn("server_ts", r)
        self.assertIsNone(r["config"])


def hour(h, polls=120, capture=120, gap=0.0):
    return {"sensor_id": 1, "hour": h, "polls": polls, "capture_polls": capture, "gap_s": gap, "limited_polls": 0}


class Coverage(unittest.TestCase):
    """2 Nov (UTC): collecting 00:00-10:00, discard mode 10:00-16:00 (no poll
    stored), collecting again 16:00-24:00 - the window is excluded, not an outage."""

    S = {"id": 1, "first_poll": utc(2026, 11, 1, 0), "last_poll": utc(2026, 11, 2, 23, 59, 30)}
    W = [(utc(2026, 11, 2, 10), utc(2026, 11, 2, 16))]

    def rows(self):
        out = [hour(utc(2026, 11, 2, h)) for h in range(10)]
        out += [hour(utc(2026, 11, 2, h), polls=0, capture=0, gap=3600.0) for h in range(10, 16)]
        return out + [hour(utc(2026, 11, 2, h)) for h in range(16, 24)]

    def test_day_shares(self):
        days = periods.local_days("UTC", utc(2026, 11, 3, 0, 0, 1), 2)[:1]
        plain = shape.day_coverage(days, self.rows(), self.S, utc(2026, 11, 3, 1))[0]
        self.assertEqual((plain["ok"], plain["outage"], plain["excluded"]), (0.75, 0.25, 0.0))
        d = shape.day_coverage(days, self.rows(), self.S, utc(2026, 11, 3, 1), excluded=self.W)[0]
        self.assertEqual((d["ok"], d["outage"], d["excluded"], d["no_data"]), (0.75, 0.0, 0.25, 0.0))

    def test_open_window_takes_the_time_after_the_last_poll(self):
        # on since 10:00, now 13:00: the last poll at 09:59:30 - after it excluded, not "no data"
        s = dict(self.S, last_poll=utc(2026, 11, 2, 9, 59, 30))
        days = periods.local_days("UTC", utc(2026, 11, 2, 13), 1)
        d = shape.day_coverage(days, self.rows()[:10], s, utc(2026, 11, 2, 13),
                               excluded=[(utc(2026, 11, 2, 10), None)])[0]
        self.assertEqual((d["ok"], d["excluded"], d["no_data"], d["outage"]), (round(10 / 13, 3), round(3 / 13, 3), 0.0, 0.0))

    def test_window_share_leaves_the_window_out(self):
        start, end = utc(2026, 11, 2, 0), utc(2026, 11, 3, 0)
        self.assertEqual(shape.window_share(self.rows(), self.S, start, end), 0.75)
        self.assertEqual(shape.window_share(self.rows(), self.S, start, end, self.W), 1.0)
        self.assertIsNone(shape.window_share(self.rows(), self.S, utc(2026, 11, 2, 10), utc(2026, 11, 2, 16), self.W))

    def test_status_while_discarding(self):
        cap = NOW - timedelta(hours=2)            # nothing stored since the window started
        contact = {"last_contact_at": NOW - timedelta(seconds=20), "backlog_polls": 0, "held_polls": 0}
        self.assertEqual(shape.sensor_status(cap, cap, NOW, 15, contact), ("not_capturing", 7200))
        self.assertEqual(shape.sensor_status(cap, cap, NOW, 15, contact, discarding=True), ("discarding", 7200))
        # unreachable stays unreachable (the banner says discard mode is on)
        gone = dict(contact, last_contact_at=NOW - timedelta(minutes=30))
        self.assertEqual(shape.sensor_status(cap, cap, NOW, 15, gone, discarding=True), ("unreachable", 1800))


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Conn:
    closed = False

    def __init__(self, expired=(), missing=False):
        self.expired, self.missing, self.sql, self.runs = list(expired), missing, [], []

    def execute(self, sql, params=(), prepare=None):
        self.sql.append(sql)
        if "discard_close_expired" in sql:
            if self.missing:
                raise psycopg.errors.UndefinedFunction("function discard_close_expired() does not exist")
            out, self.expired = self.expired, []
            return Result(out)
        if sql.startswith("SELECT i.sensor_id"):
            return Result([])
        if "refresh_contact_periods" in sql:
            return Result([])
        if sql.startswith("INSERT INTO refresh_runs"):
            self.runs.append(params)
            return Result([])
        raise AssertionError(sql)


import psycopg  # noqa: E402

if not hasattr(psycopg.errors, "UndefinedFunction"):        # another test's stub came first
    psycopg.errors.UndefinedFunction = type("UndefinedFunction", (psycopg.Error,), {})


class Expiry(unittest.TestCase):
    def setUp(self):
        refresher._carry.clear()
        refresher._expiry = True
        self._baselines, refresher.BASELINES = refresher.BASELINES, False

    def tearDown(self):
        refresher.BASELINES = self._baselines
        refresher._expiry = True

    def test_every_round_closes_expired_windows(self):
        c = Conn([{"window_id": 3, "sensor": "pi-fri", "until": utc(2026, 11, 12, 18)}])
        refresher.round_once(c, {})
        self.assertEqual(sum("discard_close_expired" in s for s in c.sql), 1)
        self.assertTrue(c.sql[0].startswith("SELECT") and "discard_close_expired" in c.sql[0],
                        "expiry runs first in the round")
        refresher.round_once(c, {})                  # nothing left: the next round finds nothing
        self.assertEqual(sum("discard_close_expired" in s for s in c.sql), 2)

    def test_before_schema_20_the_step_turns_itself_off(self):
        c = Conn(missing=True)
        refresher.round_once(c, {})
        self.assertFalse(refresher._expiry)
        refresher.round_once(c, {})
        self.assertEqual(sum("discard_close_expired" in s for s in c.sql), 1)


if __name__ == "__main__":
    unittest.main()
