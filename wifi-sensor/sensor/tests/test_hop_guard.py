"""Tests for sensor/hop_guard.py with a fake Kismet (no network).

Run from wifi-sensor/:  python3 -m unittest discover -s sensor/tests
"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hop_guard  # noqa: E402

CONFIGURED = ["1", "1HT40+", "2", "6", "6HT40-", "11", "36", "36VHT80", "44", "44HT40+", "165"]  # 11, prime


class FakeKismet:
    def __init__(self, hop, skip=4, shuffle=1, running=1, rate=5.0, fail_set=False):
        self.src = {"kismet.datasource.uuid": "U", "kismet.datasource.name": "capture",
                    "kismet.datasource.running": running, "kismet.datasource.hop_channels": hop,
                    "kismet.datasource.hop_shuffle_skip": skip, "kismet.datasource.hop_shuffle": shuffle,
                    "kismet.datasource.hop_rate": rate}
        self.fail_set = fail_set
        self.set_calls = []

    def sources(self):
        return [dict(self.src)]

    def set_hop(self, uuid, channels, rate, shuffle):
        self.set_calls.append((uuid, list(channels), rate, shuffle))
        if self.fail_set:
            raise OSError("HTTP 500")
        return {}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = os.path.join(self.tmp.name, "kismet_site.conf")
        with open(self.conf, "w") as f:
            f.write('source=wlan1:name=capture,type=linuxwifi,channels="%s"\n' % ",".join(CONFIGURED))
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, kismet):
        return hop_guard.Guard(kismet, self.conf, log=self.logs.append)

    def test_shuffled_live_list_is_left_alone(self):
        live = CONFIGURED[:]
        random.Random(3).shuffle(live)
        self.assertNotEqual(live, CONFIGURED)
        k = FakeKismet(live)
        g = self.guard(k)
        for _ in range(5):
            self.assertEqual(g.check(), "ok")
        self.assertEqual(k.set_calls, [])

    def test_case_difference_is_left_alone(self):
        k = FakeKismet([c.lower() for c in CONFIGURED])
        self.assertEqual(self.guard(k).check(), "ok")
        self.assertEqual(k.set_calls, [])

    def test_collapse_is_reapplied_with_the_configured_list(self):
        k = FakeKismet(["1"], skip=1, rate=5.0)
        g = self.guard(k)
        self.assertEqual(g.check(), "reapplied")
        self.assertEqual(k.set_calls, [("U", CONFIGURED, 5.0, 1)])
        self.assertTrue(any("live 1 entries -> 11" in m for m in self.logs))
        # Once the list is back, a check resets the failure count.
        k.src["kismet.datasource.hop_channels"] = CONFIGURED[::-1]
        self.assertEqual(g.check(), "ok")
        self.assertEqual(g.failures, 0)

    def test_duplicate_or_missing_entry_is_reapplied(self):
        for live in (CONFIGURED + ["1"], CONFIGURED[:-1]):
            k = FakeKismet(live)
            self.assertEqual(self.guard(k).check(), "reapplied")

    def test_zero_rate_falls_back_to_5(self):
        k = FakeKismet(["1"], rate=0)
        self.guard(k).check()
        self.assertEqual(k.set_calls[0][2], 5.0)

    def test_backoff_after_repeated_failures(self):
        k = FakeKismet(["1"], fail_set=True)
        g = self.guard(k)
        for _ in range(hop_guard.BACKOFF_AFTER):
            self.assertEqual(g.check(), "error")
        self.assertEqual(g.interval(10), hop_guard.BACKOFF_INTERVAL)

    def test_not_sticking_also_backs_off(self):
        k = FakeKismet(["1"])       # re-apply "succeeds" but the list never changes
        g = self.guard(k)
        for _ in range(hop_guard.BACKOFF_AFTER):
            g.check()
        self.assertEqual(g.interval(10), hop_guard.BACKOFF_INTERVAL)

    def test_source_not_running_is_idle(self):
        k = FakeKismet(["1"], running=0)
        self.assertEqual(self.guard(k).check(), "idle")
        self.assertEqual(k.set_calls, [])

    def test_auto_list_is_idle(self):
        with open(self.conf, "w") as f:
            f.write("source=wlan1:name=capture,type=linuxwifi\n")
        k = FakeKismet(["1"])
        self.assertEqual(self.guard(k).check(), "idle")
        self.assertEqual(k.set_calls, [])

    def test_stride_warning(self):
        live = CONFIGURED + ["13"]  # 12 entries, stride 4 -> 3 visited
        with open(self.conf, "w") as f:
            f.write('source=wlan1:channels="%s"\n' % ",".join(live))
        g = self.guard(FakeKismet(live, skip=4))
        self.assertEqual(g.check(), "ok")
        self.assertTrue(any("visits only 3 of 12" in m for m in self.logs))

    def test_visited(self):
        self.assertEqual(hop_guard.visited(90, 4, 1), 45)
        self.assertEqual(hop_guard.visited(89, 4, 1), 89)
        self.assertEqual(hop_guard.visited(90, 4, 0), 90)


if __name__ == "__main__":
    unittest.main()
