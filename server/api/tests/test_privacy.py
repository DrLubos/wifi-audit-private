"""The response scanner of the v2 privacy rule (app/v2/privacy.py). Standard library only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import privacy  # noqa: E402

BSSIDS = {"e4:8d:8c:41:7b:a2", "EC:58:EA:12:34:56"}


class Scanner(unittest.TestCase):
    def test_finds_every_spelling(self):
        resp = {"items": [{"bssid": "E4:8D:8C:41:7B:A2", "summary": "client 3c-22-fb-01-02-03 left"},
                          {"evidence": {"clients": ["3C:22:FB:01:02:04"]}},
                          {"key": "4202770D00000000_050201FB223C"}],      # byte-reversed
                "3c:22:fb:01:02:06": 1}
        self.assertEqual(privacy.macs_in(resp), {"E4:8D:8C:41:7B:A2", "3C:22:FB:01:02:03",
                                                 "3C:22:FB:01:02:04", "3C:22:FB:01:02:05",
                                                 "3C:22:FB:01:02:06"})
        self.assertEqual(privacy.foreign_macs(resp, BSSIDS),
                         {"3C:22:FB:01:02:03", "3C:22:FB:01:02:04", "3C:22:FB:01:02:05",
                          "3C:22:FB:01:02:06"})

    def test_bssids_and_ordinary_values_pass(self):
        resp = {"ap": {"bssid": "ec:58:ea:12:34:56", "oui": "EC:58:EA"},
                "ts": "2026-10-11T14:08:52Z", "label": "3 Oct 18:00 – 10 Oct 17:52",
                "ipv6ish": "fe80::1", "hex": "0123456789abcdef0123", "ratio": "12:34:56"}
        self.assertEqual(privacy.foreign_macs(resp, BSSIDS), set())

    def test_no_partial_matches_inside_longer_tokens(self):
        self.assertEqual(privacy.macs_in("AA:BB:CC:DD:EE:FF:00"), set())
        self.assertEqual(privacy.macs_in("x1AA:BB:CC:DD:EE:FF"), set())


class Redact(unittest.TestCase):
    def test_client_macs_go_bssids_stay(self):
        ev = {"ap": {"bssid": "E4:8D:8C:41:7B:A2", "device_key": "4202770D00000000_A27B418C8DE4"},
              "alerts": {"n": 2, "ids": [12, 13], "sources": ["1C:57:DC:7B:0C:DF"]},
              "note": "from 1c:57:dc:7b:0c:df to e4:8d:8c:41:7b:a2", "3C:22:FB:01:02:06": 1}
        out = privacy.redact(ev, BSSIDS)
        self.assertEqual(out["ap"], ev["ap"])
        self.assertEqual(out["alerts"], {"n": 2, "ids": [12, 13], "sources": [privacy.MASK]})
        self.assertEqual(out["note"], "from %s to e4:8d:8c:41:7b:a2" % privacy.MASK)
        self.assertIn(privacy.MASK, out)
        self.assertEqual(privacy.foreign_macs(out, BSSIDS), set())
        self.assertEqual(ev["alerts"]["sources"], ["1C:57:DC:7B:0C:DF"])      # input untouched


if __name__ == "__main__":
    unittest.main()
