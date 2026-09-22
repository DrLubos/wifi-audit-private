"""Tests for the evaluation join and statistics - synthetic rows, no database."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from evaluation import metrics, report                       # noqa: E402
from evaluation.groundtruth import Trial                     # noqa: E402
from evaluation.metrics import Detection, evaluate, match_trial, wilson_ci  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 24, 9, 0, 0, tzinfo=UTC)
BSSID = "AA:BB:CC:00:00:01"
OTHER = "AA:BB:CC:00:00:09"


def at(sec):
    return T0 + timedelta(seconds=sec)


def trial(tid, variant, cfg, start_s, dur_s=180, bssid=BSSID, first_frame_s=None):
    t = Trial(trial_id=tid, type="deauth_flood", variant=variant, channel_config=cfg,
              target_bssid=(bssid if variant != "clean" else None),
              start=at(start_s), stop=at(start_s + dur_s))
    if first_frame_s is not None:
        t.first_frame = at(first_frame_s)
    return t


def det(id_, bssid, start_s, severity="high", rule="A+B", last=8, counter=0,
        first_burst_s=None, alert_first_s=None, mgmt_ratio=None, version=2):
    rules = {"A": {"alerts": True, "bursts": False},
             "B": {"alerts": False, "bursts": True},
             "A+B": {"alerts": True, "bursts": True}}[rule]
    ev = {
        "detector": "deauth_flood", "version": version,
        "window_start": _iso(at(start_s)), "window_end": _iso(at(start_s + 120)),
        "rules": rules,
        "alerts": {"first_ts": _iso(at(alert_first_s)) if alert_first_s is not None else None},
        "bursts": {"sources": {"last": last, "counter": counter},
                   "times": [_iso(at(first_burst_s))] if first_burst_s is not None else []},
        "observed": {"mgmt_ratio_to_median": mgmt_ratio},
        "ap": {"bssid": bssid},
    }
    return Detection.from_row({"id": id_, "mac": bssid, "ssid": "TEST", "device_key": None,
                               "severity": severity, "evidence": ev})


def _iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class WilsonTest(unittest.TestCase):
    def test_zero_trials(self):
        self.assertEqual(wilson_ci(0, 0), (None, None))

    def test_all_success_interval_below_one(self):
        lo, hi = wilson_ci(10, 10)
        self.assertGreater(lo, 0.6)
        self.assertLessEqual(hi, 1.0)
        self.assertLess(lo, 1.0)          # honest: not a point estimate of 100%

    def test_half(self):
        lo, hi = wilson_ci(5, 10)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)


class MatchTest(unittest.TestCase):
    def test_matches_same_bssid_in_window(self):
        t = trial("L01", "fast", "locked", 0)
        d = det(1, BSSID, 5, first_burst_s=5)
        self.assertEqual([x.id for x in match_trial(t, [d])], [1])

    def test_wrong_bssid_does_not_match(self):
        t = trial("L01", "fast", "locked", 0)
        self.assertEqual(match_trial(t, [det(1, OTHER, 5, first_burst_s=5)]), [])

    def test_outside_window_does_not_match(self):
        t = trial("L01", "fast", "locked", 0, dur_s=180)
        # a detection dated 5 min after stop is beyond MATCH_SLACK_POST
        self.assertEqual(match_trial(t, [det(1, BSSID, 480, first_burst_s=480)]), [])

    def test_pre_slack_allows_small_clock_skew(self):
        t = trial("L01", "fast", "locked", 100)
        d = det(1, BSSID, 90, first_burst_s=90)          # 10 s before start, within 15 s PRE
        self.assertEqual([x.id for x in match_trial(t, [d])], [1])

    def test_clean_trial_never_matches(self):
        t = trial("C1", "clean", "locked", 0)
        self.assertEqual(match_trial(t, [det(1, BSSID, 5, first_burst_s=5)]), [])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        # 3 fast-locked trials spaced 600 s; 2 detected, 1 missed.
        self.trials = [trial("L01", "fast", "locked", 0, first_frame_s=1),
                       trial("L02", "fast", "locked", 600, first_frame_s=601),
                       trial("L03", "fast", "locked", 1200, first_frame_s=1201)]
        self.dets = [det(1, BSSID, 3, first_burst_s=3),
                     det(2, BSSID, 603, first_burst_s=603)]

    def test_recall_cell(self):
        cells, per_trial, fps, strays = evaluate(self.trials, self.dets, "deauth_flood")
        cell = cells[("fast", "locked")]
        self.assertEqual((cell.n, cell.detected), (3, 2))
        self.assertAlmostEqual(cell.recall, 2 / 3)
        self.assertEqual(fps, [])
        self.assertEqual(strays, [])
        self.assertEqual(sum(1 for r in per_trial if r["detected"]), 2)
        missed = [r for r in per_trial if not r["detected"]]
        self.assertEqual([r["trial_id"] for r in missed], ["L03"])

    def test_latency_uses_frame_anchor_when_available(self):
        cells, per_trial, _, _ = evaluate(self.trials, self.dets, "deauth_flood")
        row = next(r for r in per_trial if r["trial_id"] == "L01")
        # evidence first burst at +3 s, true first frame at +1 s -> 2 s frame-anchor
        self.assertAlmostEqual(row["latency_frame_s"], 2.0)
        self.assertAlmostEqual(row["latency_emit_s"], 3.0)   # minus logged start (+0)

    def test_false_positive_on_other_ap_outside_guard(self):
        # a detection on OTHER, 5000 s out, far from every trial guard band
        dets = self.dets + [det(9, OTHER, 5000, first_burst_s=5000)]
        _, _, fps, _ = evaluate(self.trials, dets, "deauth_flood")
        self.assertEqual([d.id for d in fps], [9])

    def test_other_ap_inside_guard_is_not_a_fp(self):
        dets = self.dets + [det(9, OTHER, 60, first_burst_s=60)]   # during L01 +guard
        _, _, fps, _ = evaluate(self.trials, dets, "deauth_flood")
        self.assertEqual(fps, [])

    def test_test_bssid_unmatched_is_a_stray_not_a_fp(self):
        dets = self.dets + [det(9, BSSID, 5000, first_burst_s=5000)]
        _, _, fps, strays = evaluate(self.trials, dets, "deauth_flood")
        self.assertEqual(fps, [])
        self.assertEqual([d.id for d in strays], [9])

    def test_pick_prefers_higher_severity(self):
        dets = [det(1, BSSID, 3, severity="low", rule="A", first_burst_s=3),
                det(2, BSSID, 4, severity="high", rule="A+B", first_burst_s=4)]
        _, per_trial, _, _ = evaluate([self.trials[0]], dets, "deauth_flood")
        self.assertEqual(per_trial[0]["severity"], "high")
        self.assertEqual(per_trial[0]["n_detections"], 2)


class PenaltyAndCounterfactualTest(unittest.TestCase):
    def test_channel_hop_penalty(self):
        trials = [trial("L01", "slow", "locked", 0, first_frame_s=1),
                  trial("H01", "slow", "hopping", 6000, first_frame_s=6001)]
        dets = [det(1, BSSID, 3, severity="medium", rule="B", last=6, first_burst_s=3)]  # locked detected
        cells, *_ = evaluate(trials, dets, "deauth_flood")
        pen = metrics.channel_hop_penalty(cells)["slow"]
        self.assertEqual(pen, (1.0, 0.0, 1.0))               # locked 1/1, hopping 0/1

    def test_counterfactual_table_slow_collapses(self):
        trials = [trial("L01", "slow", "locked", 0, first_frame_s=1)]
        v2 = evaluate(trials, [det(1, BSSID, 3, severity="medium", rule="B",
                                   last=8, counter=0, first_burst_s=3)], "deauth_flood")[0]
        counter = evaluate(trials, [], "deauth_flood")[0]     # counter-only found nothing
        md = report.counterfactual_table(v2, counter)
        self.assertIn("slow", md)
        self.assertIn("| slow | 1/1 | 100% | 0/1 | 0% |", md)   # v2 caught it, counter-only missed


if __name__ == "__main__":
    unittest.main()
