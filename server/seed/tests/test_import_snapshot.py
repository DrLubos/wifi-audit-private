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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "wifi-sensor", "collector"))

import import_snapshot as imp  # noqa: E402
import store  # noqa: E402

AP = "4202770D00000000_A1B2C3D4E5F6"
CLIENT = "4202770D00000000_112233445566"


def make_buffer(path):
    """A small buffer of the collector's current version (v7): 3 polls, an AP
    and a client with observations, one history row, one alert, the AP's load
    on devices, client_bssids, ap_load_hourly. The observation rows are
    written like a v5 collector did (all columns, the client's BSSID and the
    AP load per poll) - a v6/v7 buffer still holds such rows until the
    retention removes them."""
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
    db.execute("UPDATE devices SET cur_n_clients = 3, cur_qbss_stations = 2, cur_util_pct = 12.5, "
               "cur_at = 1758981900 WHERE key = ?", (AP,))
    db.execute("INSERT INTO client_bssids (client_key, bssid, first_seen, last_seen) VALUES "
               "(?, 'A1:B2:C3:D4:E5:F6', 1758981000, 1758982000), "
               "(?, 'A1:B2:C3:D4:E5:F7', 1758981800, 1758981800)", (CLIENT, CLIENT))
    db.execute("INSERT INTO ap_load_hourly (key, hour, channel, polls, clients_polls, clients_max, "
               "clients_sum, qbss_polls, qbss_max, qbss_sum, util_max, util_sum, first_ts, last_ts) "
               "VALUES (?, 1758981600, '6', 2, 2, 4, 7, 1, 2, 2, 12.5, 12.5, 1758981630, 1758981900), "
               "(?, 1758981600, '', 1, 0, NULL, NULL, 0, NULL, NULL, NULL, NULL, 1758981600, 1758981600)",
               (AP, CLIENT))
    db.execute("COMMIT")
    s.close()


def make_v5(path):
    """The same buffer as a v5 collector left it: no devices.cur_*, no
    client_bssids, no ap_load_hourly."""
    make_buffer(path)
    db = sqlite3.connect(path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(devices)") if not r[1].startswith("cur_")]
    db.executescript("""
        UPDATE meta SET value = '5' WHERE key = 'schema_version';
        DROP TABLE client_bssids;
        DROP TABLE ap_load_hourly;
        CREATE TABLE d5 AS SELECT %s FROM devices;
        DROP TABLE devices;
        ALTER TABLE d5 RENAME TO devices;
    """ % ", ".join(cols))
    db.close()


def staged(sql):
    """{table: [row fields]} of the COPY blocks."""
    copied = re.findall(r"COPY stage_(\w+) \([^)]*\) FROM STDIN;\n(.*?)\\\.\n", sql, re.S)
    return {t: [line.split("\t") for line in body.splitlines()] for t, body in copied}


def copy_columns(sql):
    """{table: [columns]} of the COPY statements."""
    return {t: [c.strip() for c in cols.split(",")]
            for t, cols in re.findall(r"COPY stage_(\w+) \(([^)]*)\) FROM STDIN;", sql)}


def args(**kw):
    a = dict(snapshot="snap.db", sensor="pi-test", description=None, location=None, tz=None,
             no_baselines=False, no_rollups=False, min_obs=200, min_days=2, with_client_tables=False)
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

    def test_one_shared_merge_after_the_copies(self):
        # schema 14: the stream stages and calls ingest_merge() - the merge
        # rules live in schema.sql, shared with the live ingest
        sql = self.stream()
        begin, commit = sql.index("BEGIN;"), sql.index("COMMIT;")
        create = sql.index("SELECT ingest_stage_create();")
        merge = sql.index("SELECT ingest_merge(:sid, 'snapshot') AS merged \\gset")
        self.assertEqual(sql.count("SELECT ingest_merge("), 1)
        self.assertLess(begin, create)
        self.assertLess(create, sql.index("COPY stage_devices ("))
        self.assertLess(sql.rindex("\\.\n"), merge)          # after the last COPY block
        self.assertLess(merge, commit)
        for t in ("devices", "polls", "observations", "alerts", "ap_load_hourly", "device_freq_hist"):
            self.assertNotIn("INSERT INTO %s " % t, sql, t)
        self.assertNotIn("CREATE TEMP TABLE", sql)            # the stage tables are the server's
        self.assertNotIn("import_from", sql)

    def test_copy_lists_the_columns(self):
        cols = copy_columns(self.stream())
        self.assertEqual(cols["polls"][:2], ["seq", "ts"])
        self.assertEqual(cols["observations"][:2], ["ts", "device_key"])

    def test_poll_seq_is_the_buffer_row_id(self):
        rows = staged(self.stream())["polls"]
        self.assertEqual([r[:2] for r in rows], [["1", "1758981600"], ["2", "1758981630"], ["3", "1758981900"]])

    def test_client_devices_only_with_the_flag(self):
        # the recovery path loads what the live ingest accepts: AP devices
        # (and their frequency histograms); clients only with the client tables
        rows = staged(self.stream())
        self.assertEqual([r[0] for r in rows["devices"]], [AP])
        rows = staged(self.stream(with_client_tables=True))
        self.assertEqual(sorted(r[0] for r in rows["devices"]), sorted([AP, CLIENT]))

    def test_client_tables_only_with_the_flag(self):
        # D4: the snapshot import is a recovery tool; client data stays on the Pi
        rows = staged(self.stream())
        for t in ("associations", "client_bssids", "probes"):
            self.assertNotIn(t, rows, t)
        rows = staged(self.stream(with_client_tables=True))
        self.assertIn("client_bssids", rows)
        self.assertIn("probes", rows)
        self.assertIn("associations", rows)

    def test_refresh_rollups_after_commit_and_vacuum(self):
        sql = self.stream()
        commit = sql.index("COMMIT;")
        vacuum = sql.index("VACUUM (ANALYZE) observations;")
        refresh = sql.index("SELECT * FROM refresh_rollups(:sid, "
                            "coalesce((:'merged'::jsonb ->> 'min_ts')::timestamptz, now()));")
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

    def test_rows_are_staged(self):
        rows = staged(self.stream())
        self.assertEqual(len(rows["polls"]), 3)
        self.assertEqual(len(rows["devices"]), 1)        # the AP; the client stays on the Pi
        self.assertEqual(len(rows["alerts"]), 1)

    def test_only_ap_observations_with_the_kept_columns(self):
        sql = self.stream()
        obs = staged(sql)["observations"]
        # ts, key, rssi, rssi_floor, pk_total, pk_data, disconnects, disconnects_last, bss_timestamp
        self.assertEqual(obs, [["1758981600", AP, "-60", "\\N", "10", "0", "0", "\\N", "99"],
                               ["1758981630", AP, "\\N", "1", "10", "0", "0", "\\N", "99"],
                               ["1758981900", AP, "-62", "\\N", "10", "0", "0", "\\N", "99"]])
        for gone in ("last_time", "freq_khz", "rssi_min", "pk_tx", "bytes", "n_clients",
                     "ie_checksum", "beacon_fp"):
            self.assertNotIn(gone, copy_columns(sql)["observations"], gone)

    def test_observations_of_an_ap_that_kismet_re_typed(self):
        # schema 10: a device with an advertised configuration has been an AP
        # (it beaconed) - its rows come along although the snapshot types it
        # client; the plain client's rows stay on the Pi
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.db")
            make_buffer(path)
            retyped = "4202770D00000000_A1B2C3D4E5F7"
            db = sqlite3.connect(path)
            db.execute("INSERT INTO devices (key, mac, type, first_seen, last_seen, ssid, cloaked, adv_channel) "
                       "VALUES (?, 'A1:B2:C3:D4:E5:F7', 'client', 1758981600, 1758981900, '', 1, '6')", (retyped,))
            db.execute("INSERT INTO observations (ts, key, last_time, rssi, pk_total, pk_data) "
                       "VALUES (1758981630, ?, 1758981630, -75, 3, 0)", (retyped,))
            db.commit()
            db.close()
            snap = imp.open_snapshot(path)
            out = io.StringIO()
            imp.emit_stream(out, snap, args(), lambda msg: None)
            snap.close()
        keys = sorted({r[1] for r in staged(out.getvalue())["observations"]})
        self.assertEqual(keys, [AP, retyped])

    def test_client_bssids_from_the_table_and_the_old_rows(self):
        rows = staged(self.stream(with_client_tables=True))["client_bssids"]
        # F6: per-poll rows 1758981600..1758981900 merged with the table's wider pair
        self.assertEqual(rows, [[CLIENT, "A1:B2:C3:D4:E5:F6", "1758981000", "1758982000"],
                                [CLIENT, "A1:B2:C3:D4:E5:F7", "1758981800", "1758981800"]])

    def test_ap_load_is_staged_with_the_device(self):
        dev = {r[0]: r for r in staged(self.stream(with_client_tables=True))["devices"]}
        self.assertEqual(dev[AP][-4:], ["3", "2", "12.5", "1758981900"])
        self.assertEqual(dev[CLIENT][-4:], ["\\N"] * 4)

    def test_ap_load_hourly_rows_merge_fuller_wins(self):
        sql = self.stream()
        rows = staged(sql)["ap_load_hourly"]
        self.assertEqual([r for r in rows if r[0] == AP],
                         [[AP, "1758981600", "6", "2", "2", "4", "7", "1", "2", "2", "12.5", "12.5"]])
        self.assertEqual(len(rows), 2)        # the client's row is staged, the merge drops it

    def test_v5_load_columns_become_load_samples(self):
        sql = self.stream()
        def iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
        rows = sorted(staged(sql)["load_samples"])
        # AP rows only (a client never carries the columns), ts as timestamptz text
        self.assertEqual(rows, [[iso(ts), AP, "3", "2", "12.5"]
                                for ts in (1758981600, 1758981630, 1758981900)])
        # merged by ingest_merge() (load_ap_load_samples(), after the history)
        self.assertLess(sql.index("COPY stage_load_samples ("), sql.index("SELECT ingest_merge("))

    def test_upload_is_recorded_in_the_transaction(self):
        sql = self.stream()
        rec = sql.split("INSERT INTO sensor_uploads")[1].split(";")[0]
        self.assertIn("SELECT :sid, 'snapshot', '7', to_timestamp(1758981600), to_timestamp(1758981900), "
                      "NULL,", rec)                       # no export meta in this fixture
        self.assertIn("coalesce(:'merged'::jsonb -> 'inserted', '{}'::jsonb)", rec)
        self.assertLess(sql.index("INSERT INTO sensor_uploads"), sql.index("COMMIT;"))
        self.assertLess(sql.index("SELECT ingest_merge("), sql.index("INSERT INTO sensor_uploads"))

    def test_buffer_size_from_the_export_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.db")
            make_buffer(path)
            db = sqlite3.connect(path)
            db.execute("INSERT INTO meta VALUES ('snapshot_source_bytes', '574000000')")
            db.commit()
            db.close()
            db = imp.open_snapshot(path)
            out = io.StringIO()
            imp.emit_stream(out, db, args(), lambda msg: None)
            db.close()
        self.assertIn("to_timestamp(1758981900), 574000000,", out.getvalue())

    def test_buffer_version_check(self):
        db = sqlite3.connect(self.path)
        v = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        db.close()
        self.assertIn(v, imp.EXPECTED_SCHEMA_VERSIONS)


class StreamV5(unittest.TestCase):
    """A v5 snapshot: accepted; no AP load; client BSSIDs from observations.bssid."""

    def test_v5_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v5.db")
            make_v5(path)
            db = imp.open_snapshot(path)
            out = io.StringIO()
            imp.emit_stream(out, db, args(), lambda msg: None)
            db.close()
        rows = staged(out.getvalue())
        self.assertEqual(len(rows["observations"]), 3)
        self.assertNotIn("ap_load_hourly", rows)          # no such table before v7
        self.assertEqual(len(rows["load_samples"]), 3)
        self.assertNotIn("client_bssids", rows)
        self.assertEqual({tuple(r[-4:]) for r in rows["devices"]}, {("\\N",) * 4})


if __name__ == "__main__":
    unittest.main()
