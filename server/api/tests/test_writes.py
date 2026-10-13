"""Validation of the write bodies, the channel plans and the hop-stride rule
(app/v2/writes.py). Standard library only."""

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import writes as w  # noqa: E402

INSTALLER = os.path.join(os.path.dirname(__file__), "..", "..", "..", "wifi-sensor", "install_sensor.sh")
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


class Labels(unittest.TestCase):
    def test_three_outcomes_each_with_its_reason(self):
        self.assertEqual(w.parse_status({"status": "benign", "reason": "expected_behaviour", "note": " open net "}),
                         ("benign", "expected_behaviour", "open net"))
        self.assertEqual(w.parse_status({"status": "confirmed", "reason": "staged_test"}),
                         ("confirmed", "staged_test", None))
        self.assertEqual(w.parse_status({"status": "false_positive", "reason": "artefact"}),
                         ("false_positive", "artefact", None))
        self.assertEqual(w.parse_status({"status": "open"}), ("open", None, None))
        for bad in ({"status": "confirmed"}, {"status": "benign"}, {"status": "false_positive", "reason": None},
                    {"status": "confirmed", "reason": "expected_behaviour"}, {"status": "benign", "reason": "rule_defect"},
                    {"status": "false_positive", "reason": "staged_test"}, {"status": "open", "reason": "artefact"},
                    {"status": "closed"}, {"status": "benign", "reason": "accepted_risk", "note": "x" * 1001}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_status(bad)

    def test_ids(self):
        self.assertEqual(w.parse_ids({"ids": [3, 1, 3]}), [1, 3])
        for bad in ({}, {"ids": []}, {"ids": [0]}, {"ids": [True]}, {"ids": ["1"]}, {"ids": list(range(1, 502))}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_ids(bad)


class Aps(unittest.TestCase):
    def test_classification_and_note(self):
        self.assertEqual(w.parse_ap_classification({"classification": "managed"}), "managed")
        self.assertIsNone(w.parse_ap_classification({"classification": None}))     # back to the rules
        for bad in ({}, {"classification": "evil"}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_ap_classification(bad)
        self.assertEqual(w.parse_ap_note({"note": " lab AP "}), "lab AP")
        self.assertIsNone(w.parse_ap_note({"note": ""}))
        with self.assertRaises(w.Invalid):
            w.parse_ap_note({})


class Networks(unittest.TestCase):
    def test_network(self):
        n = w.parse_network({"ssid": "IK-WIFI", "kind": "managed", "expected_security": "open_allowed",
                             "allowed_bssids": ["ec-58-ea-55-88-fc", "EC:58:EA:55:88:FC"], "allowed_ouis": ["ec58ea", "3C:46:A1"]})
        self.assertEqual(n["allowed_bssids"], ["EC:58:EA:55:88:FC"])
        self.assertEqual(n["allowed_ouis"], ["3C:46:A1", "EC:58:EA"])
        self.assertFalse(n["require_mfp"])
        # a friendly network is not checked
        f = w.parse_network({"ssid": "Neighbour", "kind": "friendly", "expected_security": "wpa3", "require_mfp": True})
        self.assertEqual((f["expected_security"], f["require_mfp"]), (None, False))
        for bad in ({"ssid": "", "kind": "managed"}, {"ssid": "x" * 33, "kind": "managed"},
                    {"ssid": "a", "kind": "own"}, {"ssid": "a", "kind": "managed", "expected_security": "wep"},
                    {"ssid": "a", "kind": "managed", "allowed_ouis": ["EC:58"]},
                    {"ssid": "a", "kind": "managed", "allowed_bssids": ["EC:58:EA"]},
                    {"ssid": "a", "kind": "managed", "require_mfp": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_network(bad)

    def test_list_and_allowed(self):
        rows = w.parse_networks({"networks": [{"id": 1, "ssid": "A", "kind": "managed"}, {"ssid": "B", "kind": "friendly"}]})
        self.assertEqual([(r["id"], r["ssid"]) for r in rows], [(1, "A"), (None, "B")])
        with self.assertRaises(w.Invalid):
            w.parse_networks({"networks": [{"ssid": "A", "kind": "managed"}, {"ssid": "A", "kind": "friendly"}]})
        self.assertEqual(w.parse_allowed({"bssid": "f0:6f:ce:41:1f:b0"}), ("bssid", "F0:6F:CE:41:1F:B0"))
        self.assertEqual(w.parse_allowed({"oui": "F0-6F-CE"}), ("oui", "F0:6F:CE"))
        for bad in ({}, {"bssid": "x", "oui": "y"}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_allowed(bad)


class AccountSettings(unittest.TestCase):
    def test_classification(self):
        c = w.parse_classification({"ignore_random_bssid": True, "suspicious_unallowed": True, "managed_by_vendor": False,
                                    "ignore_weaker_dbm": None, "mobile_vendors": ["Apple", " Samsung", "Apple"]})
        self.assertEqual(c["mobile_vendors"], ["Apple", "Samsung"])
        with self.assertRaises(w.Invalid):
            w.parse_classification(dict(c, ignore_weaker_dbm=-20))
        with self.assertRaises(w.Invalid):
            w.parse_classification(dict(c, managed_by_vendor=1))

    def test_detectors(self):
        d = {"deauth_min_bursts": 3, "deauth_window_s": 600, "evil_twin_k": 6, "evil_twin_min_db": 8,
             "evil_twin_guard_s": 0, "sensor_offline_min": 15}
        self.assertEqual(w.parse_detectors(d)["evil_twin_k"], 6.0)
        for k, v in (("evil_twin_guard_s", 900), ("deauth_min_bursts", 0), ("sensor_offline_min", 2000)):
            with self.subTest(k=k), self.assertRaises(w.Invalid):
                w.parse_detectors(dict(d, **{k: v}))


class ChannelPlans(unittest.TestCase):
    def test_stride_rule_as_the_installer(self):
        # PLAN 7.5: 89 ok, 90 bad (stride 4, only 45 visited)
        self.assertEqual(w.hop_stride(89), (4, 1))
        self.assertEqual(w.hop_stride(90), (4, 2))
        self.assertEqual(w.hop_stride(91), (4, 1))
        self.assertEqual(w.hop_stride(38), (4, 2))
        self.assertTrue(all(w.stride_ok(n) for n in (1, 2, 3, 37, 59, 89)))
        self.assertFalse(w.stride_ok(74))

    def test_plans(self):
        self.assertEqual(len(w.plan_channels("all_89")), 89)
        ht20 = w.plan_channels("ht20_37")
        self.assertEqual((len(ht20), "144" in ht20, ht20[:3], ht20[-1]), (37, False, ["1", "2", "3"], "165"))
        self.assertEqual(w.plan_channels("locked", lock="6"), ["6"])
        self.assertEqual(w.plan_channels("custom", ["1", "6", "11", "36ht40+", "44VHT80"]),
                         ["1", "6", "11", "36HT40+", "44VHT80"])
        # 14 entries: stride 4, gcd 2 - only 7 visited (short lists end at stride 1 and are fine)
        self.assertEqual(w.hop_stride(4), (1, 1))
        fourteen = [str(c) for c in range(1, 14)] + ["36"]
        for args in (("locked", None, "6HT40+"), ("custom", fourteen), ("custom", ["1", "1"]),
                     ("custom", ["ch1"]), ("no_empty_dfs_59",), ("auto",)):
            with self.subTest(args=args), self.assertRaises(w.Invalid):
                w.plan_channels(*args)

    def test_the_89_list_is_the_installers(self):
        if not os.path.exists(INSTALLER):          # the api image has no wifi-sensor folder
            self.skipTest("wifi-sensor/install_sensor.sh not here")
        src = open(INSTALLER, encoding="utf-8").read()
        m = re.search(r'RTW88_8821CU_CHANNELS="(.*?)"', src, re.S)
        self.assertEqual(m.group(1).replace("\\\n", "").split(","), w.ALL_89)

    def test_capture_config(self):
        names, c = w.parse_capture_config({"sensors": ["pi-fri", "pi-fri"],
                                           "config": {"channel_plan": "ht20_37", "dwell_s": 0.2, "poll_s": 30}})
        self.assertEqual((names, c["n_channels"], c["dwell_s"]), (["pi-fri"], 37, 0.2))
        for bad in ({"sensors": [], "config": {}}, {"sensors": ["a"], "config": {"channel_plan": "all_89", "dwell_s": 0.3, "poll_s": 30}},
                    {"sensors": ["a"], "config": {"channel_plan": "all_89", "dwell_s": 0.2, "poll_s": 45}}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_capture_config(bad)


class Hardware(unittest.TestCase):
    def test_hardware_change(self):
        a = w.parse_hardware_change({}, NOW)
        self.assertEqual((a["valid_from"], a["note"]), (NOW, None))
        past = w.parse_hardware_change({"valid_from": "2026-10-12T10:00:00+02:00", "note": " swapped antenna "}, NOW)
        self.assertEqual((past["valid_from"], past["note"]), (datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc), "swapped antenna"))
        self.assertEqual(w.parse_hardware_change({"valid_from": (NOW + timedelta(minutes=3)).isoformat()}, NOW)["note"], None)
        for body in ({"valid_from": "2026-10-12T19:00:00Z"},                       # the future
                     {"valid_from": "2026-10-12T10:00:00"},                        # no zone
                     {"valid_from": "yesterday"}, {"note": "x" * 501}, {"note": 5}):
            with self.subTest(body=body), self.assertRaises(w.Invalid):
                w.parse_hardware_change(body, NOW)
        self.assertEqual(w.hardware_change_label("swapped antenna"), "Hardware change: swapped antenna")
        self.assertEqual(w.hardware_change_label(None), "Hardware change")

    def test_sensor_meta(self):
        self.assertEqual(w.parse_sensor_meta({"location": " Dorm DF, 3rd floor ", "description": ""}),
                         {"location": "Dorm DF, 3rd floor", "description": None})
        with self.assertRaises(w.Invalid):
            w.parse_sensor_meta({"location": "x" * 121})

    def test_sensor_name_for_add_and_rename(self):
        # a new sensor needs a name; an edit may leave it out (unchanged) or give a new one
        self.assertEqual(w.parse_sensor_meta({"name": " pi-lab ", "location": "Lab"}, need_name=True),
                         {"name": "pi-lab", "location": "Lab", "description": None})
        self.assertNotIn("name", w.parse_sensor_meta({"location": "x"}))
        self.assertEqual(w.parse_sensor_meta({"name": "pi-fri2"})["name"], "pi-fri2")
        for bad in ({}, {"name": ""}, {"name": "-lab"}, {"name": "a b"}, {"name": "x" * 41}, {"name": "../x"}, {"name": 5}):
            with self.subTest(bad=bad), self.assertRaises(w.Invalid):
                w.parse_sensor_meta(bad, need_name=True)


if __name__ == "__main__":
    unittest.main()
