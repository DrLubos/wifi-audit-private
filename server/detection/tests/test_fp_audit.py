"""Tests for fp_audit pure helpers - no database."""

import os
import statistics
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import fp_audit as fa                                     # noqa: E402
from detection.evil_twin import Episode                                    # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


class CensoringBiasTest(unittest.TestCase):
    def test_bias_recovers_the_true_median(self):
        # true readings -114 .. -80 dBm, 20 of each; the receiver reports
        # everything at or below -106 as the floor (censored)
        true = sorted(v for v in range(-114, -79) for _ in range(20))
        floor = -106
        valid = [v for v in true if v > floor]
        f = 1 - len(valid) / len(true)
        self.assertAlmostEqual(f, 9 / 35)
        bias = fa.censoring_bias(valid, f)
        # dropping the floors moved the median up by exactly the censoring bias
        self.assertAlmostEqual(statistics.median(valid) - bias, statistics.median(true), delta=0.5)
        self.assertGreater(bias, 4)

    def test_no_floors_no_bias(self):
        self.assertEqual(fa.censoring_bias([-70, -68, -66], 0.0), 0)

    def test_censored_median_is_undefined(self):
        self.assertIsNone(fa.censoring_bias([-90, -88], 0.5))
        self.assertIsNone(fa.censoring_bias([], 0.1))

    def test_admissible_interval(self):
        rows = [(0.0, 0.0, 3.0), (0.15, 0.0, 3.0), (0.32, 2.0, 3.0), (0.43, 14.0, 3.0), (0.6, None, 3.0)]
        self.assertEqual(fa.admissible_floor_share(rows), (0.32, 0.43))
        self.assertEqual(fa.admissible_floor_share([(0.0, 0.0, 1.5)]), (0.0, 0.5))
        # the 1 dB tolerance floor applies when the robust sd is smaller
        self.assertEqual(fa.admissible_floor_share([(0.1, 0.9, 0.0), (0.2, 1.1, 0.0)]), (0.1, 0.2))


class ClassifyTest(unittest.TestCase):
    def ep(self, key, start_s, end_s):
        e = Episode(device_key=key)
        e.first_ts, e.last_ts = T0 + timedelta(seconds=start_s), T0 + timedelta(seconds=end_s)
        return e

    def test_classes(self):
        degraded = [(T0, T0 + timedelta(hours=1), "gap", "disk full")]
        changes = {"B": [T0 + timedelta(hours=5)]}
        self.assertEqual(fa.classify(self.ep("A", 600, 900), degraded, changes, {}), "degraded: disk full")
        self.assertEqual(fa.classify(self.ep("B", 5.5 * 3600, 5.6 * 3600), degraded, changes, {}),
                         "channel change +-1 h")
        self.assertEqual(fa.classify(self.ep("B", 8 * 3600, 8.1 * 3600), degraded, changes, {}), "open")

    def test_degraded_csv_parses(self):
        rows = fa.load_degraded(fa.DEGRADED_CSV, "pi-fri")
        self.assertGreaterEqual(len(rows), 9)
        self.assertTrue(all(s <= e for s, e, _, _ in rows))
        self.assertIn("disk full", [label for _, _, _, label in rows])

    def test_ap_kind(self):
        self.assertEqual(fa.ap_kind({"random_bssid": True, "oui": "02:00:00"}), "randomised")
        self.assertEqual(fa.ap_kind({"random_bssid": False, "oui": "ec:58:ea"}), "campus Ruckus")
        self.assertEqual(fa.ap_kind(None), "?")


class SplitAndHardwareTest(unittest.TestCase):
    def test_the_default_split_is_the_middle_of_the_window(self):
        frm, to = datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
        self.assertEqual(fa.split_point(frm, to), datetime(2026, 9, 21, 12, tzinfo=UTC))
        # the middle of all stored data (2026-09-15 .. 2026-11-15) is after this window's end
        self.assertLess(fa.split_point(frm, to), to)
        at = datetime(2026, 9, 20, tzinfo=UTC)
        self.assertEqual(fa.split_point(frm, to, at), at)

    def test_readings_of_other_hardware_are_counted_not_compared(self):
        from collections import Counter
        swap = datetime(2026, 10, 1, tzinfo=UTC)
        readings = [(swap + timedelta(hours=h), -60) for h in range(-3, 4)]
        skipped = Counter()
        new = fa.same_hardware(readings, {"hw_from": swap, "hw_to": None}, skipped)
        self.assertEqual((len(new), skipped["other_hardware"]), (4, 3))
        old = fa.same_hardware(readings, {"hw_from": None, "hw_to": swap})
        self.assertEqual([r[0] < swap for r in old], [True] * 3)
        self.assertEqual(len(fa.same_hardware(readings, {"hw_from": None, "hw_to": None})), 7)

    def test_the_split_fit_is_cut_to_a_hardware_period(self):
        self.assertIn("hardware_period(%(sid)s, %(at)s)", fa.HARDWARE_PERIOD_SQL)
        self.assertIn("o.ts >= %(p_from)s", fa.SPLIT_SQL)


if __name__ == "__main__":
    unittest.main()
