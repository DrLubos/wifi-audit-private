"""Tests for store.py using an in-memory-like temporary database."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shape import shape_alert, shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import ALERT_RAW, AP_RAW, CLIENT_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1,
          "ds_error": None, "ds_packets": 1000}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "sub", "buffer.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()

    def poll(self, ts, raws, alerts=(), health=None):
        recs = [shape_device(r, ts) for r in raws]
        return self.store.write_poll(ts, health or HEALTH, recs, list(alerts), 42)

    def test_first_poll_writes_everything(self):
        new_obs, new_alerts = self.poll(TS, [AP_RAW, CLIENT_RAW])
        self.assertEqual((new_obs, new_alerts), (2, 0))
        self.assertEqual(self.q("SELECT COUNT(*) FROM devices")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM observations")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM associations")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM probes")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM device_freq_hist")[0][0], 4)
        self.assertEqual(self.store.get_meta("last_kismet_ts"), str(TS))
        polls = self.q("SELECT devices_total, devices_active, new_obs, ok FROM polls")
        self.assertEqual(polls, [(2, 2, 2, 1)])

    def test_ap_observation_columns(self):
        self.poll(TS, [AP_RAW])
        row = self.q("SELECT rssi, n_clients, disconnects, disconnects_last, qbss_stations, "
                     "util_pct, bss_timestamp, ie_checksum, beacon_fp, bssid FROM observations")[0]
        self.assertEqual(row[:5], (-54, 2, 3, 1789481102, 1))
        self.assertAlmostEqual(row[5], 2.352941)
        self.assertEqual(row[6:], (1628729078212, 1851240727, 563282674, None))

    def test_client_observation_columns(self):
        self.poll(TS, [CLIENT_RAW])
        row = self.q("SELECT rssi, n_clients, disconnects, disconnects_last, bssid "
                     "FROM observations")[0]
        self.assertEqual(row, (-76, None, None, None, "02:0B:0A:03:02:01"))

    def test_signal_sentinel_stored_as_null(self):
        raw = dict(CLIENT_RAW, sig_last=0)
        self.poll(TS, [raw])
        self.assertEqual(self.q("SELECT rssi FROM observations"), [(None,)])

    def test_unchanged_device_is_not_observed_twice(self):
        self.poll(TS, [AP_RAW])
        # Same last_time again (the overlap window re-delivers the device).
        new_obs, _ = self.poll(TS + 30, [AP_RAW])
        self.assertEqual(new_obs, 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM observations")[0][0], 1)
        # New packets -> new observation.
        raw = dict(AP_RAW)
        raw["kismet.device.base.last_time"] += 20
        new_obs, _ = self.poll(TS + 60, [raw])
        self.assertEqual(new_obs, 1)
        self.assertEqual(self.q("SELECT last_seen FROM devices")[0][0],
                         AP_RAW["kismet.device.base.last_time"] + 20)

    def test_config_change_is_logged_once(self):
        self.poll(TS, [AP_RAW])
        self.assertEqual(self.q("SELECT COUNT(*) FROM device_config_history")[0][0], 0)
        changed = dict(AP_RAW, crypt="Open", crypt_bits=0, adv_ch="11")
        changed["kismet.device.base.last_time"] += 10
        self.poll(TS + 30, [changed])
        hist = self.q("SELECT ts, crypt, adv_channel FROM device_config_history")
        self.assertEqual(hist, [(TS + 30, "WPA2 WPA2-PSK AES-CCMP", "8")])
        cur = self.q("SELECT crypt, adv_channel, config_changed_at FROM devices")
        self.assertEqual(cur, [("Open", "11", TS + 30)])
        # Same config again: nothing appended.
        changed["kismet.device.base.last_time"] += 10
        self.poll(TS + 60, [changed])
        self.assertEqual(self.q("SELECT COUNT(*) FROM device_config_history")[0][0], 1)

    def test_varying_ie_checksum_does_not_log_config_change(self):
        self.poll(TS, [AP_RAW])
        raw = dict(AP_RAW, ie_sum=1, beacon_fp=2, util_pct=40.0)
        raw["kismet.device.base.last_time"] += 10
        self.poll(TS + 30, [raw])
        self.assertEqual(self.q("SELECT COUNT(*) FROM device_config_history")[0][0], 0)
        self.assertEqual(self.q("SELECT ie_checksum FROM observations ORDER BY ts"),
                         [(1851240727,), (1,)])

    def test_probes_are_merged(self):
        self.poll(TS, [CLIENT_RAW])
        raw = dict(CLIENT_RAW)
        raw["kismet.device.base.last_time"] += 10
        raw["probed"] = [dict(CLIENT_RAW["probed"][1],
                              **{"dot11.probedssid.last_time": 1789481150})]
        self.poll(TS + 30, [raw])
        rows = self.q("SELECT ssid, first_time, last_time FROM probes ORDER BY ssid")
        self.assertEqual(rows, [("", 1789480391, 1789480571),
                                ("Example-Net", 1789480400, 1789481150)])

    def test_associations_are_merged(self):
        self.poll(TS, [AP_RAW])
        raw = dict(AP_RAW)
        raw["kismet.device.base.last_time"] += 10
        self.poll(TS + 30, [raw])
        rows = self.q("SELECT client_mac, first_seen, last_seen, sent FROM associations "
                      "ORDER BY client_mac")
        self.assertEqual(rows, [("02:AA:00:00:00:01", TS, TS + 30, 0),
                                ("02:AA:00:00:00:02", TS, TS + 30, 0)])
        # Only one client still listed: the other row is kept, not advanced.
        raw = dict(raw, clients={"02:AA:00:00:00:02": "x"})
        raw["kismet.device.base.last_time"] += 10
        self.poll(TS + 60, [raw])
        rows = self.q("SELECT client_mac, first_seen, last_seen FROM associations "
                      "ORDER BY client_mac")
        self.assertEqual(rows, [("02:AA:00:00:00:01", TS, TS + 30),
                                ("02:AA:00:00:00:02", TS, TS + 60)])

    def test_alerts_deduplicated_by_hash(self):
        a = shape_alert(ALERT_RAW)
        _, n1 = self.poll(TS, [], alerts=[a])
        _, n2 = self.poll(TS + 30, [], alerts=[a])
        self.assertEqual((n1, n2), (1, 0))
        self.assertEqual(self.q("SELECT header, severity FROM alerts"), [("DEAUTHFLOOD", 10)])

    def test_failed_poll_row(self):
        self.store.write_failed_poll(TS, "GET /system/status.json: refused", 7)
        self.assertEqual(self.q("SELECT ok, error FROM polls"),
                         [(0, "GET /system/status.json: refused")])

    def test_prune_keeps_identity(self):
        self.poll(TS, [AP_RAW, CLIENT_RAW])
        removed = self.store.prune(TS + 1)
        self.assertEqual(removed, {"observations": 2, "polls": 1})
        self.assertEqual(self.q("SELECT COUNT(*) FROM devices")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM associations")[0][0], 2)
        self.assertEqual(self.q("SELECT COUNT(*) FROM probes")[0][0], 2)

    def test_reopen_keeps_schema_version(self):
        path = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.get_meta("schema_version"), "3")

    def test_migrates_v2_to_v3_keeps_observations(self):
        path = os.path.join(self.tmp.name, "v2.db")
        v2 = sqlite3.connect(path)
        v2.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '2');
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, key TEXT NOT NULL,
              last_time INTEGER NOT NULL, freq_khz INTEGER, channel TEXT,
              rssi INTEGER, rssi_min INTEGER, rssi_max INTEGER,
              pk_total INTEGER, pk_tx INTEGER, pk_rx INTEGER, pk_data INTEGER, bytes INTEGER,
              n_clients INTEGER, disconnects INTEGER, qbss_stations INTEGER, util_pct REAL,
              bss_timestamp INTEGER, ie_checksum INTEGER, beacon_fp INTEGER,
              bssid TEXT, sent INTEGER NOT NULL DEFAULT 0);
            INSERT INTO observations(ts, key, last_time, rssi, disconnects)
              VALUES (100, 'AP', 99, -50, 2);
        """)
        v2.close()
        store = Store(path)
        try:
            self.assertEqual(store.get_meta("schema_version"), "3")
            cols = [r[1] for r in store.db.execute("PRAGMA table_info(observations)")]
            self.assertIn("disconnects_last", cols)
            # The pre-migration row survives, with the new column NULL.
            self.assertEqual(store.db.execute(
                "SELECT ts, rssi, disconnects, disconnects_last FROM observations").fetchall(),
                [(100, -50, 2, None)])
            # New polls fill it in.
            store.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [], 1)
            self.assertEqual(store.db.execute(
                "SELECT disconnects_last FROM observations WHERE ts = ?", (TS,)).fetchall(),
                [(1789481102,)])
        finally:
            store.close()
        # Reopening a migrated buffer is a no-op.
        store = Store(path)
        try:
            self.assertEqual(store.get_meta("schema_version"), "3")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        finally:
            store.close()

    def test_migrates_v1_associations(self):
        path = os.path.join(self.tmp.name, "v1.db")
        v1 = sqlite3.connect(path)
        v1.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE associations (
              ts INTEGER NOT NULL, ap_key TEXT NOT NULL, client_mac TEXT NOT NULL,
              PRIMARY KEY (ts, ap_key, client_mac)) WITHOUT ROWID;
            INSERT INTO associations VALUES (1, 'AP', 'C1'), (2, 'AP', 'C1'), (2, 'AP', 'C2');
        """)
        v1.close()
        store = Store(path)
        try:
            # v1 -> v2 -> v3 in one start.
            self.assertEqual(store.get_meta("schema_version"), "3")
            cols = [r[1] for r in store.db.execute("PRAGMA table_info(associations)")]
            self.assertEqual(cols, ["ap_key", "client_mac", "first_seen", "last_seen", "sent"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM associations").fetchone()[0], 0)
            # The migrated buffer accepts polls normally.
            store.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM associations").fetchone()[0], 2)
        finally:
            store.close()

    def test_unknown_schema_version_is_refused(self):
        path = os.path.join(self.tmp.name, "v9.db")
        v9 = sqlite3.connect(path)
        v9.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                         "INSERT INTO meta VALUES ('schema_version', '9');")
        v9.close()
        with self.assertRaises(RuntimeError):
            Store(path)


if __name__ == "__main__":
    unittest.main()
