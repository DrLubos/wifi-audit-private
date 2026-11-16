"""The client retention (store.prune_clients(), 2026-11-16): client data not
seen for RETENTION_DAYS goes - client devices with their frequency
histograms, associations, client BSSIDs, probes - an AP never, whatever its
last_seen; at most `limit` rows a table per call."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import collector  # noqa: E402
from shape import shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import AP_RAW, CLIENT_RAW, TS  # noqa: E402

DAY = 86400
HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
AP_KEY, CLIENT_KEY = AP_RAW["kismet.device.base.key"], CLIENT_RAW["kismet.device.base.key"]


class PruneClients(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "buffer.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()

    def n(self, table, where="1"):
        return self.q("SELECT count(*) FROM %s WHERE %s" % (table, where))[0][0]

    def poll(self, ts, raws):
        self.store.write_poll(ts, HEALTH, [shape_device(r, ts) for r in raws], [], 42)

    def test_old_client_data_goes_the_ap_stays(self):
        self.poll(TS, [AP_RAW, CLIENT_RAW])
        self.assertGreater(self.n("probes"), 0)
        counts, done = self.store.prune_clients(TS + 14 * DAY)
        self.assertTrue(done)
        self.assertEqual(counts["client_devices"], 1)
        self.assertEqual(self.q("SELECT key FROM devices"), [(AP_KEY,)])           # the AP, although as old
        self.assertEqual(self.n("device_freq_hist", "key = '%s'" % CLIENT_KEY), 0)
        self.assertGreater(self.n("device_freq_hist", "key = '%s'" % AP_KEY), 0)
        self.assertEqual((self.n("associations"), self.n("client_bssids"), self.n("probes")), (0, 0, 0))

    def test_recent_client_data_stays(self):
        self.poll(TS, [AP_RAW, CLIENT_RAW])
        counts, done = self.store.prune_clients(TS - DAY)
        self.assertTrue(done)
        self.assertEqual(sum(counts.values()), 0)
        self.assertEqual(self.n("devices"), 2)

    def test_an_ap_typed_as_client_keeps_its_row(self):
        """Kismet re-types an AP that also sends as a station (server schema 10):
        its advertised configuration is the evidence."""
        self.poll(TS, [AP_RAW])
        self.store.db.execute("UPDATE devices SET type = 'client' WHERE key = ?", (AP_KEY,))
        self.store.prune_clients(TS + 30 * DAY)
        self.assertEqual(self.q("SELECT key FROM devices"), [(AP_KEY,)])

    def test_a_backlog_goes_in_slices(self):
        self.store.db.execute("BEGIN")
        for i in range(25):
            self.store.db.execute("INSERT INTO devices(key, mac, type, first_seen, last_seen) "
                                  "VALUES (?, ?, 'client', ?, ?)", ("K%02d" % i, "02:00:00:00:00:%02X" % i, TS, TS))
            self.store.db.execute("INSERT INTO probes(key, ssid, first_time, last_time) VALUES (?, 'x', ?, ?)",
                                  ("K%02d" % i, TS, TS))
        self.store.db.execute("COMMIT")
        passes, done = 0, False
        while not done:
            counts, done = self.store.prune_clients(TS + DAY, limit=10)
            self.assertLessEqual(max(counts.values()), 10)
            passes += 1
        self.assertEqual(passes, 3)
        self.assertEqual((self.n("devices"), self.n("probes")), (0, 0))

    def add_clients(self, n, last_seen):
        self.store.db.execute("BEGIN")
        for i in range(n):
            self.store.db.execute("INSERT INTO devices(key, mac, type, first_seen, last_seen) "
                                  "VALUES (?, ?, 'client', ?, ?)", ("C%02d" % i, "02:00:00:00:01:%02X" % i,
                                                                    last_seen, last_seen))
        self.store.db.execute("COMMIT")

    def test_prune_step_prunes_clients_by_the_retention(self):
        self.add_clients(1, TS)                                         # no polls: no time-series backlog
        removed, caught_up = collector.prune_step(self.store, 14, TS + 13 * DAY)
        self.assertEqual((removed["client_devices"], caught_up), (0, True))    # seen 13 days ago: stays
        removed, caught_up = collector.prune_step(self.store, 14, TS + 15 * DAY)
        self.assertEqual((removed["client_devices"], caught_up), (1, True))
        self.assertEqual(self.n("devices"), 0)

    def test_prune_step_is_not_caught_up_while_clients_remain(self):
        import store
        saved = store.Store.prune_clients.__defaults__
        store.Store.prune_clients.__defaults__ = (1,)                    # limit 1 for this test
        try:
            self.add_clients(2, TS)
            removed, caught_up = collector.prune_step(self.store, 14, TS + 15 * DAY)
            self.assertEqual((removed["client_devices"], caught_up), (1, False))
            removed, caught_up = collector.prune_step(self.store, 14, TS + 15 * DAY)
            self.assertEqual(self.n("devices"), 0)
        finally:
            store.Store.prune_clients.__defaults__ = saved

if __name__ == "__main__":
    unittest.main()
