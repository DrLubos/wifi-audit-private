"""Configuration delivery on the ingest side (app/ingest/config.py,
docs/config-delivery.md 3, 5.5, 5.6, 6): what the sensor reports
(config_state, capabilities), when a response carries the desired
configuration, and what each reported attempt does. Standard library only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ingest import config as c  # noqa: E402
from app.ingest.validate import Invalid  # noqa: E402

H1, H2 = "a" * 64, "b" * 64


def desired(**kw):
    row = {"version": 4, "channels": ["1", "6", "11"], "dwell_s": 0.2, "poll_s": 30, "sha256": H1,
           "refused_at": None, "confirmed_at": None, "retry": False}
    row.update(kw)
    return row


def state(**kw):
    s = {"accepting": True, "applied_version": 3, "applied_sha256": H2, "attempt": None}
    s.update(kw)
    return s


def attempt(result, **kw):
    a = {"version": 4, "sha256": H1, "result": result, "reason": None, "started_poll": 100,
         "effective_poll": None, "restored_poll": None, "checks": {}}
    a.update(kw)
    return a


class ParseState(unittest.TestCase):
    def test_valid(self):
        s = c.parse_state({"accepting": True, "applied_version": 3, "applied_sha256": H2,
                           "attempt": attempt("applied", effective_poll=130, checks={"polls": 4, "hop_ok": True})})
        self.assertEqual(s["attempt"]["result"], "applied")
        self.assertEqual(s["attempt"]["effective_poll"], 130)
        self.assertIsNone(c.parse_state(None))

    def test_invalid(self):
        for bad in ({"accepting": "yes"}, {"accepting": True, "applied_sha256": "xyz"},
                    {"accepting": True, "attempt": attempt("done")},
                    {"accepting": True, "attempt": attempt("applied", reason="x" * 501)},
                    {"accepting": True, "attempt": attempt("applied", checks={"k": [1] * 100})},
                    {"accepting": True, "attempt": dict(attempt("applied"), version="4")}, []):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                c.parse_state(bad)

    def test_capabilities(self):
        cap = c.parse_capabilities({"channels_supported": ["1", "6HT40+", "165HT40-"], "reg_country": "SK"})
        self.assertEqual(cap["reg_country"], "SK")
        self.assertIsNone(c.parse_capabilities(None))
        for bad in ({"channels_supported": "1,6"}, {"channels_supported": ["x" * 40]},
                    {"channels_supported": ["1"] * 401}, {"reg_country": "Slovakia"}):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                c.parse_capabilities(bad)


class Delivery(unittest.TestCase):
    def test_a_pending_configuration_is_delivered(self):
        d = c.delivery(desired(), state())
        self.assertEqual(d, {"version": 4, "sha256": H1, "retry": False,
                             "desired": {"channels": ["1", "6", "11"], "dwell_s": 0.2, "poll_s": 30}})

    def test_nothing_to_deliver(self):
        self.assertIsNone(c.delivery(None, state()))                                   # nothing saved
        self.assertIsNone(c.delivery(desired(), None))                                # uploader without config_state
        self.assertIsNone(c.delivery(desired(), state(accepting=False)))              # CONFIG_APPLY=0
        self.assertIsNone(c.delivery(desired(), state(applied_sha256=H1)))            # already applied
        self.assertIsNone(c.delivery(desired(refused_at="2026-10-25"), state()))      # refused
        self.assertIsNone(c.delivery(desired(confirmed_at="2026-10-25"), state()))    # applied before
        self.assertIsNone(c.delivery(desired(sha256=None), state()))                  # a row without hash

    def test_retry_is_passed_on(self):
        self.assertTrue(c.delivery(desired(retry=True), state())["retry"])


class Attempts(unittest.TestCase):
    def kinds(self, a, row=None):
        return [x[0] for x in c.attempt_actions(a, row if row is not None else desired())]

    def test_received_marks_delivered(self):
        self.assertEqual(self.kinds(attempt("received")), ["attempt", "delivered"])
        self.assertEqual(self.kinds(attempt("applying")), ["attempt", "delivered"])

    def test_applied_confirms_and_draws_the_boundary(self):
        acts = c.attempt_actions(attempt("applied", effective_poll=130), desired())
        self.assertEqual([x[0] for x in acts], ["attempt", "delivered", "confirm", "boundary", "close_events"])
        boundary = dict(acts)["boundary"]
        self.assertEqual(boundary["poll"], 130)
        self.assertEqual(boundary["label"], "Capture configuration v4: 3 channels, dwell 0.2 s, poll 30 s")

    def test_applied_without_an_effective_poll_waits(self):
        self.assertEqual(self.kinds(attempt("applied")), ["attempt", "delivered"])

    def test_refusals(self):
        self.assertEqual(self.kinds(attempt("rejected", reason="not supported")), ["attempt", "delivered", "refuse"])
        acts = c.attempt_actions(attempt("rolled_back", reason="hop list 1/3", restored_poll=140), desired())
        self.assertEqual([x[0] for x in acts], ["attempt", "delivered", "refuse", "coverage", "open_event"])
        self.assertEqual(dict(acts)["coverage"], {"from_poll": 100, "to_poll": 140,
                                                  "label": "Configuration v4 tried, rolled back: hop list 1/3"})
        self.assertEqual(dict(acts)["open_event"]["type"], "config_rolled_back")
        acts = c.attempt_actions(attempt("failed", reason="rollback did not verify"), desired())
        self.assertEqual([x[0] for x in acts], ["attempt", "delivered", "refuse", "open_event"])
        self.assertEqual((dict(acts)["open_event"]["type"], dict(acts)["open_event"]["severity"]),
                         ("config_failed", "high"))

    def test_a_hash_other_than_the_stored_one_is_only_recorded(self):
        self.assertEqual(self.kinds(attempt("applied", sha256=H2, effective_poll=130)), ["attempt"])
        self.assertEqual(self.kinds(attempt("applied", effective_poll=130), row={}), ["attempt"])   # unknown version


if __name__ == "__main__":
    unittest.main()
