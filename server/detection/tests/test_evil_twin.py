"""Tests for evil_twin pure logic - synthetic timelines, no database.

The three historical false positives from analysis/inventory_changes.py are
encoded as regressions: a same-OUI + same-crypt unknown must be low (not high),
an IK-WIFI-DOT1X BSSID must never be matched to IK-WIFI's trusted set, and a
single noisy reading must never fire signal (b)."""

import argparse
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import evil_twin as et                       # noqa: E402
from detection.evil_twin import (Params, Unknown, assess, assess_unknown,      # noqa: E402
                                 build_episodes, classify_unknowns, grade_a, grade_b,
                                 windowed_deviations)

UTC = timezone.utc
T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
KEY = "AP1"


def at(sec):
    return T0 + timedelta(seconds=sec)


def stream(values, start=0, step=30):
    """[(ts, rssi)] from a list of rssi values, one per poll."""
    return [(at(start + i * step), v) for i, v in enumerate(values)]


class WindowedDeviationTest(unittest.TestCase):
    def setUp(self):
        self.p = Params(window_w=10, k=6.0, floor_db=8.0, spread_k=3.0, spread_floor_db=6.0)

    def test_clean_stream_no_deviation(self):
        # baseline -60, robust sd 2; readings jitter within a few dB
        vals = [-60, -61, -59, -62, -60, -58, -61, -60, -59, -60, -61, -60, -59, -62, -60]
        devs = windowed_deviations(stream(vals), -60.0, 2.0, self.p, KEY)
        self.assertEqual(devs, [])

    def test_sustained_median_shift_is_detected(self):
        # 10 clean then a sustained jump to -42 (18 dB stronger): windows deviate.
        # The fully-shifted windows are median_shift; the bimodal boundary window
        # (dev 9 dB, below k*sigma=12) legitimately registers as spread first.
        vals = [-60] * 10 + [-42] * 12
        devs = windowed_deviations(stream(vals), -60.0, 2.0, self.p, KEY)
        self.assertTrue(devs)
        self.assertTrue(any(d.facet in ("median_shift", "both") for d in devs))
        self.assertGreater(max(abs(d.dev_db) for d in devs), 15)

    def test_spread_inflation_detected_when_bimodal(self):
        # alternating between the real -60 and a clone at -40: median may stay mid
        # but the window MAD explodes
        vals = ([-60, -40] * 8)
        devs = windowed_deviations(stream(vals), -60.0, 2.0, self.p, KEY)
        self.assertTrue(devs)
        self.assertTrue(any("spread" in d.facet for d in devs))

    def test_floor_blocks_small_shift_on_tiny_sigma(self):
        # robust sd 0.5 -> k*sigma tiny, but the 8 dB floor still applies: a 5 dB
        # shift must NOT fire
        vals = [-60] * 10 + [-55] * 10
        devs = windowed_deviations(stream(vals), -60.0, 0.5, self.p, KEY)
        self.assertEqual([d for d in devs if d.facet != "spread_inflation"], [])


class EpisodeTest(unittest.TestCase):
    def setUp(self):
        self.p = Params(window_w=10, k=6.0, floor_db=8.0, persistence=6, persist_window_s=900,
                        med_dev_db=12.0, high_dev_db=20.0)
        self.base = {"rssi_median": -60.0, "rssi_robust_sd": 2.0, "base_n_obs": 5000, "base_n_days": 4}

    def _episodes(self, vals):
        devs = windowed_deviations(stream(vals), -60.0, 2.0, self.p, KEY)
        return build_episodes(devs, self.p.gap_s)

    def test_sustained_strong_deviation_emits_high_on_trusted(self):
        eps = self._episodes([-60] * 10 + [-38] * 15)   # 22 dB stronger, sustained
        self.assertEqual(len(eps), 1)
        assess(eps[0], self.p, self.base, "AA:BB:CC:00:00:01", "IK-WIFI", trusted=True)
        self.assertTrue(eps[0].rule_b)
        self.assertEqual(eps[0].direction, "stronger")
        self.assertEqual(eps[0].severity, "critical")     # high + trusted-stronger bump
        self.assertGreaterEqual(eps[0].max_in_window, self.p.persistence)

    def test_short_deviation_below_persistence_not_emitted(self):
        # A brief blip: 3 strong readings never move a 10-window median enough to
        # produce >= persistence deviating windows -> no detection (anti-FP).
        eps = self._episodes([-60] * 10 + [-40] * 3 + [-60] * 10)
        for e in eps:
            assess(e, self.p, self.base, "AA:BB:CC:00:00:01", "IK-WIFI", trusted=True)
            self.assertFalse(e.rule_b)
            self.assertIsNone(e.severity)

    def test_weaker_deviation_not_bumped(self):
        eps = self._episodes([-60] * 10 + [-82] * 15)     # 22 dB weaker
        assess(eps[0], self.p, self.base, "AA:BB:CC:00:00:01", "IK-WIFI", trusted=True)
        self.assertEqual(eps[0].direction, "weaker")
        self.assertEqual(eps[0].severity, "high")         # magnitude high, no stronger bump


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.p = Params()

    def test_grade_b_magnitude_and_bumps(self):
        self.assertEqual(grade_b(9, "stronger", False, False, self.p), "low")
        self.assertEqual(grade_b(15, "weaker", False, False, self.p), "medium")
        self.assertEqual(grade_b(25, "weaker", False, False, self.p), "high")
        self.assertEqual(grade_b(25, "stronger", True, False, self.p), "critical")   # high +1
        self.assertEqual(grade_b(15, "stronger", True, True, self.p), "critical")    # medium +2

    def test_grade_a_same_infra_is_low(self):
        self.assertEqual(grade_a(True, True, False, False), "low")
        self.assertEqual(grade_a(False, True, False, False), "medium")   # foreign OUI
        self.assertEqual(grade_a(True, False, False, False), "medium")   # crypt mismatch
        self.assertEqual(grade_a(False, False, True, False), "high")     # +close


TSETS = {
    "IK-WIFI": {"bssids": {"EC:58:EA:55:89:DC", "EC:58:EA:54:45:8C"},
                "ouis": {"EC:58:EA", "3C:46:A1"}, "dominant_crypt": "Open", "group_median": -55.0},
    "IK-WIFI-DOT1X": {"bssids": {"EC:58:EA:55:89:DD"}, "ouis": {"EC:58:EA"},
                      "dominant_crypt": "WPA2-EAP", "group_median": -55.0},
}


def inv_row(bssid, ssid, oui, crypt, **kw):
    d = dict(device_key="d_" + bssid, bssid=bssid, oui=oui, manuf="Test", ssid=ssid, crypt=crypt,
             random_bssid=False, trusted=False, first_seen=at(-100000),
             win_first=at(0), win_last=at(3600), win_obs=120, win_median=-50.0)
    d.update(kw)
    return d


class UnknownTest(unittest.TestCase):
    def setUp(self):
        self.p = Params(persist_hours=1.0, persist_obs=20, close_dbm=-60, strong_db=10)

    def test_ruckus_second_vendor_same_oui_and_crypt_is_low_not_high(self):
        # a legit second-vendor infra BSSID: OUI in the trusted set, crypt matches
        u = classify_unknowns([inv_row("3C:46:A1:11:22:33", "IK-WIFI", "3C:46:A1", "Open")],
                              TSETS, {}, self.p)
        self.assertEqual(len(u), 1)
        assess_unknown(u[0], self.p)
        self.assertTrue(u[0].oui_in_trusted and u[0].crypt_matches)
        self.assertIn(u[0].severity, ("low", "medium"))   # never high just for existing
        self.assertNotEqual(u[0].severity, "high")

    def test_foreign_oui_unknown_is_medium_or_higher(self):
        u = classify_unknowns([inv_row("DE:AD:BE:EF:00:01", "IK-WIFI", "DE:AD:BE", "Open",
                                       win_median=-45.0)], TSETS, {}, self.p)
        assess_unknown(u[0], self.p)
        self.assertFalse(u[0].oui_in_trusted)
        self.assertIn(u[0].severity, ("medium", "high"))

    def test_dot1x_bssid_not_matched_to_ik_wifi_trusted_set(self):
        # an unknown BSSID advertising IK-WIFI-DOT1X is checked against that SSID's
        # own trusted set, NOT IK-WIFI's - the exact-match guard (prefix FP dropped)
        u = classify_unknowns([inv_row("EC:58:EA:99:99:99", "IK-WIFI-DOT1X", "EC:58:EA", "WPA2-EAP")],
                              TSETS, {}, self.p)
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].ssid, "IK-WIFI-DOT1X")
        assess_unknown(u[0], self.p)
        self.assertTrue(u[0].crypt_matches)                # matched DOT1X's WPA2-EAP, not IK-WIFI's Open

    def test_trusted_member_is_not_flagged(self):
        u = classify_unknowns([inv_row("EC:58:EA:55:89:DC", "IK-WIFI", "EC:58:EA", "Open")],
                              TSETS, {}, self.p)
        self.assertEqual(u, [])

    def test_random_bssid_never_sustained(self):
        u = classify_unknowns([inv_row("DE:AD:BE:EF:00:02", "IK-WIFI", "DE:AD:BE", "Open",
                                       random_bssid=True)], TSETS, {}, self.p)
        assess_unknown(u[0], self.p)
        self.assertFalse(u[0].sustained)
        self.assertIsNone(u[0].severity)

    def test_transient_unknown_not_sustained(self):
        u = classify_unknowns([inv_row("DE:AD:BE:EF:00:03", "IK-WIFI", "DE:AD:BE", "Open",
                                       win_obs=5, win_first=at(0), win_last=at(120))],
                              TSETS, {}, self.p)
        assess_unknown(u[0], self.p)
        self.assertFalse(u[0].sustained)

    def test_unestablished_ssid_yields_no_candidate(self):
        # an SSID with no trusted set (a random hotspot name) can never trigger
        u = classify_unknowns([inv_row("DE:AD:BE:EF:00:04", "totally-new-ssid", "DE:AD:BE", "Open")],
                              TSETS, {}, self.p)
        self.assertEqual(u, [])


class TrustedSetTest(unittest.TestCase):
    def test_whitelist_source(self):
        p = Params(trusted_source="whitelist")
        inv = [inv_row("EC:58:EA:55:89:DC", "IK-WIFI", "EC:58:EA", "Open", trusted=True),
               inv_row("3C:46:A1:00:00:01", "IK-WIFI", "3C:46:A1", "Open", trusted=True),
               inv_row("DE:AD:BE:EF:00:01", "IK-WIFI", "DE:AD:BE", "Open", trusted=False)]
        sets = et._trusted_sets(inv, p, at(0))
        self.assertEqual(sets["IK-WIFI"]["bssids"], {"EC:58:EA:55:89:DC", "3C:46:A1:00:00:01"})
        self.assertEqual(sets["IK-WIFI"]["ouis"], {"EC:58:EA", "3C:46:A1"})
        self.assertEqual(sets["IK-WIFI"]["dominant_crypt"], "Open")

    def test_baseline_source_cutoff(self):
        p = Params(trusted_source="baseline", baseline_hours=24)
        frm = at(0)
        inv = [inv_row("EC:58:EA:55:89:DC", "IK-WIFI", "EC:58:EA", "Open", first_seen=at(-3600)),
               inv_row("DE:AD:BE:EF:00:01", "IK-WIFI", "DE:AD:BE", "Open",
                       first_seen=frm + timedelta(hours=48))]   # appears well after the cutoff
        sets = et._trusted_sets(inv, p, frm)
        self.assertEqual(sets["IK-WIFI"]["bssids"], {"EC:58:EA:55:89:DC"})


class ParamsTest(unittest.TestCase):
    def test_from_args(self):
        p = argparse.ArgumentParser()
        et.add_arguments(p)
        args = p.parse_args(["--k", "4", "--trusted-source", "baseline", "--window", "20"])
        params = Params.from_args(args)
        self.assertEqual((params.k, params.trusted_source, params.window_w), (4.0, "baseline", 20))
        self.assertEqual(params.as_dict()["trusted_source"], "baseline")


if __name__ == "__main__":
    unittest.main()
