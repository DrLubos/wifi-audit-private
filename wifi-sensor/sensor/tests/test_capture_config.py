"""The Pi's capture-configuration rules (sensor/capture_config.py,
server/docs/config-delivery.md 4.1) on the fixture the server's tests run
too. Standard library only:

  cd wifi-sensor && python3 -m unittest discover -s sensor/tests
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import capture_config as cc  # noqa: E402

CASES = json.load(open(os.path.join(HERE, "capture_config_cases.json"), encoding="utf-8"))["cases"]


class SharedFixture(unittest.TestCase):
    def test_every_case(self):
        for case in CASES:
            with self.subTest(case["name"]):
                ok, reason = cc.validate(case["config"], case["capabilities"])
                self.assertEqual(ok, case["ok"], reason)
                if not case["ok"]:
                    self.assertIn(case["reason"], reason)


class Stride(unittest.TestCase):
    def test_kismet_stride_search(self):
        # the installer's hop_stride_ok and the api's hop_stride: 91 -> 4 (gcd 1),
        # 90 -> 4 (gcd 2), 89 -> 4 (gcd 1), 38 -> 4 (gcd 2), 37 -> 4, 1 -> 1
        self.assertEqual([cc.hop_stride(n) for n in (91, 90, 89, 38, 37, 1)],
                         [(4, 1), (4, 2), (4, 1), (4, 2), (4, 1), (1, 1)])


class Canonical(unittest.TestCase):
    def test_hash_is_stable_and_order_of_keys_free(self):
        a = {"channels": ["1", "6", "11"], "dwell_s": 0.2, "poll_s": 30}
        b = {"poll_s": 30, "dwell_s": 0.2, "channels": ["1", "6", "11"]}
        self.assertEqual(cc.canonical(a), cc.canonical(b))
        self.assertEqual(cc.canonical(a), b'{"channels":["1","6","11"],"dwell_s":0.2,"poll_s":30,"v":1}')
        self.assertEqual(len(cc.sha256(a)), 64)
        self.assertNotEqual(cc.sha256(a), cc.sha256(dict(a, poll_s=60)))

    def test_entries_are_normalised_upper_case(self):
        self.assertEqual(cc.normalise({"channels": ["6ht40+", " 36vht80"], "dwell_s": 1, "poll_s": 15}),
                         {"channels": ["6HT40+", "36VHT80"], "dwell_s": 1.0, "poll_s": 15})


if __name__ == "__main__":
    unittest.main()
