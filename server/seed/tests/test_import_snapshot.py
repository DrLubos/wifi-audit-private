"""The SQL stream of import_snapshot.py (standard library only, no database):

  cd server && python3 -m unittest discover -s seed/tests

The fixture buffer is created by the collector's own store (wifi-sensor/
collector/store.py, imported read-only), so the importer is checked against
the buffer schema the sensor really writes.
"""

import argparse
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "wifi-sensor", "collector"))

import import_snapshot as imp  # noqa: E402
import store  # noqa: E402

AP = "4202770D00000000_A1B2C3D4E5F6"
CLIENT = "4202770D00000000_112233445566"


def make_buffer(path):
    """A small buffer as the current collector writes it: 3 polls, an AP and a
    client with observations, one history row, one alert."""
    s = store.Store(path)
    db = s.db
    db.execute("BEGIN")
    for i, ts in enumerate((1758981600, 1758981630, 1758981900)):
        db.execute("INSERT INTO polls (ts, kismet_ts, devices_total, devices_active, new_obs, "
                   "ds_running, ds_packets, ds_hop_n, ds_hop_visited, ds_hop_ok, duration_ms, ok) "
                   "VALUES (?, ?, 2, 2, 2, 1, 100, 89, 89, 1, 200, 1)", (ts, ts))
        db.execute("INSERT INTO observations (ts, key, last_time, freq_khz, channel, rssi, "
                   "rssi_floor, pk_total, pk_tx, pk_rx, pk_data, bytes, n_clients, disconnects, "
                   "qbss_stations, util_pct, bss_timestamp) "
                   "VALUES (?, ?, ?, 2412000, '1', ?, ?, 10, 0, 10, 0, 1000, 3, 0, 2, 12.5, 99)",
                   (ts, AP, ts, None if i == 1 else -60 - i, 1 if i == 1 else None))
        db.execute("INSERT INTO observations (ts, key, last_time, rssi, pk_total, pk_tx, pk_rx, "
                   "pk_data, bytes, bssid) VALUES (?, ?, ?, -70, 5, 5, 0, 5, 500, 'A1:B2:C3:D4:E5:F6')",
                   (ts, CLIENT, ts))
    db.execute("INSERT INTO devices (key, mac, type, first_seen, last_seen, ssid, cloaked, crypt, "
               "crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country) "
               "VALUES (?, 'A1:B2:C3:D4:E5:F6', 'ap', 1758981600, 1758981900, 'Net', 0, "
               "'WPA2 WPA2-PSK AES-CCMP', 274945016842, 1, 0, '1', 'HT20', 1024, 'SK')", (AP,))
    db.execute("INSERT INTO devices (key, mac, type, first_seen, last_seen) "
               "VALUES (?, '11:22:33:44:55:66', 'client', 1758981600, 1758981900)", (CLIENT,))
    db.execute("INSERT INTO device_config_history (ts, key, ssid, cloaked, crypt, crypt_bits, "
               "mfp_sup, mfp_req, adv_channel) VALUES (1758981630, ?, 'Net', 0, "
               "'WPA2 WPA2-PSK AES-CCMP', 274945016842, 1, 0, '6')", (AP,))
    db.execute("INSERT INTO alerts (hash, ts, header, raw) VALUES ('h1', 1758981700.5, "
               "'DEAUTHFLOOD', '{}')")
    db.execute("COMMIT")
    s.close()


def args(**kw):
    a = dict(snapshot="snap.db", sensor="pi-test", description=None, location=None, tz=None,
             no_baselines=False, no_rollups=False, min_obs=200, min_days=2)
    a.update(kw)
    return argparse.Namespace(**a)


class Stream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "snap.db")
        make_buffer(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def stream(self, **kw):
        db = imp.open_snapshot(self.path)
        out = io.StringIO()
        imp.emit_stream(out, db, args(**kw), lambda msg: None)
        db.close()
        return out.getvalue()

    def test_tracked_merges_report_inserted_rows_and_earliest_ts(self):
        sql = self.stream()
        self.assertEqual(sql.count("CREATE TEMP TABLE import_from"), 1)
        self.assertLess(sql.index("BEGIN;"), sql.index("CREATE TEMP TABLE import_from"))
        for t in ("polls", "observations", "alerts"):
            m = re.search(r"WITH ins AS \(\nINSERT INTO %s .*?\nRETURNING ts\)\n"
                          r"INSERT INTO import_from SELECT '%s', count\(\*\), min\(ts\) FROM ins;"
                          % (t, t), sql, re.S)
            self.assertIsNotNone(m, t)
            self.assertNotIn(";", m.group(0)[:-1], t)     # one statement
        for t in ("devices", "device_config_history", "device_freq_hist", "associations", "probes"):
            self.assertNotIn("INSERT INTO import_from SELECT '%s'" % t, sql)

    def test_refresh_rollups_after_commit_and_vacuum(self):
        sql = self.stream()
        commit = sql.index("COMMIT;")
        vacuum = sql.index("VACUUM (ANALYZE) observations;")
        refresh = sql.index("SELECT * FROM refresh_rollups(:sid, "
                            "coalesce((SELECT min(min_ts) FROM import_from), now()));")
        analyze = sql.index("ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, "
                            "ap_config_changes;")
        self.assertLess(commit, vacuum)
        self.assertLess(vacuum, refresh)
        self.assertLess(refresh, analyze)
        self.assertLess(sql.index("refresh_ap_baselines(:sid, 200, 2)"), commit)

    def test_no_rollups_and_no_baselines(self):
        sql = self.stream(no_rollups=True, no_baselines=True)
        self.assertNotIn("refresh_rollups", sql)
        self.assertNotIn("refresh_ap_baselines", sql)
        self.assertIn("VACUUM (ANALYZE) observations;", sql)

    def test_all_rows_are_staged(self):
        sql = self.stream()
        copied = re.findall(r"COPY stage_(\w+) FROM STDIN;\n(.*?)\\\.\n", sql, re.S)
        n = {t: len(body.splitlines()) for t, body in copied}
        self.assertEqual(n["polls"], 3)
        self.assertEqual(n["observations"], 6)
        self.assertEqual(n["devices"], 2)
        self.assertEqual(n["alerts"], 1)

    def test_buffer_version_check(self):
        db = sqlite3.connect(self.path)
        v = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        db.close()
        self.assertIn(v, imp.EXPECTED_SCHEMA_VERSIONS)


if __name__ == "__main__":
    unittest.main()
