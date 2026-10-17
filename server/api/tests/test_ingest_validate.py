"""The ingest payload (app/ingest/validate.py, docs/live-upload.md 5). Standard library only."""

import gzip
import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ingest import validate as v  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp()
T = int(NOW) - 600
AP = "4202770D00000000_A1B2C3D4E5F6"
BUFFER = "5b2c3a4e-0d1f-4e8a-9a51-1c2d3e4f5a6b"


def stage_tables():
    """{table: [columns]} of ingest_stage_create() in schema.sql."""
    schema = open(os.path.join(HERE, "..", "..", "schema.sql"), encoding="utf-8").read()
    fn = re.search(r"CREATE OR REPLACE FUNCTION ingest_stage_create\(.*?\n\$\$;", schema, re.S).group(0)
    return {name: [c.strip().split()[0] for c in body.split(",") if c.strip()]
            for name, body in re.findall(r"CREATE TEMP TABLE IF NOT EXISTS stage_(\w+) \((.*?)\) ON COMMIT", fn, re.S)}


def device(**kw):
    row = dict(device_key=AP, mac="A1:B2:C3:D4:E5:F6", type="ap", manuf="Acme", first_seen=T - 3600,
               last_seen=T, ssid="Net", cloaked=0, crypt="WPA2", crypt_bits=274945016842, mfp_sup=1,
               mfp_req=0, adv_channel="6", ht_mode="HT20", beacon_rate=1024, country="SK",
               config_changed_at=None, cur_n_clients=3, cur_qbss_stations=None, cur_util_pct=None, cur_at=T)
    row.update(kw)
    return row


def poll(**kw):
    row = dict(seq=101, ts=T, ts_offset_s=None, kismet_ts=T, devices_total=900, devices_active=70,
               new_obs=60, ds_running=1, ds_error=None, ds_packets=123456, ds_hop_n=89, ds_hop_visited=89,
               ds_hop_ok=1, duration_ms=800, ok=1, error=None)
    row.update(kw)
    return row


def obs(**kw):
    row = dict(ts=T, device_key=AP, rssi=-60, rssi_floor=None, pk_total=10, pk_data=2, disconnects=None,
               disconnects_last=None, bss_timestamp=123)
    row.update(kw)
    return row


def table(name, rows):
    cols = v.columns(name)
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


def batch(**tables):
    body = {"v": 1, "batch_seq": 5, "buffer_id": BUFFER, "buffer_version": "8", "sent_at": NOW - 1,
            "attempt": 1, "heartbeat": {"sensor_ts": NOW - 1, "boot_id": BUFFER, "backlog_polls": 0},
            "tables": {}}
    if not tables:
        tables = {"devices": [device()], "polls": [poll()], "observations": [obs()]}
    for name, rows in tables.items():
        body["tables"][name] = table(name, rows)
    return body


class Columns(unittest.TestCase):
    def test_columns_are_the_staging_tables(self):
        # the ingest COPYs exactly what ingest_stage_create() makes
        stages = stage_tables()
        for name in v.TABLES:
            self.assertEqual(v.columns(name), stages[name], name)

    def test_no_client_tables(self):
        for name in ("associations", "client_bssids", "probes"):
            self.assertNotIn(name, v.TABLES)


class Body(unittest.TestCase):
    def test_gzip_json(self):
        raw = gzip.compress(json.dumps({"a": 1}).encode())
        self.assertEqual(v.decode_body(raw, "gzip"), {"a": 1})
        self.assertEqual(v.decode_body(b'{"a": 1}', None), {"a": 1})

    def test_too_large(self):
        with self.assertRaises(v.TooLarge):
            v.decode_body(b"x" * (v.MAX_BODY + 1), None)
        bomb = gzip.compress(b"[" + b"0," * (v.MAX_JSON // 2) + b"0]")     # small compressed, huge inflated
        self.assertLess(len(bomb), v.MAX_BODY)
        with self.assertRaises(v.TooLarge):
            v.decode_body(bomb, "gzip")

    def test_bad_request(self):
        for raw, enc in ((b"not gzip", "gzip"), (b"{nope", None), (b"[1, 2]", None), (b"{}", "br")):
            with self.assertRaises(v.BadRequest, msg=repr(raw)):
                v.decode_body(raw, enc)


class Batch(unittest.TestCase):
    def test_a_valid_batch(self):
        b = v.batch(batch(), NOW)
        self.assertEqual(b["batch_seq"], 5)
        self.assertEqual(b["buffer_id"], BUFFER)
        self.assertEqual([r[0] for r in b["tables"]["devices"]], [AP])
        self.assertEqual(b["polls_from"], T)
        self.assertEqual(b["polls_to"], T)
        self.assertEqual(b["rows"], 3)
        self.assertEqual(b["heartbeat"]["backlog_polls"], 0)

    def invalid(self, body, fragment):
        with self.assertRaises(v.Invalid) as cm:
            v.batch(body, NOW)
        self.assertIn(fragment, str(cm.exception))

    def test_envelope(self):
        for key, value, fragment in (("v", 2, "version"), ("batch_seq", -1, "batch_seq"),
                                     ("buffer_id", "nope", "buffer_id"), ("attempt", 0, "attempt"),
                                     ("tables", [], "tables")):
            body = batch()
            body[key] = value
            self.invalid(body, fragment)

    def test_columns_must_match(self):
        body = batch()
        body["tables"]["polls"]["cols"] = body["tables"]["polls"]["cols"][::-1]
        self.invalid(body, "polls: columns")
        body = batch()
        body["tables"]["associations"] = {"cols": ["ap_key"], "rows": []}
        self.invalid(body, "associations")

    def test_row_length(self):
        body = batch()
        body["tables"]["polls"]["rows"][0].pop()
        self.invalid(body, "polls row 1")

    def test_client_devices_are_refused(self):
        # privacy: a sensor never uploads a client device (ap_evidence(): typed ap,
        # or an advertised configuration)
        self.invalid(batch(devices=[device(type="client", ssid=None, crypt=None, adv_channel=None)]),
                     "not an AP")
        v.batch(batch(devices=[device(type="client")]), NOW)       # re-typed AP with a configuration: fine

    def test_types_and_nulls(self):
        self.invalid(batch(polls=[poll(ok=None)]), "ok")                 # required
        self.invalid(batch(polls=[poll(ok=2)]), "ok")                    # a flag
        self.invalid(batch(observations=[obs(rssi=-40000)]), "rssi")     # smallint
        self.invalid(batch(observations=[obs(pk_total=1.5)]), "pk_total")
        self.invalid(batch(observations=[obs(pk_total=True)]), "pk_total")
        self.invalid(batch(devices=[device(mac="A1:B2:C3:D4:E5")]), "mac")
        self.invalid(batch(devices=[device(ssid="a\x00b")]), "ssid")
        self.invalid(batch(devices=[device(ssid="x" * 1000)]), "ssid")
        self.invalid(batch(polls=[poll(ts_offset_s=float("nan"))]), "ts_offset_s")

    def test_times(self):
        self.invalid(batch(polls=[poll(ts=int(NOW) + 301)]), "in the future")
        self.invalid(batch(polls=[poll(ts=1700000000)]), "before")
        v.batch(batch(polls=[poll(ts=int(NOW) + 299)]), NOW)
        self.invalid(batch(observations=[obs(disconnects_last=int(NOW) + 3600)]), "disconnects_last")

    def test_duplicate_keys(self):
        self.invalid(batch(polls=[poll(), poll(seq=102)]), "duplicate")
        self.invalid(batch(observations=[obs(), obs()]), "duplicate")
        v.batch(batch(observations=[obs(), obs(ts=T + 30)]), NOW)

    def test_caps(self):
        many = [poll(seq=i, ts=T - 30 * i) for i in range(1, v.MAX_POLLS + 2)]
        self.invalid(batch(polls=many), "polls")

    def test_load_samples_become_datetimes(self):
        b = v.batch(batch(load_samples=[{"ts": T, "device_key": AP, "n_clients": 3, "qbss_stations": None,
                                         "util_pct": None}]), NOW)
        self.assertEqual(b["tables"]["load_samples"][0][0], datetime.fromtimestamp(T, timezone.utc))

    def test_alert_raw_must_be_json(self):
        alert = dict(hash="h1", ts=T + 0.5, header="DEAUTHFLOOD", **{"class": "DOT11"}, severity=10,
                     source_mac=None, dest_mac=None, transmitter_mac="A1:B2:C3:D4:E5:F6", other_mac=None,
                     channel="6", freq_khz=2437000, device_key=AP, text="x", raw='{"a": 1}')
        v.batch(batch(alerts=[alert]), NOW)
        self.invalid(batch(alerts=[dict(alert, raw="{nope")]), "raw")

    def test_gaps_and_boots(self):
        body = batch()
        body["gaps"] = [{"seq_from": 10, "seq_to": 20, "reason": "lost"},
                        {"seq_from": 30, "seq_to": 31, "reason": "unverified", "raw_from": 1757944356.0,
                         "raw_to": 1757944386.0}]
        body["boots"] = [{"boot_id": BUFFER, "source": "ntp", "epoch_true": 1760000000.25,
                          "epoch_stale": 1757944356.0, "step_mono": 812.4, "polls_corrected": 27,
                          "offset_min_s": 2487000.0, "offset_max_s": 2487003.0}]
        b = v.batch(body, NOW)
        self.assertEqual([(g["seq_from"], g["polls"]) for g in b["gaps"]], [(10, 11), (30, 2)])
        self.assertEqual(b["boots"][0]["source"], "ntp")
        for bad in ({"seq_from": 5, "seq_to": 4, "reason": "lost"}, {"seq_from": 5, "seq_to": 6, "reason": "x"}):
            body["gaps"] = [bad]
            self.invalid(body, "gaps")
        body["gaps"] = []
        body["boots"] = [{"boot_id": BUFFER, "source": "gps", "epoch_true": 1.0}]
        self.invalid(body, "boots")


class Heartbeat(unittest.TestCase):
    def test_known_fields_kept_unknown_dropped(self):
        hb = v.heartbeat({"sensor_ts": NOW, "backlog_polls": 3, "chrony_synced": True, "future_field": 1,
                          "adapter": {"usb_id": "0bda:c811", "mac": "00:11:22:33:44:55", "driver": "rtw88_8821cu",
                                      "chipset": "RTL8821CU", "interface": "wlan1", "extra": "x"}})
        self.assertEqual(hb["backlog_polls"], 3)
        self.assertNotIn("future_field", hb)
        self.assertEqual(hb["adapter"], {"usb_id": "0bda:c811", "mac": "00:11:22:33:44:55",
                                         "driver": "rtw88_8821cu", "chipset": "RTL8821CU", "interface": "wlan1"})

    def test_adapter_absent_and_invalid(self):
        self.assertIsNone(v.heartbeat({"sensor_ts": NOW, "adapter": None})["adapter"])
        for bad in ({"usb_id": "0BDA:C811"}, {"mac": "nope"}):
            with self.assertRaises(v.Invalid):
                v.heartbeat({"sensor_ts": NOW, "adapter": bad})

    def test_sensor_ts_required(self):
        with self.assertRaises(v.Invalid):
            v.heartbeat({"backlog_polls": 1})
        with self.assertRaises(v.Invalid):
            v.heartbeat({"sensor_ts": "now"})


if __name__ == "__main__":
    unittest.main()
