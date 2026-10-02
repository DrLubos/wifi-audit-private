"""Tests for shape.py against records mirroring real Kismet 2025.09.0 output.

The fixtures reproduce the exact structure of a field-filtered POST response
(including the 0-for-missing quirk) with made-up MAC addresses and SSIDs.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shape import DEVICE_FIELDS, shape_alert, shape_device  # noqa: E402

TS = 1_789_481_150

# An AP with clients, exactly as Kismet returns it for DEVICE_FIELDS.
AP_RAW = {
    "kismet.device.base.key": "4202770D00000000_0102030A0B02",
    "kismet.device.base.macaddr": "02:0B:0A:03:02:01",
    "kismet.device.base.type": "Wi-Fi AP",
    "kismet.device.base.manuf": "Example Corp",
    "kismet.device.base.channel": "8",
    "kismet.device.base.frequency": 2447000,
    "kismet.device.base.first_time": 1789480390,
    "kismet.device.base.last_time": 1789481148,
    "kismet.device.base.packets.total": 99,
    "kismet.device.base.packets.tx_total": 99,
    "kismet.device.base.packets.rx_total": 0,
    "kismet.device.base.packets.data": 0,
    "kismet.device.base.datasize": 0,
    "kismet.device.base.num_alerts": 0,
    "kismet.device.base.freq_khz_map": {"2447000": 90, "2412000": 9},
    "sig_last": -54, "sig_min": -72, "sig_max": -49,
    "last_bssid": "02:0B:0A:03:02:01",
    "n_clients": 2, "disconnects": 3, "disconnects_last": 1789481102,
    "beacon_fp": 563282674, "bss_ts": 1628729078212,
    "clients": {"02:AA:00:00:00:01": "4202770D00000000_0100000000AA02",
                "02:AA:00:00:00:02": "4202770D00000000_0200000000AA02"},
    "probed": 0,
    "ssid": "Example-Net", "cloaked": 0,
    "crypt": "WPA2 WPA2-PSK AES-CCMP", "crypt_bits": 274945016842,
    "mfp_sup": 1, "mfp_req": 0,
    "adv_ch": "8", "ht": "HT20", "beacon_rate": 10, "country": "SK",
    "ie_sum": 1851240727, "qbss_stations": 1, "util_pct": 2.352941,
}

# A client: every AP-only field is the integer 0, not null.
CLIENT_RAW = {
    "kismet.device.base.key": "4202770D00000000_0200000000AA02",
    "kismet.device.base.macaddr": "02:AA:00:00:00:02",
    "kismet.device.base.type": "Wi-Fi Client",
    "kismet.device.base.manuf": "Unknown",
    "kismet.device.base.channel": "48",
    "kismet.device.base.frequency": 2457000,
    "kismet.device.base.first_time": 1789480391,
    "kismet.device.base.last_time": 1789481154,
    "kismet.device.base.packets.total": 25,
    "kismet.device.base.packets.tx_total": 11,
    "kismet.device.base.packets.rx_total": 14,
    "kismet.device.base.packets.data": 0,
    "kismet.device.base.datasize": 0,
    "kismet.device.base.num_alerts": 0,
    "kismet.device.base.freq_khz_map": {"2457000": 22, "5180000": 1},
    "sig_last": -76, "sig_min": -78, "sig_max": -68,
    "last_bssid": "02:0B:0A:03:02:01",
    "n_clients": 0, "disconnects": 0, "disconnects_last": 0, "beacon_fp": 0, "bss_ts": 0,
    "clients": 0,
    "probed": [
        {"dot11.probedssid.ssid": "", "dot11.probedssid.ssidlen": 0,
         "dot11.probedssid.bssid": "00:00:00:00:00:00",
         "dot11.probedssid.first_time": 1789480391, "dot11.probedssid.last_time": 1789480571,
         "dot11.probedssid.crypt_bitfield": 0, "dot11.probedssid.crypt_set": 0,
         "dot11.probedssid.crypt_string": "Open",
         "dot11.probedssid.wpa_mfp_required": 0, "dot11.probedssid.wpa_mfp_supported": 0},
        {"dot11.probedssid.ssid": "Example-Net", "dot11.probedssid.ssidlen": 11,
         "dot11.probedssid.bssid": "00:00:00:00:00:00",
         "dot11.probedssid.first_time": 1789480400, "dot11.probedssid.last_time": 1789481100,
         "dot11.probedssid.crypt_bitfield": 0, "dot11.probedssid.crypt_set": 0,
         "dot11.probedssid.crypt_string": "Open",
         "dot11.probedssid.wpa_mfp_required": 0, "dot11.probedssid.wpa_mfp_supported": 0},
    ],
    "ssid": 0, "cloaked": 0, "crypt": 0, "crypt_bits": 0, "mfp_sup": 0, "mfp_req": 0,
    "adv_ch": 0, "ht": 0, "beacon_rate": 0, "country": 0, "ie_sum": 0,
    "qbss_stations": 0, "util_pct": 0,
}


class DeviceFieldsTest(unittest.TestCase):
    def test_no_pii_fields_requested(self):
        paths = [f[0] if isinstance(f, list) else f for f in DEVICE_FIELDS]
        for p in paths:
            self.assertNotIn("ipdata", p)
            self.assertNotIn("wps_", p)
            self.assertNotIn("seenby", p)
            self.assertNotIn("client_map", p.replace("associated_client_map", ""))

    def test_aliases_are_unique(self):
        aliases = [f[1] for f in DEVICE_FIELDS if isinstance(f, list)]
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_v6_requests_only_stored_fields(self):
        paths = [f[0] if isinstance(f, list) else f for f in DEVICE_FIELDS]
        for gone in ("kismet.device.base.channel", "kismet.device.base.frequency",
                     "kismet.device.base.packets.tx_total", "kismet.device.base.packets.rx_total",
                     "kismet.device.base.datasize", "min_signal", "max_signal",
                     "beacon_fingerprint", "ietag_checksum"):
            self.assertFalse([p for p in paths if p.endswith(gone)], gone)
        for kept in ("kismet.device.base.last_time", "num_associated_clients", "last_bssid",
                     "dot11e_qbss_stations", "dot11e_channel_utilization_perc", "freq_khz_map"):
            self.assertTrue([p for p in paths if p.endswith(kept)], kept)


class ShapeApTest(unittest.TestCase):
    def setUp(self):
        self.rec = shape_device(AP_RAW, TS)

    def test_flat_fields(self):
        r = self.rec
        self.assertEqual(r["ts"], TS)
        self.assertEqual(r["key"], AP_RAW["kismet.device.base.key"])
        self.assertEqual(r["type"], "ap")
        self.assertEqual(r["last"], 1789481148)
        self.assertEqual(r["rssi"], -54)
        self.assertEqual((r["pk"], r["data"]), (99, 0))
        self.assertEqual(r["freqs"], {2447000: 90, 2412000: 9})

    def test_fields_dropped_in_v6_are_not_in_the_record(self):
        for k in ("freq", "ch", "rssi_min", "rssi_max", "tx", "rx", "bytes"):
            self.assertNotIn(k, self.rec)
        for k in ("ie_sum", "beacon_fp"):
            self.assertNotIn(k, self.rec["ap"])

    def test_ap_block(self):
        ap = self.rec["ap"]
        self.assertEqual(ap["ssid"], "Example-Net")
        self.assertEqual(ap["crypt"], "WPA2 WPA2-PSK AES-CCMP")
        self.assertEqual(ap["crypt_bits"], 274945016842)
        self.assertEqual(ap["mfp"], [1, 0])
        self.assertEqual(ap["ht"], "HT20")
        self.assertEqual(ap["beacon_rate"], 10)
        self.assertEqual(ap["country"], "SK")
        self.assertEqual(ap["bss_ts"], 1628729078212)
        self.assertEqual(ap["qbss_stations"], 1)
        self.assertAlmostEqual(ap["util_pct"], 2.352941)
        self.assertEqual(ap["n_clients"], 2)
        self.assertEqual(ap["disconnects"], 3)
        self.assertEqual(ap["disconnects_last"], 1789481102)
        self.assertEqual(ap["clients"], ["02:AA:00:00:00:01", "02:AA:00:00:00:02"])

    def test_ap_has_no_client_block(self):
        # last_bssid of an AP is its own MAC - not an association.
        self.assertNotIn("cl", self.rec)

    def test_ap_zero_counters_are_real_zeros(self):
        raw = dict(AP_RAW, disconnects=0, n_clients=0, qbss_stations=0, clients={})
        ap = shape_device(raw, TS)["ap"]
        self.assertEqual(ap["disconnects"], 0)
        self.assertEqual(ap["n_clients"], 0)
        self.assertEqual(ap["qbss_stations"], 0)
        self.assertEqual(ap["clients"], [])

    def test_ap_never_deauthed_has_no_last_disconnect(self):
        # A timestamp of 0 means "no deauth/disassoc frame yet", not epoch.
        raw = dict(AP_RAW, disconnects=0, disconnects_last=0)
        self.assertIsNone(shape_device(raw, TS)["ap"]["disconnects_last"])

    def test_cloaked_ssid_kept_as_empty_string(self):
        raw = dict(AP_RAW, ssid="", cloaked=1)
        ap = shape_device(raw, TS)["ap"]
        self.assertEqual(ap["ssid"], "")
        self.assertEqual(ap["cloaked"], 1)


class ShapeClientTest(unittest.TestCase):
    def setUp(self):
        self.rec = shape_device(CLIENT_RAW, TS)

    def test_zero_sentinel_does_not_create_ap_block(self):
        self.assertEqual(self.rec["type"], "client")
        self.assertNotIn("ap", self.rec)

    def test_client_block(self):
        cl = self.rec["cl"]
        self.assertEqual(cl["bssid"], "02:0B:0A:03:02:01")
        self.assertEqual(cl["probes"], [["", 1789480391, 1789480571],
                                        ["Example-Net", 1789480400, 1789481100]])

    def test_signal_zero_is_unknown(self):
        raw = dict(CLIENT_RAW, sig_last=0)
        r = shape_device(raw, TS)
        self.assertIsNone(r["rssi"])
        self.assertIsNone(r["rssi_floor"])

    def test_signal_floor_is_censored(self):
        for v in (-106, -120):
            r = shape_device(dict(CLIENT_RAW, sig_last=v), TS)
            self.assertIsNone(r["rssi"])
            self.assertEqual(r["rssi_floor"], 1)

    def test_values_next_to_the_floor_are_levels(self):
        for v in (-105, -107, -104):
            r = shape_device(dict(CLIENT_RAW, sig_last=v), TS)
            self.assertEqual((r["rssi"], r["rssi_floor"]), (v, None))

    def test_signal_object_missing(self):
        raw = {k: v for k, v in CLIENT_RAW.items() if not k.startswith("sig_")}
        r = shape_device(raw, TS)
        self.assertIsNone(r["rssi"])

    def test_null_bssid_and_no_probes_gives_no_client_block(self):
        raw = dict(CLIENT_RAW, last_bssid="00:00:00:00:00:00", probed=0)
        self.assertNotIn("cl", shape_device(raw, TS))

    def test_bridged_type(self):
        raw = dict(CLIENT_RAW)
        raw["kismet.device.base.type"] = "Wi-Fi Bridged"
        self.assertEqual(shape_device(raw, TS)["type"], "bridged")

    def test_missing_key_is_rejected(self):
        raw = dict(CLIENT_RAW)
        raw["kismet.device.base.key"] = 0
        with self.assertRaises(ValueError):
            shape_device(raw, TS)


# An alert, field names taken from the Kismet binary (none had fired live).
ALERT_RAW = {
    "kismet.alert.timestamp": 1789481200.25,
    "kismet.alert.header": "DEAUTHFLOOD",
    "kismet.alert.class": "DENIAL",
    "kismet.alert.severity": 10,
    "kismet.alert.phy_id": 0,
    "kismet.alert.source_mac": "02:0B:0A:03:02:01",
    "kismet.alert.dest_mac": "FF:FF:FF:FF:FF:FF",
    "kismet.alert.transmitter_mac": "02:0B:0A:03:02:01",
    "kismet.alert.other_mac": "00:00:00:00:00:00",
    "kismet.alert.channel": "6",
    "kismet.alert.frequency": 2437000,
    "kismet.alert.location": {},
    "kismet.alert.text": "Deauth flood",
    "kismet.alert.hash": 123456789,
    "kismet.alert.device_key": "4202770D00000000_0102030A0B02",
}


class ShapeAlertTest(unittest.TestCase):
    def test_shape(self):
        a = shape_alert(ALERT_RAW)
        self.assertEqual(a["hash"], "123456789")
        self.assertEqual(a["ts"], 1789481200.25)
        self.assertEqual(a["header"], "DEAUTHFLOOD")
        self.assertEqual(a["severity"], 10)
        self.assertEqual(a["source_mac"], "02:0B:0A:03:02:01")
        self.assertIsNone(a["other_mac"])
        self.assertEqual(a["channel"], "6")
        self.assertEqual(a["freq"], 2437000)
        self.assertIn('"kismet.alert.header":"DEAUTHFLOOD"', a["raw"])

    def test_hash_fallback_is_deterministic(self):
        raw = dict(ALERT_RAW, **{"kismet.alert.hash": 0})
        self.assertEqual(shape_alert(raw)["hash"], shape_alert(raw)["hash"])
        self.assertTrue(shape_alert(raw)["hash"].startswith("sha1:"))

    def test_numeric_channel(self):
        raw = dict(ALERT_RAW, **{"kismet.alert.channel": 11})
        self.assertEqual(shape_alert(raw)["channel"], "11")


if __name__ == "__main__":
    unittest.main()
