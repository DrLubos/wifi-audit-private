"""Configuration delivery (docs/config-delivery.md): the server's rules for
a capture configuration agree with the Pi's on the shared fixture
(wifi-sensor/sensor/tests/capture_config_cases.json), and the canonical hash
is the Pi's. Standard library only."""

import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app.v2 import writes as w  # noqa: E402

FIXTURE = os.path.join(HERE, "..", "..", "..", "wifi-sensor", "sensor", "tests", "capture_config_cases.json")
CASES = json.load(open(FIXTURE, encoding="utf-8"))["cases"]


class SharedFixture(unittest.TestCase):
    def test_same_verdict_as_the_pi(self):
        for case in CASES:
            with self.subTest(case["name"]):
                c = case["config"]
                if case["ok"]:
                    w.check_capture_config(c["channels"], c["dwell_s"], c["poll_s"], case["capabilities"])
                else:
                    with self.assertRaises(w.Invalid) as e:
                        w.check_capture_config(c["channels"], c["dwell_s"], c["poll_s"], case["capabilities"])
                    self.assertIn(case["reason"], str(e.exception))

    def test_canonical_hash(self):
        cfg = {"channels": ["1", "6", "11"], "dwell_s": 0.2, "poll_s": 30}
        self.assertEqual(w.config_canonical(cfg), b'{"channels":["1","6","11"],"dwell_s":0.2,"poll_s":30,"v":1}')
        self.assertEqual(w.config_sha256(cfg), hashlib.sha256(w.config_canonical(cfg)).hexdigest())
        # a real column reads 0.2 back as 0.20000000298...: the same hash
        import struct
        as_real = struct.unpack("f", struct.pack("f", 0.2))[0]
        self.assertEqual(w.config_sha256(dict(cfg, dwell_s=as_real)), w.config_sha256(cfg))

    def test_the_plans_pass_the_rules(self):
        for plan in ("all_89", "ht20_37"):
            w.check_capture_config(w.plan_channels(plan), 0.2, 30, None)


if __name__ == "__main__":
    unittest.main()
