"""batch.py: one upload batch from a buffer written by the real collector
store (buffer v8). Standard library only."""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector", "tests"))

import batch  # noqa: E402
from shape import shape_alert, shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import ALERT_RAW, AP_RAW, CLIENT_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
A, B = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
DAY = 86400
LAST = AP_RAW["kismet.device.base.last_time"]
AP_KEY = shape_device(AP_RAW, TS)["key"]
CLIENT_KEY = shape_device(CLIENT_RAW, TS)["key"]


def clk(boot, mono, synced, wall=None):
    return {"boot_id": boot, "mono": mono, "synced": synced, "chrony_offset": 0.0, "wall": wall}


def ap(last, **kw):
    raw = dict(AP_RAW, **kw)
    raw["kismet.device.base.last_time"] = last
    return raw


def client(last):
    raw = dict(CLIENT_RAW)
    raw["kismet.device.base.last_time"] = last
    return raw


def load_server_validate():
    path = os.path.join(HERE, "..", "..", "..", "server", "api", "app", "ingest", "validate.py")
    spec = importlib.util.spec_from_file_location("server_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "buffer.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def poll(self, ts, raws, c, alerts=()):
        self.store.write_poll(ts, HEALTH, [shape_device(r, ts) for r in raws], list(alerts), 42, clock=c)

    def build(self, cursor=None, boot=B, anchors=None, max_polls=120):
        db = batch.open_buffer(self.path)
        try:
            cursor = cursor or batch.new_cursor(batch.meta(db, "buffer_id"))
            return batch.build(db, cursor, boot, anchors or {}, max_polls)
        finally:
            db.close()

    @staticmethod
    def rows(b, table):
        t = b["tables"].get(table)
        return [dict(zip(t["cols"], r)) for r in t["rows"]] if t else []


class Steady(Fixture):
    def test_one_poll_one_batch_ap_only(self):
        self.poll(TS, [AP_RAW, CLIENT_RAW], clk(B, 100.0, True, TS + 0.2), alerts=[shape_alert(ALERT_RAW)])
        b, cur, info = self.build()
        self.assertEqual(b["polls"], 1)
        self.assertEqual([d["device_key"] for d in self.rows(b, "devices")], [AP_KEY])   # never the client
        self.assertEqual([o["device_key"] for o in self.rows(b, "observations")], [AP_KEY])
        poll = self.rows(b, "polls")[0]
        self.assertEqual((poll["seq"], poll["ts"], poll["ts_offset_s"]), (1, TS, None))
        self.assertEqual(len(self.rows(b, "alerts")), 1)
        self.assertEqual(len(self.rows(b, "ap_load_hourly")), 1)
        self.assertEqual(cur["polls_id"], 1)
        # nothing new: nothing to send
        self.assertIsNone(self.build(cur)[0])
        # the next poll: only its rows
        self.poll(TS + 30, [ap(LAST + 30), client(LAST + 30)], clk(B, 130.0, True, TS + 30.2))
        b2, cur2, _ = self.build(cur)
        self.assertEqual([p["seq"] for p in self.rows(b2, "polls")], [2])
        self.assertEqual([o["ts"] for o in self.rows(b2, "observations")], [TS + 30])
        self.assertEqual(self.rows(b2, "alerts"), [])

    def test_catch_up_in_order(self):
        for i in range(5):
            self.poll(TS + 30 * i, [ap(LAST + 30 * i)], clk(B, 100.0 + 30 * i, True, TS + 30 * i))
        cur, seen = None, []
        while True:
            b, cur, _ = self.build(cur, max_polls=2)
            if b is None:
                break
            seen += [p["seq"] for p in self.rows(b, "polls")]
            self.assertEqual(len(self.rows(b, "observations")), len(self.rows(b, "polls")))
        self.assertEqual(seen, [1, 2, 3, 4, 5])

    def test_frequency_histograms_at_most_once_an_hour(self):
        # every poll updates every AP's per-frequency counters; sent per poll they
        # were ~1,200 row updates per batch - now with the first batch of each hour
        hour = TS - TS % 3600
        self.poll(hour + 10, [ap(LAST)], clk(B, 10.0, True, hour + 10.0))
        b, cur, _ = self.build()
        self.assertTrue(self.rows(b, "device_freq_hist"))                 # the first batch of the hour
        self.poll(hour + 40, [ap(LAST + 30)], clk(B, 40.0, True, hour + 40.0))
        b, cur2, _ = self.build(cur)
        self.assertEqual(self.rows(b, "device_freq_hist"), [])             # same hour: none
        self.assertEqual(cur2["freq_ts"], cur["freq_ts"])
        self.poll(hour + 3610, [ap(LAST + 3600)], clk(B, 3610.0, True, hour + 3610.0))
        b, cur3, _ = self.build(cur2)
        self.assertTrue(self.rows(b, "device_freq_hist"))                 # the next hour: the latest counts
        self.assertGreater(cur3["freq_ts"], cur2["freq_ts"])

    def test_columns_equal_the_servers(self):
        v = load_server_validate()
        for name, cols in batch.COLUMNS.items():
            self.assertEqual(list(cols), v.columns(name), name)

    def test_ap_evidence_equals_the_importer(self):
        path = os.path.join(HERE, "..", "..", "..", "server", "seed", "import_snapshot.py")
        text = open(path, encoding="utf-8").read()
        self.assertIn('_AP_EVIDENCE = "%s"' % batch.AP_EVIDENCE, text)

    def test_payload_passes_the_servers_validation(self):
        v = load_server_validate()
        self.poll(TS, [AP_RAW, CLIENT_RAW], clk(B, 100.0, True, TS), alerts=[shape_alert(ALERT_RAW)])
        b, cur, _ = self.build()
        body = {"v": 1, "batch_seq": 1, "buffer_id": cur["buffer_id"], "buffer_version": "8", "sent_at": TS,
                "attempt": 1, "heartbeat": None, "tables": b["tables"], "gaps": b["gaps"], "boots": b["boots"]}
        out = v.batch(body, TS + 60)
        self.assertEqual(len(out["tables"]["polls"]), 1)


class Clock(Fixture):
    def test_held_until_the_anchor(self):
        self.poll(TS - 30 * DAY, [ap(LAST - 30 * DAY)], clk(B, 20.0, False))
        b, cur, info = self.build(boot=B)
        self.assertIsNone(b)
        self.assertEqual(info["held"], 1)
        self.assertEqual(cur["polls_id"], 0)

    def test_corrected_after_the_anchor(self):
        stale_ts = TS - 30 * DAY
        self.poll(stale_ts, [ap(LAST - 30 * DAY, disconnects_last=LAST - 30 * DAY - 5)], clk(B, 20.0, False))
        wall = TS + 3600.4
        self.poll(TS + 3600, [ap(LAST + 3600)], clk(B, 3620.0, True, wall))
        b, cur, info = self.build(boot=B)
        polls = self.rows(b, "polls")
        epoch = wall - 3620.0
        self.assertEqual(polls[0]["ts"], int(round(epoch + 20.0)))
        self.assertAlmostEqual(polls[0]["ts_offset_s"], polls[0]["ts"] - stale_ts)
        self.assertIsNone(polls[1]["ts_offset_s"])
        obs = self.rows(b, "observations")
        self.assertEqual(obs[0]["ts"], polls[0]["ts"])                     # the row placed with its poll
        self.assertGreater(obs[0]["disconnects_last"], TS - DAY)           # the stale frame time shifted
        dev = self.rows(b, "devices")[0]
        self.assertGreater(dev["first_seen"], TS - DAY)                    # never a stale first_seen
        self.assertGreaterEqual(dev["last_seen"], dev["first_seen"])
        self.assertEqual(b["boots"][0]["boot_id"], B)
        self.assertEqual(b["boots"][0]["polls_corrected"], 1)
        hours = [r["hour"] for r in self.rows(b, "ap_load_hourly")]
        true_hour = polls[0]["ts"] - polls[0]["ts"] % 3600
        self.assertIn(true_hour, hours)
        self.assertNotIn(stale_ts - stale_ts % 3600, hours)

    def test_server_anchor(self):
        self.poll(TS - 30 * DAY, [ap(LAST - 30 * DAY)], clk(B, 20.0, False))
        b, _, _ = self.build(boot=B, anchors={B: {"wall": TS + 100.0, "mono": 120.0}})
        self.assertEqual(self.rows(b, "polls")[0]["ts"], int(round(TS + 100.0 - 120.0 + 20.0)))
        self.assertEqual(b["boots"][0]["source"], "server")

    def test_a_boot_that_never_got_an_anchor_is_skipped(self):
        self.poll(TS - 30 * DAY, [ap(LAST - 30 * DAY)], clk(A, 20.0, False))
        self.poll(TS - 30 * DAY + 30, [ap(LAST - 30 * DAY + 30)], clk(A, 50.0, False))
        self.poll(TS, [ap(LAST)], clk(B, 15.0, True, TS))
        b, cur, info = self.build(boot=B)
        self.assertEqual([p["seq"] for p in self.rows(b, "polls")], [3])
        self.assertEqual(b["gaps"][0]["reason"], "unverified")
        self.assertEqual((b["gaps"][0]["seq_from"], b["gaps"][0]["seq_to"]), (1, 2))
        self.assertEqual(info["unverified"], {A: 2})
        self.assertEqual([o["ts"] for o in self.rows(b, "observations")], [TS])   # none of boot A's rows
        self.assertEqual(cur["polls_id"], 3)


class Gaps(Fixture):
    def test_pruned_before_the_upload_is_lost(self):
        for i in range(4):
            self.poll(TS + 30 * i, [ap(LAST + 30 * i)], clk(B, 10.0 + 30 * i, True, TS + 30 * i))
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM polls WHERE id IN (1, 2)")
        db.commit()
        db.close()
        cur = dict(batch.new_cursor(self.store.get_meta("buffer_id")), polls_id=0)
        b, _, _ = self.build(dict(cur, polls_id=0))
        self.assertEqual(b["gaps"], [])                          # a new uploader: nothing to call lost
        b, _, _ = self.build(dict(cur, polls_id=1))               # it had sent poll 1 when 2 was pruned
        self.assertEqual(b["gaps"], [{"seq_from": 2, "seq_to": 2, "reason": "lost"}])


class Resume(Fixture):
    def test_cursor_after_the_servers_newest_poll(self):
        for i in range(4):
            self.poll(TS + 30 * i, [ap(LAST + 30 * i)], clk(B, 10.0 + 30 * i, True, TS + 30 * i))
        db = batch.open_buffer(self.path)
        try:
            bid = batch.meta(db, "buffer_id")
            by_seq = batch.cursor_after(db, bid, {"seq": 2, "ts": TS + 30})
            by_ts = batch.cursor_after(db, bid, {"seq": None, "ts": TS + 30})       # snapshot-era poll
            fresh = batch.cursor_after(db, bid, None)
        finally:
            db.close()
        self.assertEqual(by_seq["polls_id"], 2)
        self.assertEqual(by_ts["polls_id"], 2)
        self.assertEqual(fresh["polls_id"], 0)
        b, _, _ = self.build(by_seq)
        self.assertEqual([p["seq"] for p in self.rows(b, "polls")], [3, 4])
        self.assertEqual([o["ts"] for o in self.rows(b, "observations")], [TS + 60, TS + 90])


if __name__ == "__main__":
    unittest.main()
