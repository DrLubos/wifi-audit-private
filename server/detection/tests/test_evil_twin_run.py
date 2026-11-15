"""The evil_twin database phase against a fake connection, and its rows in the
sink: the two kinds of finding of one AP are two rows, a membership finding
(a) keeps its first start across runs, (a) and (b) corroborate each other only
when both are reported and present at the same time, and "trusted" is the
operator's whitelist of the dashboard. No database."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from detection import common, deauth_flood as df, detections, evil_twin as et   # noqa: E402
from test_run import FakeConn, FakeResult                                        # noqa: E402

UTC = timezone.utc
KT = "4202770D00000000_111111EA58EC"       # EC:58:EA:11:11:11, a managed AP of IK-WIFI
KU = "4202770D00000000_3333335544AA"       # AA:44:55:33:33:33, an unknown BSSID advertising IK-WIFI
KP = "4202770D00000000_222222222226"       # 26:22:22:22:22:22, a phone hotspot named IK-WIFI
SENSOR = {"id": 1, "name": "pi-fri"}


def T(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def meta_row(key, bssid, trusted):
    return dict(device_key=key, bssid=bssid, ssid="IK-WIFI", manuf=None, crypt="Open", oui=bssid[:8].lower() + ":00:00:00",
                random_bssid=False, global_sibling=False, rssi_median=-60.0, rssi_robust_sd=1.0, base_n_obs=9000,
                base_n_days=9, base_n_floor=0, base_from=T("2026-11-01"), base_to=T("2026-11-09"), trusted=trusted,
                hw_from=None, hw_to=None)


def inv_row(key, bssid, trusted, first, last, n, random_bssid=False, crypt="Open"):
    return dict(device_key=key, bssid=bssid, oui=bssid[:8].lower() + ":00:00:00", manuf=None, ssid="IK-WIFI",
                crypt=crypt, random_bssid=random_bssid, trusted=trusted, first_seen=T("2026-09-15"),
                last_seen=T(last), win_obs=n, win_first=T(first), win_last=T(last), win_median=-70.0)


class EtConn(FakeConn):
    """The evil_twin statements over given rows; the sink statements of test_run.FakeConn."""

    def __init__(self, meta, inventory, readings):
        super().__init__(alerts=[], bursts=[])
        self.meta, self.inventory, self.readings = meta, inventory, readings

    def execute(self, sql, params=None):
        if sql is et.AP_META_SQL:
            self.calls.append((sql, params))
            return FakeResult(self.meta)
        if sql is et.INVENTORY_SQL:
            self.calls.append((sql, params))
            return FakeResult(self.inventory)
        if sql in (et.HISTORY_SQL, et.ALERTS_SQL, et.CHANNEL_CHANGES_SQL):
            return FakeResult([])
        return super().execute(sql, params)


def deviation(key, start, polls=30, level=-73):
    """(device_key, ts, rssi): the baseline level around `polls` readings at `level`."""
    return [(key, start + timedelta(seconds=30 * i), level if 0 <= i < polls else -60) for i in range(-40, polls + 30)]


class RunTest(unittest.TestCase):
    def setUp(self):
        self.scans = []
        self._stream = et._stream_readings

        def stream(conn, sql, args):
            self.scans.append((args["scan_from"], args["scan_to"]))
            rows = sorted(conn.readings, key=lambda r: (r[0], r[1]))
            return iter([r for r in rows if args["scan_from"] <= r[1] < args["scan_to"]])
        et._stream_readings = stream

    def tearDown(self):
        et._stream_readings = self._stream

    def analyse(self, conn, frm, to, **params):
        return et.analyse(conn, SENSOR, T(frm), T(to), et.Params(**params), quiet=True)

    def test_a_and_b_of_one_ap_are_two_rows(self):
        # the unknown BSSID deviates from its own baseline twice and is an (a) finding too
        conn = EtConn([meta_row(KT, "EC:58:EA:11:11:11", True), meta_row(KU, "AA:44:55:33:33:33", False)],
                      [inv_row(KT, "EC:58:EA:11:11:11", True, "2026-11-12 23:55:10", "2026-11-14 00:04:40", 2800),
                       inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-12 23:55:10", "2026-11-14 00:04:40", 2800)],
                      deviation(KU, T("2026-11-13 03:00:00")) + deviation(KU, T("2026-11-13 15:00:00")))
        for run in (1, 2):
            _, _, findings, info = self.analyse(conn, "2026-11-13", "2026-11-14")
            self.assertEqual((info["findings_b"], info["findings_a"]), (2, 1))
            for f in findings:
                detections.write(conn, 1, f, 300)
            self.assertEqual([f.action for f in findings], ["insert" if run == 1 else "update"] * 3)
            self.assertEqual(sorted((r["evidence"]["signal"], r["evidence"]["window_start"][11:16]) for r in conn.stored),
                             [("a", "23:55"), ("b", "03:02"), ("b", "15:02")])
        self.assertEqual(sorted(r["evidence"]["runs"] for r in conn.stored), [2, 2, 2])

    def test_a_membership_finding_keeps_its_first_start(self):
        rows = [inv_row(KT, "EC:58:EA:11:11:11", True, "2026-11-12 23:55:10", "2026-11-14 00:04:40", 2800)]
        conn = EtConn([], rows + [inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-12 23:55:10", "2026-11-14 00:04:40", 2800)], [])
        _, _, findings, _ = self.analyse(conn, "2026-11-13", "2026-11-14")
        detections.write(conn, 1, findings[0], 300)
        # the next day's run sees the same BSSID through its own window
        conn.inventory = rows + [inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-13 23:55:10", "2026-11-15 00:04:40", 2800)]
        _, _, findings, _ = self.analyse(conn, "2026-11-14", "2026-11-15")
        detections.write(conn, 1, findings[0], 300)
        self.assertEqual(findings[0].action, "update")
        self.assertEqual(len(conn.stored), 1)
        ev = conn.stored[0]["evidence"]
        self.assertEqual(conn.stored[0]["ts"], T("2026-11-12 23:55:10"))          # not the second run's 11-13 23:55
        self.assertEqual((ev["window_start"], ev["window_end"]), ("2026-11-12T23:55:10.000Z", "2026-11-15T00:04:40.000Z"))

    def test_corroboration_needs_a_reportable_unknown_at_the_same_time(self):
        trusted = [meta_row(KT, "EC:58:EA:11:11:11", True)]
        t_inv = inv_row(KT, "EC:58:EA:11:11:11", True, "2026-11-02 00:00:00", "2026-11-14 00:00:00", 9000)
        readings = deviation(KT, T("2026-11-13 10:00:00"))                  # 13 dB weaker for 15 minutes: medium

        def severity(inventory):
            eps, unknowns, findings, _ = self.analyse(EtConn(trusted, [t_inv] + inventory, readings),
                                                      "2026-11-02", "2026-11-14")
            b = next(f for f in findings if f.signal == "b")
            return b.severity, b.evidence["rules"]["unknown_bssid"], [f.severity for f in findings if f.signal == "a"]
        self.assertEqual(severity([]), ("medium", False, []))
        # a phone hotspot with the SSID, three polls eleven days earlier: not reported, corroborates nothing
        phone = inv_row(KP, "26:22:22:22:22:22", False, "2026-11-02 08:00:00", "2026-11-02 08:01:00", 3, random_bssid=True)
        self.assertEqual(severity([phone]), ("medium", False, []))
        # a sustained unknown BSSID that left two days before the deviation: reported, but not at that time
        gone = inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-08 08:00:00", "2026-11-11 08:00:00", 2000)
        self.assertEqual(severity([gone]), ("medium", False, ["medium"]))
        # the same one present during the deviation: both findings go up a level
        there = inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-12 08:00:00", "2026-11-13 20:00:00", 2000)
        self.assertEqual(severity([there]), ("high", True, ["high"]))

    def test_one_deviating_window_corroborates_nothing(self):
        trusted = [meta_row(KT, "EC:58:EA:11:11:11", True)]
        inventory = [inv_row(KT, "EC:58:EA:11:11:11", True, "2026-11-02 00:00:00", "2026-11-14 00:00:00", 9000),
                     inv_row(KU, "AA:44:55:33:33:33", False, "2026-11-12 08:00:00", "2026-11-13 20:00:00", 2000)]
        # deviating windows, but fewer than the persistence rule asks for - no (b) finding
        eps, unknowns, findings, info = self.analyse(
            EtConn(trusted, inventory, deviation(KT, T("2026-11-13 10:00:00"), polls=8)), "2026-11-02", "2026-11-14",
            persistence=30)
        self.assertTrue(eps and not any(e.emitted for e in eps))
        self.assertEqual([(f.signal, f.severity, f.evidence["rules"]["rssi_deviation"]) for f in findings],
                         [("a", "medium", False)])

    def test_a_stored_b_episode_widens_the_rssi_scan_only(self):
        conn = EtConn([meta_row(KT, "EC:58:EA:11:11:11", True)],
                      [inv_row(KT, "EC:58:EA:11:11:11", True, "2026-11-13 00:00:00", "2026-11-14 00:00:00", 2800)],
                      deviation(KT, T("2026-11-13 11:50:00"), polls=40))             # 11:50 .. 12:10
        _, _, findings, _ = self.analyse(conn, "2026-11-13 10:00:00", "2026-11-13 12:00:00")
        detections.write(conn, 1, findings[0], 300)
        first = conn.stored[0]["evidence"]["window_start"]
        _, _, findings, _ = self.analyse(conn, "2026-11-13 12:00:00", "2026-11-13 14:00:00")
        detections.write(conn, 1, findings[0], 300)
        self.assertEqual(self.scans[-1][0], T(first.replace("Z", "").replace("T", " ")[:19]) - timedelta(seconds=300))
        self.assertEqual((len(conn.stored), conn.stored[0]["evidence"]["window_start"]), (1, first))
        self.assertEqual(conn.stored[0]["evidence"]["window_end"][11:19], "12:12:00")     # past the first run's end
        inv_params = [p for sql, p in conn.calls if sql is et.INVENTORY_SQL][-1]
        self.assertEqual(inv_params["scan_from"], T("2026-11-13 11:55:00"))           # (a): the window's own range


class TrustedSourceTest(unittest.TestCase):
    def test_trusted_is_the_dashboards_managed_classification_or_the_old_flag(self):
        self.assertIn("v.classification = 'managed'", common.MANAGED_CTE)
        for sql in (et.AP_META_SQL, et.INVENTORY_SQL, df.AP_INFO_SQL):
            self.assertTrue(sql.startswith("WITH managed AS MATERIALIZED"), sql[:40])
            self.assertIn("LEFT JOIN managed m ON m.device_key =", sql)
            self.assertIn("OR m.device_key IS NOT NULL) AS trusted", sql)


class SinkTest(unittest.TestCase):
    def test_the_overlap_lookup_matches_the_signal(self):
        for sql in (detections._OVERLAP_SQL, detections._SPAN_SQL):
            self.assertIn("coalesce(evidence->>'signal', '') = %(signal)s", sql)
        self.assertNotIn("update", detections._SPAN_SQL.lower())               # read-only: runs in the analysis

    def test_widen(self):
        conn = FakeConn(alerts=[], bursts=[])
        a, b = T("2026-11-13 11:55:00"), T("2026-11-13 14:05:00")
        self.assertEqual(detections.widen(conn, 1, "deauth_flood", a, b, 300), (a, b))
        conn.stored.append(dict(type="deauth_flood", subject="K", evidence={}, ts=a,
                                window_start=T("2026-11-13 11:50:00"), window_end=T("2026-11-13 12:04:48")))
        self.assertEqual(detections.widen(conn, 1, "deauth_flood", a, b, 300), (T("2026-11-13 11:45:00"), b))
        self.assertEqual(detections.widen(conn, 1, "deauth_flood", a, b, 300, signal="b"), (a, b))
        self.assertEqual(detections.widen(conn, 1, "evil_twin", a, b, 300), (a, b))


if __name__ == "__main__":
    unittest.main()
