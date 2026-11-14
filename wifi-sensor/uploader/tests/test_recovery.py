"""What the uploader got wrong after a long stretch without NTP (the offline
run of 2026-11-02..11) and around a refused batch:

- the AP load the collector folds when its NTP anchor commits, after the
  uploader had sent the stretch's polls on a server anchor, is sent again
  (17 hours of 2026-11-02/03 never went); no server anchor is taken while
  chrony is synchronised and the collector's anchor is on its way;
- a batch carries a bounded number of load rows and of rows in all;
- a device first seen on a stale clock keeps a true first_seen in every later
  batch - also of another boot, also after the stale polls are pruned;
- a 422 for several polls is narrowed down to the poll that causes it;
- an unreadable state file is a lost state, not a failing start.

Real collector store, real batch builder, the server's own validation; a fake
client instead of the network. Standard library only."""

import importlib.util
import json
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
import state as statemod  # noqa: E402
import uploader  # noqa: E402
from shape import shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import AP_RAW, TS  # noqa: E402

A, B = "aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b"
HEALTH = {"kismet_ts": TS, "devices_total": 1, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
LAST = AP_RAW["kismet.device.base.last_time"]
STALE = 54660                       # the clock of the offline run was 15 h 11 min behind
HOUR0 = TS - TS % 3600 + 7200       # a true hour start, the stretch begins here


def load_server_validate():
    path = os.path.join(HERE, "..", "..", "..", "server", "api", "app", "ingest", "validate.py")
    spec = importlib.util.spec_from_file_location("server_validate_recovery", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VALIDATE = load_server_validate()


def ap(n, last):
    """AP number n (its own key and BSSID), last seen at `last` (Kismet's clock)."""
    raw = dict(AP_RAW)
    raw["kismet.device.base.key"] = "4202770D00000000_%012X" % n
    raw["kismet.device.base.macaddr"] = "02:00:00:00:00:%02X" % n
    raw["kismet.device.base.first_time"] = last
    raw["kismet.device.base.last_time"] = last
    return raw


class Server:
    """Answers like client.Client.post and keeps what a real server would:
    polls by seq, the load row with more polls per (AP, hour, channel)."""

    def __init__(self):
        self.polls, self.load, self.devices, self.batches = {}, {}, {}, []
        self.refuse = None          # callable(batch) -> detail to answer 422 with, or None
        self.now = TS + 400 * 86400
        self.t = 0.0

    def post(self, path, payload):
        self.t += 1.0
        if path == "/heartbeat":
            return 200, {"server_ts": float(self.server_ts), "resume": None, "last_batch_seq": None}, 20, self.t
        try:
            b = VALIDATE.batch(json.loads(json.dumps(payload)), self.now)
        except VALIDATE.Invalid as e:
            return 422, {"detail": str(e)}, 20, self.t
        detail = self.refuse(b) if self.refuse else None
        if detail:
            return 422, {"detail": detail}, 20, self.t
        cols = VALIDATE.columns
        for row in b["tables"].get("polls", []):
            p = dict(zip(cols("polls"), row))
            self.polls[p["seq"]] = p
        for row in b["tables"].get("ap_load_hourly", []):
            r = dict(zip(cols("ap_load_hourly"), row))
            k = (r["device_key"], r["hour"], r["channel"])
            if k not in self.load or r["polls"] > self.load[k]["polls"]:
                self.load[k] = r
        for row in b["tables"].get("devices", []):
            d = dict(zip(cols("devices"), row))
            old = self.devices.get(d["device_key"])
            d["first_seen"] = min(d["first_seen"], old["first_seen"]) if old else d["first_seen"]    # the merge's LEAST
            self.devices[d["device_key"]] = d
        self.batches.append(b)
        return 200, {"batch_seq": b["batch_seq"], "inserted": {}, "server_ts": float(self.server_ts)}, 20, self.t

    server_ts = TS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "buffer.db")
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.store = Store(self.db_path, boot_id=A)
        self.server = Server()
        self._sleep, self._chrony = uploader.time.sleep, uploader.clock.chrony_state
        uploader.time.sleep = lambda s: None
        self.chrony = (False, None)
        uploader.clock.chrony_state = lambda: self.chrony

    def tearDown(self):
        uploader.time.sleep, uploader.clock.chrony_state = self._sleep, self._chrony
        self.store.close()
        self.tmp.cleanup()

    def poll(self, ts, raws, boot, mono, synced, wall=None):
        self.store.write_poll(ts, HEALTH, [shape_device(r, ts) for r in raws], [], 42,
                              clock={"boot_id": boot, "mono": mono, "synced": synced, "chrony_offset": 0.0, "wall": wall})

    def uploader(self, boot=A, **cfg):
        c = dict(uploader.DEFAULTS, DB_PATH=self.db_path, STATE_PATH=self.state_path,
                 INGEST_URL="https://ingest.invalid/api/ingest/v1", INGEST_TOKEN="wsi1.7." + "ab" * 32,
                 CAPTURE_IFACE="none0")
        c.update(cfg)
        for k in uploader.NUMERIC:
            c[k] = float(c[k])
        up = uploader.Uploader(c, self.server, current_boot=boot)
        self.addCleanup(lambda: up.db is not None and up.db.close())
        return up

    def catch_up(self, up, rounds=200):
        for _ in range(rounds):
            up.backoff.until = 0.0
            if up.tick():
                return True
        return False

    def offline_stretch(self, hours, per_hour=4, aps=(1, 2)):
        """`hours` true hours of unsynchronised polls of boot A from HOUR0 on
        (the clock STALE behind); returns the mono of the next poll."""
        mono = 100.0
        for i in range(hours * per_hour):
            true = HOUR0 + i * (3600 // per_hour)
            self.poll(true - STALE, [ap(n, true - STALE) for n in aps], A, mono, False)
            mono += 3600 // per_hour
        return mono

    def anchor_poll(self, mono, hours, per_hour=4):
        """The boot's first synchronised poll: chrony stepped, the collector anchors and folds."""
        true = HOUR0 + hours * 3600
        self.chrony = (True, 0.0)
        self.poll(true, [ap(1, true)], A, mono, True, wall=float(true))


class LoadAfterALateFold(Base):
    def test_the_load_folded_after_a_server_anchor_is_sent_again(self):
        mono = self.offline_stretch(hours=6)
        up = self.uploader(CATCHUP_POLLS="4")              # one true hour a batch
        self.assertTrue(up.ensure_cursor())
        # contact for two minutes, no NTP: the uploader anchors on the server's time ...
        up.state["server_anchors"][A] = {"wall": float(HOUR0 + 6 * 3600), "mono": mono}
        # ... and sends the first four hours before the collector's anchor is there
        for _ in range(4):
            self.assertTrue(up.send_batch())
        self.assertEqual(len(self.server.polls), 16)
        self.assertEqual(self.server.load, {})                                  # pending on the sensor: nothing to send
        self.anchor_poll(mono, hours=6)                                         # now the fold commits
        with self.assertLogs("uploader", level="INFO") as logs:
            self.assertTrue(self.catch_up(up))
        self.assertIn("sending the hours from", "\n".join(logs.output))
        hours = sorted({k[1] for k in self.server.load})
        self.assertEqual(hours, [HOUR0 + h * 3600 for h in range(7)])           # all six, and the anchor poll's
        on_sensor = {(r[0], r[1], r[2]): r[3] for r in self.store.db.execute(
            "SELECT key, hour, channel, polls FROM ap_load_hourly")}
        self.assertEqual({k: v["polls"] for k, v in self.server.load.items()}, on_sensor)
        self.assertEqual(up.state["load_resent"], [A])
        # settled: a restart does not send the stretch a second time
        before = len(self.server.batches)
        true = HOUR0 + 6 * 3600 + 900
        self.poll(true, [ap(1, true)], A, mono + 900, True, wall=float(true))
        up2 = self.uploader()
        with self.assertNoLogs("uploader", level="WARNING"):
            self.assertTrue(self.catch_up(up2))
        rows = [len(b["tables"].get("ap_load_hourly", [])) for b in self.server.batches[before:]]
        self.assertEqual(rows, [1])

    def test_a_boot_that_ended_without_ntp_is_settled_without_a_rewind(self):
        self.offline_stretch(hours=1)
        self.poll(HOUR0 + 7200, [ap(1, HOUR0 + 7200)], B, 50.0, True, wall=float(HOUR0 + 7200))
        up = self.uploader(boot=B)
        self.assertTrue(up.ensure_cursor())
        up.state["server_anchors"][A] = {"wall": float(HOUR0 + 3600), "mono": 3700.0}
        self.assertTrue(self.catch_up(up))
        self.assertEqual(up.state["load_resent"], [A])
        self.assertEqual(sorted({k[1] for k in self.server.load}), [HOUR0 + 7200])

    def test_no_server_anchor_while_chrony_is_synchronised(self):
        self.offline_stretch(hours=1)
        up = self.uploader(SERVER_ANCHOR_AFTER_S="120", SERVER_ANCHOR_SYNCED_S="1800")
        self.assertTrue(up.ensure_cursor())
        t = [1000.0]
        mono = uploader.time.monotonic
        uploader.time.monotonic = lambda: t[0]
        try:
            self.chrony = (True, 0.0)                     # chrony stepped; the collector's anchor poll is committing
            up.contact_since = t[0]
            for t[0] in (1000.0 + 130, 1000.0 + 600, 1000.0 + 1700):
                self.assertIsNotNone(up.send_heartbeat())
                self.assertEqual(up.state["server_anchors"], {})
            t[0] = 1000.0 + 1900                          # a collector that never anchors: the last resort
            with self.assertLogs("uploader", level="WARNING"):
                up.send_heartbeat()
            self.assertIn(A, up.state["server_anchors"])
        finally:
            uploader.time.monotonic = mono

    def test_without_ntp_the_server_anchor_comes_after_two_minutes_as_before(self):
        self.offline_stretch(hours=1)
        up = self.uploader()
        self.assertTrue(up.ensure_cursor())
        t = [1000.0]
        mono = uploader.time.monotonic
        uploader.time.monotonic = lambda: t[0]
        try:
            up.contact_since = t[0]
            t[0] += 60
            up.send_heartbeat()
            self.assertEqual(up.state["server_anchors"], {})
            t[0] += 70
            with self.assertLogs("uploader", level="WARNING"):
                up.send_heartbeat()
            self.assertIn(A, up.state["server_anchors"])
        finally:
            uploader.time.monotonic = mono


class Limits(Base):
    def synced_polls(self, n, aps=(1,), step=30):
        for i in range(n):
            ts = HOUR0 + step * i
            self.poll(ts, [ap(a, ts) for a in aps], A, 100.0 + step * i, True, wall=float(ts))

    def test_load_rows_go_in_bounded_parts_and_none_is_passed_over(self):
        self.synced_polls(12, aps=range(1, 8), step=1200)       # 4 hours x 7 APs = 28 load rows
        limit, batch.LOAD_ROWS_PER_BATCH = batch.LOAD_ROWS_PER_BATCH, 10
        try:
            db = batch.open_buffer(self.db_path)
            cur = dict(batch.new_cursor(batch.meta(db, "buffer_id")), polls_id=11)     # only the newest poll is due
            b, cur2, _ = batch.build(db, cur, A, {}, 120)
            first = b["tables"]["ap_load_hourly"]["rows"]
            self.assertLessEqual(len(first), 10)
            self.assertLess(cur2["load_ts"], HOUR0 + 1200 * 11)                       # the cursor stops at the cut
            seen = {tuple(r[:3]) for r in first}
            for _ in range(10):                                                       # later batches bring the rest
                self.synced_polls(0)
                last = self.store.db.execute("SELECT max(ts) FROM polls").fetchone()[0] + 30
                self.poll(last, [ap(1, last)], A, 100000.0 + last, True, wall=float(last))
                b, cur2, _ = batch.build(db, cur2, A, {}, 120)
                rows = b["tables"].get("ap_load_hourly", {}).get("rows", [])
                self.assertLessEqual(len(rows), 10)
                seen |= {tuple(r[:3]) for r in rows}
            db.close()
            want = {tuple(r) for r in self.store.db.execute("SELECT key, hour, channel FROM ap_load_hourly")}
            self.assertEqual(seen, want)
        finally:
            batch.LOAD_ROWS_PER_BATCH = limit

    def test_more_rows_than_the_server_takes_means_fewer_polls(self):
        self.synced_polls(16, aps=range(1, 6))
        up = self.uploader(MAX_BATCH_ROWS="40", CATCHUP_POLLS="16")
        self.assertTrue(self.catch_up(up))
        self.assertEqual(len(self.server.polls), 16)
        self.assertGreater(len(self.server.batches), 2)
        self.assertLessEqual(max(b["rows"] for b in self.server.batches), 40)

    def test_a_refusal_is_narrowed_down_to_its_poll(self):
        self.synced_polls(16)
        seq_i = VALIDATE.columns("polls").index("seq")
        self.server.refuse = lambda b: "polls row: refused" if any(p[seq_i] == 11 for p in b["tables"].get("polls", [])) \
            else None
        up = self.uploader(CATCHUP_POLLS="16")
        with self.assertLogs("uploader", level="WARNING") as logs:
            self.assertFalse(self.catch_up(up, rounds=40))
        self.assertEqual(sorted(self.server.polls), list(range(1, 11)))          # everything before it arrived
        self.assertEqual(up.max_polls, 1)
        self.assertIn("batch 422", up.last_result)
        self.assertIn("halving", "\n".join(logs.output))
        self.server.refuse = None                                                 # fixed on the server
        self.assertTrue(self.catch_up(up))
        self.assertEqual(sorted(self.server.polls), list(range(1, 17)))
        self.assertEqual(up.max_polls, 16)


class FirstSeen(Base):
    def test_a_stale_first_seen_stays_corrected_in_later_boots_and_after_the_prune(self):
        mono = self.offline_stretch(hours=1, aps=(1,))                 # AP 1 is first seen on the stale clock
        raw_first = self.store.db.execute("SELECT first_seen FROM devices").fetchone()[0]
        self.assertEqual(raw_first, HOUR0 - STALE)                     # the sensor keeps the stale value for good
        self.anchor_poll(mono, hours=1)
        up = self.uploader()
        self.assertTrue(self.catch_up(up))
        key = "4202770D00000000_%012X" % 1
        self.assertEqual(self.server.devices[key]["first_seen"], HOUR0)
        self.assertIn(A, up.state["stale_windows"])
        # the next boot: the batch holds none of boot A's polls
        t = HOUR0 + 86400
        self.poll(t, [ap(1, t)], B, 80.0, True, wall=float(t))
        up_b = self.uploader(boot=B)
        self.assertTrue(self.catch_up(up_b))
        self.assertEqual(self.server.devices[key]["first_seen"], HOUR0)
        # the retention removes boot A's unsynchronised polls: the state still knows the window
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM polls WHERE clock_synced = 0")
        db.commit()
        db.close()
        t += 30
        self.poll(t, [ap(1, t)], B, 110.0, True, wall=float(t))
        up_c = self.uploader(boot=B)
        self.assertTrue(self.catch_up(up_c))
        self.assertEqual(self.server.devices[key]["first_seen"], HOUR0)
        self.assertEqual(up_c.state["stale_windows"][A][2], STALE)

    def test_the_window_does_not_shrink_while_the_retention_removes_its_polls(self):
        mono = self.offline_stretch(hours=4, aps=(1,))
        self.anchor_poll(mono, hours=4)
        up = self.uploader()
        self.assertTrue(self.catch_up(up))
        full = list(up.state["stale_windows"][A])
        # the retention takes the first three hours of the stale stretch (oldest first)
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM polls WHERE clock_synced = 0 AND id <= 12")
        db.commit()
        db.close()
        t = HOUR0 + 4 * 3600 + 60
        self.poll(t, [ap(1, t)], A, mono + 60, True, wall=float(t))
        up2 = self.uploader()
        self.assertTrue(self.catch_up(up2))
        self.assertEqual(up2.state["stale_windows"][A], full)
        self.assertEqual(self.server.devices["4202770D00000000_%012X" % 1]["first_seen"], HOUR0)

    def test_a_window_without_an_anchor_makes_the_value_unplaceable(self):
        self.offline_stretch(hours=1, aps=(1,))                        # boot A ends without any anchor
        t = HOUR0 + 86400
        self.poll(t, [ap(1, t)], B, 80.0, True, wall=float(t))
        up = self.uploader(boot=B)
        self.assertTrue(self.catch_up(up))
        dev = self.server.devices["4202770D00000000_%012X" % 1]
        self.assertEqual(dev["first_seen"], t)                         # the device's first placed sighting instead
        self.assertIsNone(up.state["stale_windows"][A][2])


class LostTail(Base):
    """A power cut drops the buffer's newest transactions after the uploader
    has sent them; the next boot's collector reuses their row ids (2026-11-02:
    poll 123577 twice)."""

    def synced(self, n, start=0, boot=A, mono0=100.0):
        for i in range(start, start + n):
            ts = HOUR0 + 30 * i
            self.poll(ts, [ap(1, ts), ap(2, ts)], boot, mono0 + 30 * i, True, wall=float(ts))

    def lose_tail(self, polls):
        """What the power cut does to the file: the last `polls` polls and their rows are gone."""
        db = sqlite3.connect(self.db_path)
        keep = db.execute("SELECT max(id) FROM polls").fetchone()[0] - polls
        for table in ("observations", "device_config_history", "alerts", "ap_load_pending"):
            db.execute("DELETE FROM %s WHERE poll_id > ?" % table, (keep,))
        db.execute("DELETE FROM polls WHERE id > ?", (keep,))
        db.commit()
        db.close()

    def test_rows_that_got_the_lost_polls_ids_are_sent(self):
        self.synced(6)
        up = self.uploader()
        self.assertTrue(self.catch_up(up))
        self.assertEqual(up.state["last_poll"]["id"], 6)
        self.lose_tail(2)                                  # polls 5 and 6 never reached the card
        self.store.close()
        self.store = Store(self.db_path, boot_id=B)        # the next boot: ids 5, 6, 7 again, on a stale clock
        for i in range(3):
            t = HOUR0 + 7200 + 30 * i
            self.poll(t - STALE, [ap(1, t - STALE), ap(3, t - STALE)], B, 50.0 + 30 * i, False)
        t = HOUR0 + 7200 + 90
        self.chrony = (True, 0.0)
        self.poll(t, [ap(1, t)], B, 140.0, True, wall=float(t))
        up2 = self.uploader(boot=B)
        with self.assertLogs("uploader", level="WARNING") as logs:
            self.assertTrue(self.catch_up(up2))
        self.assertIn("reused their row ids", "\n".join(logs.output))
        got = sorted((p["seq"], p["ts"]) for b in self.server.batches for p in
                     (dict(zip(VALIDATE.columns("polls"), r)) for r in b["tables"].get("polls", [])))
        new_boot = [(s, ts) for s, ts in got if ts >= HOUR0 + 7200]
        self.assertEqual(new_boot, [(5, HOUR0 + 7200), (6, HOUR0 + 7230), (7, HOUR0 + 7260), (8, HOUR0 + 7290)])
        obs = {(r[1], r[0]) for b in self.server.batches for r in b["tables"].get("observations", [])}
        key3 = "4202770D00000000_%012X" % 3
        self.assertEqual(sorted(ts for k, ts in obs if k == key3), [HOUR0 + 7200, HOUR0 + 7230, HOUR0 + 7260])
        self.assertEqual(up2.state["last_poll"]["id"], 8)
        with self.assertNoLogs("uploader", level="WARNING"):         # settled
            self.poll(t + 30, [ap(1, t + 30)], B, 170.0, True, wall=float(t + 30))
            self.assertTrue(self.catch_up(up2))

    def test_a_lost_tail_whose_ids_are_not_taken_yet(self):
        self.synced(4)
        up = self.uploader()
        self.assertTrue(self.catch_up(up))
        self.lose_tail(2)
        db = batch.open_buffer(self.db_path)
        cur, back = batch.repair_cursor(db, up.state["cursor"], up.state["last_poll"])
        db.close()
        self.assertEqual((cur["polls_id"], back), (2, 2))
        self.assertLessEqual(cur["obs_id"], up.state["cursor"]["obs_id"])

    def test_the_cursor_of_a_healthy_buffer_is_left_alone(self):
        self.synced(4)
        up = self.uploader()
        self.assertTrue(self.catch_up(up))
        db = batch.open_buffer(self.db_path)
        self.assertEqual(batch.repair_cursor(db, up.state["cursor"], up.state["last_poll"]), (up.state["cursor"], 0))
        self.assertEqual(batch.repair_cursor(db, up.state["cursor"], None), (up.state["cursor"], 0))
        db.close()

    def test_polls_pruned_by_the_retention_are_not_a_lost_tail(self):
        self.synced(6)
        up = self.uploader(CATCHUP_POLLS="2")
        self.assertTrue(up.ensure_cursor())
        self.assertTrue(up.send_batch())                    # polls 1-2 sent, 3-6 wait (an outage)
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM polls WHERE id <= 4")       # the retention takes 1-4 meanwhile
        db.commit()
        db.close()
        db = batch.open_buffer(self.db_path)
        self.assertEqual(batch.repair_cursor(db, up.state["cursor"], up.state["last_poll"]), (up.state["cursor"], 0))
        db.close()
        self.assertTrue(self.catch_up(up))
        gaps = [g for b in self.server.batches for g in b["gaps"]]
        self.assertEqual([(g["seq_from"], g["seq_to"], g["reason"]) for g in gaps], [(3, 4, "lost")])


class State(Base):
    def test_an_unreadable_state_file_is_a_lost_state(self):
        with open(self.state_path, "w") as f:
            f.write('{"cursor": {"buffer_id": "x", "polls')            # cut off by a power loss
        with self.assertLogs("uploader", level="WARNING") as logs:
            up = self.uploader()
        self.assertIsNone(up.state)
        self.assertIn("unreadable", logs.output[0])
        self.assertTrue(os.path.exists(self.state_path + ".corrupt"))
        self.assertFalse(os.path.exists(self.state_path))
        self.poll(HOUR0, [ap(1, HOUR0)], A, 100.0, True, wall=float(HOUR0))
        self.assertTrue(self.catch_up(up))                             # from the server's last poll (none here)
        self.assertEqual(sorted(self.server.polls), [1])
        self.assertIsInstance(statemod.load(self.state_path), dict)

    def test_no_state_file_is_not_called_corrupt(self):
        self.assertIsNone(statemod.load(self.state_path))
        self.assertIsNone(statemod.last_load_corrupt)


if __name__ == "__main__":
    unittest.main()
