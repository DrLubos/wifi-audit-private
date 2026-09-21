"""Tests for the deauth_flood logic on synthetic timelines (no database).

The fixtures mirror the two organic events of the seeded data (a pair of
DEAUTHFLOOD alerts 13 ms apart, at most one burst poll) and a staged flood
(alerts at the 5/min throttle, a burst poll almost every 30 s).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import deauth_flood as df  # noqa: E402
from detection.common import iso  # noqa: E402
from detection.deauth_flood import Alert, Burst, Params  # noqa: E402

T0 = datetime(2026, 9, 18, 7, 42, 56, 533000, tzinfo=timezone.utc)
AP = "4202770D00000000_DC8955EA58EC"
BSSID = "EC:58:EA:55:89:DC"
CLIENT = "1C:57:DC:7B:0C:DF"


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def alert(id_, seconds, header="DEAUTHFLOOD", src=CLIENT, dst=BSSID, direction="to_ap",
          device_key=AP, bssid=BSSID):
    return Alert(id=id_, ts=at(seconds), header=header, bssid=bssid, source_mac=src,
                 dest_mac=dst, direction=direction, device_key=device_key, ssid="IK-WIFI")


def burst(seconds, prev=1, cur=4, since=30, device_key=AP):
    """A fallback (counter) event timed at its poll."""
    return Burst(ts=at(seconds), device_key=device_key, prev=prev, cur=cur, since_prev_s=since)


def frame(seconds, poll_seconds=None, device_key=AP):
    """An exact-rule event: ts is the frame second, poll_ts the poll that saw it."""
    return Burst(ts=at(seconds), device_key=device_key, prev=1, cur=1, since_prev_s=30,
                 poll_ts=at(poll_seconds if poll_seconds is not None else seconds + 12),
                 source="last")


def organic():
    """Alerts 12+13 within 13 ms and one burst poll 4.5 s later (event 1)."""
    return [alert(12, 0), alert(13, 0.013)], [burst(4.5)]


def flood(minutes=10):
    """Alerts at 5/min for `minutes`, a burst poll in 10 of 11 polls."""
    alerts = [alert(100 + i, 60 * m + 2 * i, src=BSSID, dst="FF:FF:FF:FF:FF:FF", direction="broadcast")
              for m in range(minutes) for i in range(5)]
    bursts = [burst(4.5 + 30 * k, prev=k % 11, cur=(k * 7) % 11 + 1) for k in range(minutes * 2)
              if k % 11 != 3]
    return alerts, bursts


class EpisodeTest(unittest.TestCase):
    def test_gap_splits_episodes_per_ap(self):
        alerts = [alert(1, 0), alert(2, 0.02), alert(3, 400)]
        bursts = [burst(4.5), burst(405), burst(30, device_key="OTHER")]
        eps = df.build_episodes(alerts, bursts, gap_s=300)
        self.assertEqual([(e.subject, len(e.alerts), len(e.bursts)) for e in eps],
                         [(AP, 2, 1), ("OTHER", 0, 1), (AP, 1, 1)])
        self.assertEqual(eps[0].first_ts, at(0))
        self.assertEqual(eps[0].last_ts, at(4.5))

    def test_alert_without_device_row_is_keyed_by_bssid(self):
        eps = df.build_episodes([alert(1, 0, device_key=None, bssid="02:00:00:00:00:01")], [], 300)
        self.assertEqual(eps[0].subject, "02:00:00:00:00:01")
        self.assertIsNone(eps[0].device_key)
        self.assertEqual(eps[0].label(), "IK-WIFI (02:00:00:00:00:01)")

    def test_corroborating_alerts_never_trigger(self):
        eps = df.build_episodes([alert(1, 0, header="BCASTDISCON", direction="broadcast")], [], 300)
        ep = df.assess(eps[0], Params())
        self.assertFalse(ep.rule_a)
        self.assertFalse(ep.emitted)
        self.assertEqual(len(ep.corroborating), 1)
        self.assertTrue(ep.broadcast)

    def test_overlaps_window(self):
        ep = df.build_episodes(*organic(), gap_s=300)[0]
        self.assertTrue(ep.overlaps(at(-3600), at(3600)))
        self.assertTrue(ep.overlaps(at(2), at(3600)))        # window starts mid-episode
        self.assertFalse(ep.overlaps(at(5), at(3600)))
        self.assertFalse(ep.overlaps(at(-3600), at(0)))      # to is exclusive


class WindowAndThresholdTest(unittest.TestCase):
    def test_max_in_window_is_inclusive(self):
        times = [at(0), at(300), at(590)]
        self.assertEqual(df.max_in_window(times, 600), 3)
        self.assertEqual(df.max_in_window([at(0), at(300), at(601)], 600), 2)
        self.assertEqual(df.max_in_window([], 600), 0)
        self.assertEqual(df.max_in_window([at(5), at(0)], 600), 2)   # unsorted input

    def test_poisson_threshold(self):
        self.assertEqual(df.poisson_threshold(0.0, 600, 1e-4, 3), 3)
        self.assertEqual(df.poisson_threshold(0.1, 600, 1e-4, 3), 3)   # mu = 0.017
        self.assertEqual(df.poisson_threshold(2.0, 600, 1e-4, 3), 5)   # mu = 0.33
        self.assertEqual(df.poisson_threshold(60.0, 600, 1e-4, 3), 25)  # mu = 10: P(X>=24) = 1.1e-4, P(X>=25) = 4e-5
        self.assertEqual(df.poisson_threshold(0.1, 600, 1e-4, 6), 6)   # floor wins


class GradeTest(unittest.TestCase):
    def test_table(self):
        g = df.grade
        self.assertIsNone(g(False, False, None, 60))
        self.assertEqual(g(True, False, 0.013, 60), "low")
        self.assertEqual(g(True, False, 10, 60), "medium")
        self.assertEqual(g(True, False, 60, 60), "high")
        self.assertEqual(g(False, True, None, 60), "medium")
        self.assertEqual(g(True, True, 0.013, 60), "high")

    def test_trusted_bump_needs_attack_direction(self):
        g = df.grade
        self.assertEqual(g(True, False, 0.013, 60, attack_like=True, trusted=True), "medium")
        self.assertEqual(g(True, False, 0.013, 60, attack_like=False, trusted=True), "low")
        self.assertEqual(g(True, False, 0.013, 60, attack_like=True, trusted=False), "low")
        self.assertEqual(g(True, True, 100, 60, attack_like=True, trusted=True), "critical")
        self.assertEqual(g(True, True, 100, 60, attack_like=True, trusted=False), "high")


class AssessmentTest(unittest.TestCase):
    def setUp(self):
        self.params = Params()
        self.ap = {"device_key": AP, "bssid": BSSID, "ssid": "IK-WIFI", "crypt": "Open",
                   "mfp_req": False, "trusted": False}
        self.baseline = {"device_key": AP, "polls": 14184, "bursts": 7, "active_hours": 118.2,
                         "mgmt_med": 10.0, "mgmt_robust_sd": 2.9, "flood_alerts": 0}

    def test_organic_pair_is_low_rule_a_only(self):
        ep = df.build_episodes(*organic(), gap_s=300)[0]
        df.assess(ep, self.params, self.baseline, self.ap)
        self.assertTrue(ep.rule_a)
        self.assertFalse(ep.rule_b)
        self.assertEqual(ep.threshold, 3)
        self.assertEqual(ep.max_in_window, 1)
        self.assertEqual(ep.severity, "low")
        self.assertEqual(ep.directions, ["to_ap"])
        self.assertEqual(ep.sources, [CLIENT])
        self.assertFalse(ep.attack_like)
        f = df.make_finding(ep, self.params, None, (at(-7 * 86400), at(-300)))
        self.assertEqual((f.type, f.severity, f.device_key, f.mac, f.ssid), 
                         ("deauth_flood", "low", AP, BSSID, "IK-WIFI"))
        self.assertEqual(f.ts, at(0))
        self.assertEqual(f.subject, AP)
        self.assertEqual(f.evidence["alerts"]["ids"], [12, 13])
        self.assertEqual(f.evidence["alerts"]["span_s"], 0.013)
        self.assertEqual(f.evidence["bursts"]["values"], [[1, 4]])
        self.assertEqual(f.evidence["rules"], {"alerts": True, "bursts": False})
        self.assertEqual(f.evidence["baseline"]["bursts_per_hour"], round(7 / 118.2, 4))
        self.assertEqual(f.evidence["window_start"], "2026-09-18T07:42:56.533Z")
        self.assertIn("2 DEAUTHFLOOD alerts within 0.01s from 1 source (to ap), 1 burst poll; brief",
                      f.summary)
        self.assertTrue(f.summary.startswith("Deauth/disassoc flood on IK-WIFI (EC:58:EA:55:89:DC)"))

    def test_alerts_without_bursts_still_emit(self):
        ep = df.build_episodes([alert(25, 0), alert(26, 0.04)], [], gap_s=300)[0]
        df.assess(ep, self.params, self.baseline, self.ap)
        self.assertTrue(ep.emitted)
        self.assertEqual(ep.severity, "low")
        self.assertEqual(len(ep.bursts), 0)
        self.assertIn("0 burst polls; brief", df.summary_text(ep, self.params))

    def test_staged_flood_is_high_with_both_rules(self):
        ep = df.build_episodes(*flood(10), gap_s=300)[0]
        df.assess(ep, self.params, self.baseline, self.ap)
        self.assertTrue(ep.rule_a and ep.rule_b)
        self.assertEqual(ep.severity, "high")
        self.assertGreaterEqual(ep.alert_span_s, 60)
        self.assertEqual(ep.directions, ["broadcast"])
        self.assertTrue(ep.attack_like)
        self.assertGreaterEqual(ep.max_in_window, 3)
        self.assertIn("sustained for", df.summary_text(ep, self.params))
        # the same flood on an operator-trusted AP is critical
        df.assess(ep, self.params, self.baseline, dict(self.ap, trusted=True))
        self.assertEqual(ep.severity, "critical")

    def test_rule_b_alone_needs_threshold_bursts(self):
        bursts = [burst(0), burst(200), burst(400)]
        ep = df.build_episodes([], bursts, gap_s=300)[0]
        df.assess(ep, self.params, self.baseline, self.ap)
        self.assertFalse(ep.rule_a)
        self.assertTrue(ep.rule_b)
        self.assertEqual(ep.severity, "medium")
        self.assertTrue(df.summary_text(ep, self.params).startswith(
            "Sustained deauth/disassoc activity on IK-WIFI"))
        # two organic bursts an hour apart are not an episode, let alone a detection
        ep2 = df.build_episodes([], [burst(0), burst(3600)], gap_s=300)
        self.assertEqual(len(ep2), 2)
        for e in ep2:
            df.assess(e, self.params, self.baseline, self.ap)
            self.assertFalse(e.emitted)

    def test_exact_and_counter_events_merge_and_are_counted_by_source(self):
        # Two frame-timed events and one counter event of the same AP within
        # the burst window: one episode, rule B on the floor of 3.
        bursts = [frame(0), burst(150), frame(300, poll_seconds=305)]
        ep = df.build_episodes([], bursts, gap_s=300)[0]
        self.assertEqual((ep.first_ts, ep.last_ts), (at(0), at(300)))
        df.assess(ep, self.params, self.baseline, self.ap)
        self.assertTrue(ep.rule_b)
        self.assertEqual(ep.max_in_window, 3)
        f = df.make_finding(ep, self.params)
        b = f.evidence["bursts"]
        self.assertEqual(b["sources"], {"last": 2, "counter": 1})
        self.assertEqual(b["times"], [iso(at(0)), iso(at(150)), iso(at(300))])
        self.assertEqual(b["polls"], [iso(at(12)), iso(at(150)), iso(at(305))])
        self.assertEqual(f.evidence["version"], 2)

    def test_frame_time_starts_the_episode_not_the_poll(self):
        # The exact rule dates the event by the frame, up to a poll earlier
        # than the observation that carried it.
        ep = df.build_episodes([alert(1, 10)], [frame(2, poll_seconds=31)], gap_s=300)[0]
        self.assertEqual(ep.first_ts, at(2))
        self.assertEqual(ep.last_ts, at(10))

    def test_busy_ap_baseline_raises_the_threshold(self):
        bursts = [burst(0), burst(200), burst(400)]
        ep = df.build_episodes([], bursts, gap_s=300)[0]
        busy = dict(self.baseline, bursts=240, active_hours=120.0)      # 2 bursts/h
        df.assess(ep, self.params, busy, self.ap)
        self.assertEqual(ep.threshold, 5)
        self.assertFalse(ep.emitted)

    def test_unknown_ap_has_no_baseline_and_no_rule_b(self):
        ep = df.build_episodes([alert(1, 0, device_key=None, bssid="02:00:00:00:00:01")], [], 300)[0]
        df.assess(ep, self.params, None, None)
        self.assertTrue(ep.rule_a)
        self.assertEqual(ep.threshold, 3)
        f = df.make_finding(ep, self.params)
        self.assertIsNone(f.device_key)
        self.assertEqual(f.subject, "02:00:00:00:00:01")
        self.assertIsNone(f.evidence["baseline"]["active_hours"])

    def test_params_from_cli(self):
        import argparse
        p = argparse.ArgumentParser()
        df.add_arguments(p)
        args = p.parse_args(["--lookback", "36h", "--headers", "deauthflood, bcastdiscon", "--gap", "120"])
        params = Params.from_args(args)
        self.assertEqual(params.lookback_s, 36 * 3600)
        self.assertEqual(params.headers, ("DEAUTHFLOOD", "BCASTDISCON"))
        self.assertEqual(params.gap_s, 120)
        self.assertEqual(params.as_dict()["trigger_headers"], ["DEAUTHFLOOD", "BCASTDISCON"])


if __name__ == "__main__":
    unittest.main()
