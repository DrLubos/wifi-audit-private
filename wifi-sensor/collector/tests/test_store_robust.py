"""The store under bad input and a large backlog: one record SQLite cannot
bind is skipped and the poll is stored (the crash loop of 2026-11-12); the
pending AP load of a long stretch without NTP is folded per true hour with
the result of the per-row upserts (the 4 min 55 s fold of 2026-11-11); prune
takes any number of stale polls; the WAL file is limited. Standard library
only."""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shape import shape_alert, shape_device  # noqa: E402
from store import WAL_LIMIT_BYTES, Store  # noqa: E402
from test_shape import ALERT_RAW, AP_RAW, CLIENT_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
BOOT = "33333333-3333-4333-8333-333333333333"
DAY = 86400
LAST = AP_RAW["kismet.device.base.last_time"]


def clock(mono, synced, wall=None, boot=BOOT):
    return {"boot_id": boot, "mono": mono, "synced": synced, "chrony_offset": 0.0, "wall": wall}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "buffer.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()


class BadRecords(Base):
    def test_a_device_sqlite_cannot_bind_is_skipped_the_rest_is_stored(self):
        good_ap, bad, good_client = shape_device(AP_RAW, TS), shape_device(AP_RAW, TS), shape_device(CLIENT_RAW, TS)
        bad["key"], bad["mac"] = "KEY_BAD", "02:00:00:00:00:01"
        bad["pk"] = 2 ** 70                                   # OverflowError in the observations insert
        with self.assertLogs("collector.store", level="WARNING") as logs:
            new_obs, _ = self.store.write_poll(TS, HEALTH, [good_ap, bad, good_client], [], 5, clock=clock(10.0, True, TS))
        self.assertEqual(new_obs, 2)
        self.assertIn("KEY_BAD", logs.output[0])
        self.assertEqual(self.q("SELECT ok, new_obs, devices_active FROM polls"), [(1, 2, 3)])
        keys = {r[0] for r in self.q("SELECT key FROM observations")}
        self.assertEqual(keys, {good_ap["key"], good_client["key"]})
        # nothing of the skipped record stays behind: no device row, no history, no load
        self.assertEqual(self.q("SELECT count(*) FROM devices WHERE key = 'KEY_BAD'"), [(0,)])
        self.assertEqual(self.q("SELECT count(*) FROM ap_load_hourly WHERE key = 'KEY_BAD'"), [(0,)])
        self.assertEqual(self.store.get_meta("last_kismet_ts"), str(TS))        # the resume point moves on

    def test_a_value_of_an_unsupported_type_is_skipped_too(self):
        bad = shape_device(AP_RAW, TS)
        bad["manuf"] = {"not": "text"}                        # sqlite3 cannot bind a dict
        with self.assertLogs("collector.store", level="WARNING"):
            new_obs, _ = self.store.write_poll(TS, HEALTH, [bad, shape_device(CLIENT_RAW, TS)], [], 5)
        self.assertEqual(new_obs, 1)
        self.assertEqual(self.q("SELECT count(*) FROM polls WHERE ok = 1"), [(1,)])

    def test_a_bad_alert_is_skipped_the_others_are_stored(self):
        good, bad = shape_alert(ALERT_RAW), shape_alert(ALERT_RAW)
        bad["hash"], bad["severity"] = "other", 2 ** 70
        with self.assertLogs("collector.store", level="WARNING"):
            _, new_alerts = self.store.write_poll(TS, HEALTH, [], [bad, good], 5)
        self.assertEqual(new_alerts, 1)
        self.assertEqual(self.q("SELECT hash FROM alerts"), [(good["hash"],)])


class Fold(Base):
    """_fold_pending() against the reference: one _write_load() per pending row."""

    def fill(self, store, rng, polls=400, aps=7):
        """A stale stretch of `polls` unsynchronised polls (30 s apart, crossing
        several true hours), each with a random subset of APs, load values with
        and without the BSS Load element and with unknown client counts."""
        stale0 = TS - 20 * DAY
        for i in range(polls):
            recs = []
            for a in range(aps):
                if rng.random() < 0.3:
                    continue
                raw = dict(AP_RAW)
                raw["kismet.device.base.key"] = "4202770D00000000_%012X" % (a + 1)
                raw["kismet.device.base.macaddr"] = "02:00:00:00:00:%02X" % (a + 1)
                raw["kismet.device.base.last_time"] = LAST - 20 * DAY + 30 * i
                rec = shape_device(raw, stale0 + 30 * i)
                has_qbss = rng.random() < 0.6
                rec["ap"].update(n_clients=rng.choice([None, 0, 1, 5, 12]), qbss=1 if has_qbss else 0,
                                 qbss_stations=rng.choice([None, 0, 3, 40]) if has_qbss else None,
                                 util_pct=rng.choice([None, 0.0, 12.5, 77.25]) if has_qbss else None,
                                 adv_ch=rng.choice(["1", "6", None]))
                recs.append(rec)
            store.write_poll(stale0 + 30 * i, HEALTH, recs, [], 5, clock=clock(50.0 + 30 * i, False))
        return polls

    def reference(self, store, epoch):
        """What the per-row loop of buffers v8/v9 wrote."""
        rows = store.db.execute(
            "SELECT p.key, p.channel, p.n_clients, p.qbss, p.qbss_stations, p.util_pct, q.mono "
            "FROM ap_load_pending p JOIN polls q ON q.id = p.poll_id WHERE q.boot_id = ? ORDER BY p.poll_id, p.key",
            (BOOT,)).fetchall()
        for key, channel, n_clients, qbss, stations, util, mono in rows:
            store._write_load(key, int(round(epoch + mono)), channel,
                              {"n_clients": n_clients, "qbss": qbss, "qbss_stations": stations, "util_pct": util})
        store.db.execute("DELETE FROM ap_load_pending")
        return len(rows)

    COLS = ("key, hour, channel, polls, clients_polls, clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum, "
            "util_max, util_sum, first_ts, last_ts, sent")

    def test_same_rows_as_one_upsert_per_pending_row(self):
        other = Store(os.path.join(self.tmp.name, "reference.db"))
        try:
            n = self.fill(self.store, random.Random(7))
            self.fill(other, random.Random(7))
            pending = self.q("SELECT count(*) FROM ap_load_pending")[0][0]
            self.assertGreater(pending, 1000)
            epoch = TS + 0.37 - 50.0                             # the first stale poll is at true TS + 0.37
            # an hour row that exists already (a synchronised poll of an earlier boot) is merged into
            for s in (self.store, other):
                s._write_load("4202770D00000000_000000000001", TS + 1800, "6",
                              {"n_clients": 2, "qbss": 1, "qbss_stations": 9, "util_pct": 50.0})
            self.store.db.execute("BEGIN")
            folded = self.store._fold_pending(BOOT, epoch)
            self.store.db.execute("COMMIT")
            self.assertEqual(folded, pending)
            self.assertEqual(self.reference(other, epoch), pending)
            got = self.q("SELECT %s FROM ap_load_hourly ORDER BY key, hour, channel" % self.COLS)
            want = other.db.execute("SELECT %s FROM ap_load_hourly ORDER BY key, hour, channel" % self.COLS).fetchall()
            self.assertEqual(len(got), len(want))
            self.assertGreater(len({r[1] for r in got}), 3)       # several true hours
            for g, w in zip(got, want):
                self.assertEqual(g[:11], w[:11])
                self.assertAlmostEqual(g[11] or 0.0, w[11] or 0.0, places=6)     # util_sum: a float sum
                self.assertEqual((g[11] is None, g[12:]), (w[11] is None, w[12:]))
            self.assertEqual(self.q("SELECT count(*) FROM ap_load_pending"), [(0,)])
            self.assertEqual(n, 400)
        finally:
            other.close()

    def test_the_anchor_poll_folds_a_long_stretch_in_one_transaction(self):
        self.fill(self.store, random.Random(1), polls=300, aps=3)
        wall = TS + 300 * 30 + 0.2
        self.store.write_poll(int(wall), HEALTH, [shape_device(AP_RAW, int(wall))], [], 5,
                              clock=clock(50.0 + 300 * 30, True, wall))
        self.assertEqual(self.q("SELECT count(*) FROM ap_load_pending"), [(0,)])
        self.assertEqual(self.q("SELECT count(*) FROM clock_anchors"), [(1,)])
        stale_hour = (TS - 20 * DAY) - (TS - 20 * DAY) % 3600
        self.assertEqual(self.q("SELECT count(*) FROM ap_load_hourly WHERE hour <= ?", stale_hour + 4 * 3600), [(0,)])
        polls_folded = self.q("SELECT sum(polls) FROM ap_load_hourly WHERE key LIKE '4202770D00000000_00000000000_'")[0][0]
        observed = self.q("SELECT count(*) FROM observations o JOIN polls p ON p.id = o.poll_id "
                          "WHERE p.clock_synced = 0")[0][0]
        self.assertEqual(polls_folded, observed)                  # every unsynchronised observation counted once


class Housekeeping(Base):
    def test_prune_takes_more_stale_polls_than_sqlite_binds(self):
        """A stretch without NTP that is old as a whole by the time prune runs."""
        stale0 = TS - 60 * DAY
        self.store.db.execute("BEGIN")
        self.store.db.executemany(
            "INSERT INTO polls(ts, ok, mono, boot_id, clock_synced) VALUES (?, 1, ?, ?, 0)",
            [(stale0 + i, 10.0 + i, BOOT) for i in range(1300)])
        self.store.db.execute("INSERT INTO clock_anchors(boot_id, mono, wall, poll_id) VALUES (?, ?, ?, 1300)",
                              (BOOT, 2000.0, float(TS - 30 * DAY)))
        self.store.db.execute("COMMIT")
        removed = self.store.prune(TS - 14 * DAY)
        self.assertEqual(removed["polls"], 1300)
        self.assertEqual(self.q("SELECT count(*) FROM polls"), [(0,)])

    def test_stale_observations_go_with_their_polls(self):
        last = AP_RAW["kismet.device.base.last_time"]
        stale0 = TS - 60 * DAY
        for i in range(3):
            raw = dict(AP_RAW)
            raw["kismet.device.base.last_time"] = last - 60 * DAY + 30 * i
            self.store.write_poll(stale0 + 30 * i, HEALTH, [shape_device(raw, stale0 + 30 * i)], [], 5,
                                  clock=clock(10.0 + 30 * i, False))
        raw = dict(AP_RAW)
        raw["kismet.device.base.last_time"] = last
        self.store.write_poll(TS - 30 * DAY, HEALTH, [shape_device(raw, TS - 30 * DAY)], [], 5,
                              clock=clock(100.0, True, wall=float(TS - 30 * DAY)))
        self.assertEqual(self.q("SELECT count(*) FROM observations"), [(4,)])
        removed = self.store.prune(TS - 14 * DAY)
        self.assertEqual((removed["polls"], removed["observations"]), (4, 4))
        self.assertEqual(self.q("SELECT count(*) FROM observations"), [(0,)])

    def test_commits_are_synced_to_the_card(self):
        self.assertEqual(self.q("PRAGMA synchronous"), [(2,)])          # FULL: a power cut loses no committed poll

    def test_the_wal_file_is_limited(self):
        self.assertEqual(self.q("PRAGMA journal_size_limit"), [(WAL_LIMIT_BYTES,)])
        self.assertLessEqual(WAL_LIMIT_BYTES, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
