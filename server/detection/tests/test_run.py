"""End-to-end run of the deauth_flood database phase against a fake connection
that replays the rows the real seeded database returned (2026-09-15 .. 09-21):
the two organic DEAUTHFLOOD pairs, the burst polls of their APs, the baseline
rows and the AP identities. Exercises analyse() -> report() -> detections.write()
without psycopg, including the SQL parameter dictionaries."""

import argparse
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import common, deauth_flood as df, detections  # noqa: E402


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


K1 = "4202770D00000000_DC8955EA58EC"      # EC:58:EA:55:89:DC, event 1
K2 = "4202770D00000000_8C4554EA58EC"      # EC:58:EA:54:45:8C, event 2
K3 = "4202770D00000000_B88E302B5FBE"      # the phone with the DISCONCODEINVALID alert

ALERT_ROWS = [
    dict(id=21, ts=ts("2026-09-19T18:51:36.024864Z"), header="DISCONCODEINVALID", bssid="BE:5F:2B:30:8E:B8",
         source_mac="A6:C7:2E:78:26:49", dest_mac="BE:5F:2B:30:8E:B8", device_key=K3,
         ssid="Redmi Note 13 Pro", direction="to_ap"),
    dict(id=25, ts=ts("2026-09-20T09:01:36.101184Z"), header="DEAUTHFLOOD", bssid="EC:58:EA:54:45:8C",
         source_mac="E2:EF:CD:70:E2:E5", dest_mac="EC:58:EA:54:45:8C", device_key=K2, ssid="IK-WIFI",
         direction="to_ap"),
    dict(id=26, ts=ts("2026-09-20T09:01:36.141224Z"), header="DEAUTHFLOOD", bssid="EC:58:EA:54:45:8C",
         source_mac="E2:EF:CD:70:E2:E5", dest_mac="EC:58:EA:54:45:8C", device_key=K2, ssid="IK-WIFI",
         direction="to_ap"),
    dict(id=12, ts=ts("2026-09-18T07:42:56.533621Z"), header="DEAUTHFLOOD", bssid="EC:58:EA:55:89:DC",
         source_mac="1C:57:DC:7B:0C:DF", dest_mac="EC:58:EA:55:89:DC", device_key=K1, ssid="IK-WIFI",
         direction="to_ap"),
    dict(id=13, ts=ts("2026-09-18T07:42:56.546082Z"), header="DEAUTHFLOOD", bssid="EC:58:EA:55:89:DC",
         source_mac="1C:57:DC:7B:0C:DF", dest_mac="EC:58:EA:55:89:DC", device_key=K1, ssid="IK-WIFI",
         direction="to_ap"),
]

# The burst polls of the two APs in the seeded data (values as polled).
BURST_ROWS = [
    dict(device_key=K1, ts=ts("2026-09-17T22:34:30Z"), prev=0, cur=1, since_prev_s=30, mgmt_delta=11),
    dict(device_key=K1, ts=ts("2026-09-18T07:43:01Z"), prev=1, cur=4, since_prev_s=30, mgmt_delta=18),
    dict(device_key=K2, ts=ts("2026-09-15T18:20:17Z"), prev=0, cur=2, since_prev_s=30, mgmt_delta=9),
    dict(device_key=K2, ts=ts("2026-09-17T05:18:17Z"), prev=0, cur=2, since_prev_s=30, mgmt_delta=8),
    dict(device_key=K2, ts=ts("2026-09-18T22:17:01Z"), prev=2, cur=5, since_prev_s=30, mgmt_delta=12),
    dict(device_key=K2, ts=ts("2026-09-19T16:57:27Z"), prev=0, cur=2, since_prev_s=30, mgmt_delta=7),
    dict(device_key=K2, ts=ts("2026-09-19T18:11:27Z"), prev=1, cur=2, since_prev_s=30, mgmt_delta=9),
]

BASELINE_ROWS = {
    K1: dict(device_key=K1, polls=13831, bursts=2, first_ts=ts("2026-09-15T14:55:17Z"),
             last_ts=ts("2026-09-20T12:35:58Z"), active_hours=115.2583, mgmt_med=9.0, mgmt_robust_sd=1.4826),
    K2: dict(device_key=K2, polls=13095, bursts=14, first_ts=ts("2026-09-15T14:55:17Z"),
             last_ts=ts("2026-09-20T12:35:58Z"), active_hours=109.125, mgmt_med=8.0, mgmt_robust_sd=2.9652),
}
AP_ROWS = {
    K1: dict(device_key=K1, bssid="EC:58:EA:55:89:DC", ssid="IK-WIFI", crypt="Open", mfp_req=False, trusted=False),
    K2: dict(device_key=K2, bssid="EC:58:EA:54:45:8C", ssid="IK-WIFI", crypt="Open", mfp_req=False, trusted=False),
}
EPISODE_POLLS = [
    dict(ts=ts("2026-09-18T07:42:01Z"), prev=1, cur=1, since_prev_s=30, mgmt_delta=7),
    dict(ts=ts("2026-09-18T07:42:31Z"), prev=1, cur=1, since_prev_s=30, mgmt_delta=16),
    dict(ts=ts("2026-09-18T07:43:01Z"), prev=1, cur=4, since_prev_s=30, mgmt_delta=18),
    dict(ts=ts("2026-09-18T07:43:31Z"), prev=4, cur=4, since_prev_s=30, mgmt_delta=9),
    dict(ts=ts("2026-09-18T07:44:01Z"), prev=4, cur=4, since_prev_s=30, mgmt_delta=10),
]


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeConn:
    """Dispatches on the statement text; records every call and its parameters."""

    def __init__(self, stored=None):
        self.calls = []
        self.stored = stored or []          # detections rows already "in the table"
        self.next_id = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql is df.EVENTS_SQL:
            return FakeResult([r for r in BURST_ROWS if params["scan_from"] <= r["ts"] < params["scan_to"]
                               and r["since_prev_s"] <= params["max_gap_s"]])
        if sql is df.ALERTS_SQL:
            return FakeResult([r for r in ALERT_ROWS if r["header"] in params["headers"]
                               and params["scan_from"] <= r["ts"] < params["scan_to"]])
        if sql is df.BASELINE_SQL:
            hours = 0 if params["scan_to"] <= ts("2026-09-15T13:53:00Z") else None
            rows = []
            for k in params["keys"]:
                if k in BASELINE_ROWS:
                    rows.append(dict(BASELINE_ROWS[k], active_hours=hours if hours is not None
                                     else BASELINE_ROWS[k]["active_hours"]))
            return FakeResult(rows if hours is None else [])
        if sql is df.BASELINE_ALERTS_SQL:
            return FakeResult([])
        if sql is df.EPISODE_POLLS_SQL:
            return FakeResult([r for r in EPISODE_POLLS if params["keys"] == [K1]
                               and params["scan_from"] <= r["ts"] < params["scan_to"]])
        if sql is df.AP_INFO_SQL:
            return FakeResult([AP_ROWS[k] for k in params["keys"] if k in AP_ROWS])
        if sql is detections._OVERLAP_SQL:
            hits = [r for r in self.stored if r["type"] == params["type"]
                    and r["subject"] == params["subject"]
                    and r["window_end"] >= params["start"] and r["window_start"] <= params["end"]]
            return FakeResult([dict(id=r["id"], ts=r["ts"], severity=r["severity"], acked=r["acked"],
                                    evidence=r["evidence"]) for r in hits])
        if sql is detections._INSERT_SQL:
            row = dict(params, id=self.next_id, subject=params["device_key"] or params["mac"].upper(),
                       acked=False, evidence=json.loads(params["evidence"]))
            row["window_start"] = ts(row["evidence"]["window_start"])
            row["window_end"] = ts(row["evidence"]["window_end"])
            self.stored.append(row)
            self.next_id += 1
            return FakeResult([{"id": row["id"]}])
        if sql is detections._UPDATE_SQL:
            for r in self.stored:
                if r["id"] == params["id"]:
                    r.update(ts=params["ts"], severity=params["severity"], summary=params["summary"],
                             evidence=json.loads(params["evidence"]))
                    r["window_start"] = ts(r["evidence"]["window_start"])
                    r["window_end"] = ts(r["evidence"]["window_end"])
            return FakeResult([])
        raise AssertionError("unexpected statement: %s" % sql[:60])


def args_for(argv):
    p = argparse.ArgumentParser()
    df.add_arguments(p)
    return p.parse_args(argv)


SENSOR = {"id": 1, "name": "pi-fri", "location": "FRI", "tz": "Europe/Bratislava"}


class SeededWindowTest(unittest.TestCase):
    def run_window(self, conn, frm, to, extra=()):
        args = args_for(["--from", frm, "--to", to, *extra])
        params = df.Params.from_args(args)
        f, t = common.window_from_args(args.from_, args.to)
        episodes, findings, info = df.analyse(conn, SENSOR, f, t, params, quiet=True)
        return args, params, episodes, findings, info

    def test_seeded_window_yields_the_two_organic_events(self):
        conn = FakeConn()
        args, params, episodes, findings, info = self.run_window(conn, "2026-09-15", "2026-09-21")
        self.assertEqual(info["alerts"], 4)
        self.assertEqual(info["corroborating"], 1)
        self.assertEqual(len(findings), 2)
        by_key = {f.device_key: f for f in findings}
        f1, f2 = by_key[K1], by_key[K2]
        self.assertEqual((f1.severity, f2.severity), ("low", "low"))
        self.assertEqual(f1.evidence["alerts"]["ids"], [12, 13])
        self.assertEqual(f2.evidence["alerts"]["ids"], [25, 26])
        self.assertEqual(f1.evidence["bursts"]["values"], [[1, 4]])
        self.assertEqual(f2.evidence["bursts"]["n"], 0)
        self.assertEqual(f1.evidence["rules"], {"alerts": True, "bursts": False})
        self.assertTrue(f1.evidence["baseline"]["fallback"])          # no lookback data before 09-15
        self.assertEqual(f1.evidence["baseline"]["to"], "2026-09-21T00:05:00.000Z")   # the range used
        self.assertEqual(f1.evidence["baseline"]["bursts"], 2)
        self.assertEqual(f1.evidence["observed"]["mgmt_per_poll_max"], 18.0)
        self.assertEqual(f1.evidence["observed"]["mgmt_ratio_to_median"], 2.0)
        self.assertEqual(f1.evidence["ap"]["bssid"], "EC:58:EA:55:89:DC")
        self.assertEqual(f1.ts, ts("2026-09-18T07:42:56.533621Z"))
        # the phone's DISCONCODEINVALID forms an episode that is never emitted
        phone = [e for e in episodes if e.device_key == K3]
        self.assertEqual(len(phone), 1)
        self.assertFalse(phone[0].emitted)
        # K2's five organic bursts are spread over days: five separate, unflagged episodes
        k2 = [e for e in episodes if e.device_key == K2]
        self.assertEqual(len(k2), 6)
        self.assertEqual(sum(1 for e in k2 if e.emitted), 1)

    def test_write_then_rerun_refreshes_not_duplicates(self):
        conn = FakeConn()
        args, params, episodes, findings, info = self.run_window(conn, "2026-09-15", "2026-09-21")
        for f in findings:
            detections.write(conn, 1, f, params.gap_s)
        self.assertEqual([f.action for f in findings], ["insert", "insert"])
        self.assertEqual(len(conn.stored), 2)
        # identical re-run
        _, _, _, findings2, _ = self.run_window(conn, "2026-09-15", "2026-09-21")
        for f in findings2:
            detections.write(conn, 1, f, params.gap_s)
        self.assertEqual([f.action for f in findings2], ["update", "update"])
        self.assertEqual(len(conn.stored), 2)
        self.assertEqual([r["evidence"]["runs"] for r in conn.stored], [2, 2])
        # two halves, then an overlapping window: still two rows
        for frm, to in (("2026-09-15", "2026-09-18T12:00Z"), ("2026-09-18T12:00Z", "2026-09-21"),
                        ("2026-09-18", "2026-09-20T09:01:36.12Z")):
            _, _, _, fs, _ = self.run_window(conn, frm, to)
            for f in fs:
                detections.write(conn, 1, f, params.gap_s)
        self.assertEqual(len(conn.stored), 2)
        self.assertEqual(sorted(r["evidence"]["runs"] for r in conn.stored), [4, 4])
        # a window starting mid-episode (after alert 25, before alert 26) still maps to the row
        _, _, _, fs, _ = self.run_window(conn, "2026-09-20T09:01:36.12Z", "2026-09-21")
        self.assertEqual(len(fs), 1)
        detections.write(conn, 1, fs[0], params.gap_s)
        self.assertEqual(fs[0].action, "update")
        self.assertEqual(len(conn.stored), 2)

    def test_per_ap_baseline_protects_a_busy_ap_from_a_low_floor(self):
        # K2's bursts of 2026-09-19 16:57 and 18:11 are 74 min apart: a 90-minute gap merges
        # them and a 90-minute window holds both, so a floor of 2 alone would flag them...
        conn = FakeConn()
        _, _, episodes, findings, _ = self.run_window(
            conn, "2026-09-15", "2026-09-21", ["--min-bursts", "2", "--burst-window", "5400", "--gap", "5400"])
        pair = [e for e in episodes if e.device_key == K2 and len(e.bursts) == 2]
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0].max_in_window, 2)
        # ...but the AP's own rate (14 bursts in 109 h) lifts the Poisson threshold to 4.
        self.assertEqual(pair[0].threshold, 4)
        self.assertFalse(pair[0].emitted)
        self.assertEqual(len(findings), 2)
        # A looser tail (alpha 0.05) accepts 2 in 90 min for this rate: the pair is flagged.
        conn = FakeConn()
        _, _, episodes, findings, _ = self.run_window(
            conn, "2026-09-15", "2026-09-21",
            ["--min-bursts", "2", "--burst-window", "5400", "--gap", "5400", "--alpha", "0.05"])
        near = [e for e in episodes if e.rule_b]
        self.assertEqual([(e.device_key, e.threshold, e.severity) for e in near], [(K2, 2, "medium")])
        self.assertEqual(len(findings), 3)
        self.assertTrue(near[0].finding.summary.startswith("Sustained deauth/disassoc activity on IK-WIFI"))

    def test_report_prints_table_and_json(self):
        conn = FakeConn()
        args, params, episodes, findings, info = self.run_window(conn, "2026-09-15", "2026-09-21", ["--verbose"])
        for f in findings:
            detections.write(conn, 1, f, params.gap_s)
        out, err = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = out, err
        try:
            df.report(SENSOR, ts("2026-09-15"), ts("2026-09-21"), params, episodes, findings, info, args)
            args.json = True
            df.report(SENSOR, ts("2026-09-15"), ts("2026-09-21"), params, episodes, findings, info, args)
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        text = out.getvalue()
        self.assertIn("insert #1", text)
        self.assertIn("EC:58:EA:55:89:DC", text)
        self.assertIn("counter 1 -> 4", text)
        self.assertIn("2 detections", text)
        payload = json.loads(text[text.index("["):])
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["severity"], "low")
        self.assertIn("window_start", payload[0]["evidence"])


if __name__ == "__main__":
    unittest.main()
