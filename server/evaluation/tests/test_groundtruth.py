"""Tests for the ground-truth loader - synthetic CSV, no pcap/tshark."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from evaluation import groundtruth as gt                     # noqa: E402
from evaluation.groundtruth import GroundTruthError, frame_count_ok, load_trials  # noqa: E402

HEADER = ("trial_id,type,variant,channel_config,target_bssid,start_utc,stop_utc,"
          "frames_claimed,pcap_file,notes,first_frame_utc,last_frame_utc,pcap_frames,frame_count_ok")


def write_csv(body):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(HEADER + "\n" + body)
    return path


class LoadTest(unittest.TestCase):
    def test_valid_attack_and_clean(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,4000,cap/L01.pcap,,,,,\n"
            "C1,deauth_flood,clean,locked,,2026-09-24T09:10:00Z,2026-09-24T09:13:00Z,,,,,,\n")
        try:
            trials = load_trials(path)
            self.assertEqual([t.trial_id for t in trials], ["L01", "C1"])
            self.assertTrue(trials[0].is_attack)
            self.assertEqual(trials[0].target_bssid, "AA:BB:CC:00:00:01")
            self.assertEqual(trials[0].frames_claimed, 4000)
            self.assertFalse(trials[1].is_attack)
            self.assertIsNone(trials[1].target_bssid)
        finally:
            os.remove(path)

    def test_bssid_uppercased(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,aa:bb:cc:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,10,,,,,,\n")
        try:
            self.assertEqual(load_trials(path)[0].target_bssid, "AA:BB:CC:00:00:01")
        finally:
            os.remove(path)

    def test_attack_without_bssid_is_rejected(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,10,,,,,,\n")
        try:
            with self.assertRaises(GroundTruthError):
                load_trials(path)
        finally:
            os.remove(path)

    def test_bad_variant_rejected(self):
        path = write_csv(
            "L01,deauth_flood,turbo,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,10,,,,,,\n")
        try:
            with self.assertRaises(GroundTruthError):
                load_trials(path)
        finally:
            os.remove(path)

    def test_stop_before_start_rejected(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:03:00Z,2026-09-24T09:00:00Z,10,,,,,,\n")
        try:
            with self.assertRaises(GroundTruthError):
                load_trials(path)
        finally:
            os.remove(path)

    def test_duplicate_trial_id_rejected(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,10,,,,,,\n"
            "L01,deauth_flood,slow,locked,AA:BB:CC:00:00:01,2026-09-24T09:10:00Z,2026-09-24T09:13:00Z,10,,,,,,\n")
        try:
            with self.assertRaises(GroundTruthError):
                load_trials(path)
        finally:
            os.remove(path)

    def test_comment_and_blank_rows_skipped(self):
        path = write_csv(
            "# Authorisation: own AP, operator X, supervisor notified 2026-09-23,,,,,,,,,,\n"
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,10,,,,,,\n")
        try:
            self.assertEqual([t.trial_id for t in load_trials(path)], ["L01"])
        finally:
            os.remove(path)

    def test_enriched_columns_loaded(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:03:00Z,"
            "4000,cap/L01.pcap,,2026-09-24T09:00:01Z,2026-09-24T09:02:59Z,3980,ok\n")
        try:
            t = load_trials(path)[0]
            self.assertEqual(t.pcap_frames, 3980)
            self.assertEqual(t.first_frame.hour, 9)
            self.assertEqual(t.anchor, t.first_frame)
        finally:
            os.remove(path)


class OverlapAndToleranceTest(unittest.TestCase):
    def test_overlaps_detected(self):
        path = write_csv(
            "L01,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:00:00Z,2026-09-24T09:05:00Z,10,,,,,,\n"
            "L02,deauth_flood,fast,locked,AA:BB:CC:00:00:01,2026-09-24T09:04:00Z,2026-09-24T09:08:00Z,10,,,,,,\n")
        try:
            self.assertEqual(gt.overlaps(load_trials(path)), [("L01", "L02")])
        finally:
            os.remove(path)

    def test_frame_count_tolerance(self):
        self.assertEqual(frame_count_ok(4000, 3980), "ok")     # within 15%
        self.assertEqual(frame_count_ok(4000, 1000), "MISMATCH")
        self.assertEqual(frame_count_ok(10, 12), "ok")         # within the abs floor of 5
        self.assertEqual(frame_count_ok(None, 100), "")


if __name__ == "__main__":
    unittest.main()
