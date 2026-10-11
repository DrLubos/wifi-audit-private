"""Row -> response helpers of the v2 endpoints (app/v2/shape.py). Standard library only."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import periods, shape  # noqa: E402

UTC = timezone.utc
TZ = "Europe/Bratislava"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def hour(h, polls=120, capture=120, gap=0.0, sensor_id=1):
    return {"sensor_id": sensor_id, "hour": h, "polls": polls, "capture_polls": capture, "gap_s": gap,
            "limited_polls": 0}


SENSOR = {"id": 1, "name": "pi-fri", "first_poll": utc(2026, 10, 9, 0, 0), "last_poll": utc(2026, 10, 10, 15, 30)}


class Coverage(unittest.TestCase):
    def test_hour_capture(self):
        h = utc(2026, 10, 10, 12)
        self.assertEqual(shape.hour_capture_s(hour(h), SENSOR["first_poll"], SENSOR["last_poll"]), 3600)
        self.assertEqual(shape.hour_capture_s(hour(h, gap=600), SENSOR["first_poll"], SENSOR["last_poll"]), 3000)
        # half the polls not collecting (the adapter loss: ok, but no devices)
        self.assertEqual(shape.hour_capture_s(hour(h, capture=60), SENSOR["first_poll"], SENSOR["last_poll"]), 1800)
        self.assertEqual(shape.hour_capture_s(hour(h, polls=0, capture=0), SENSOR["first_poll"],
                                              SENSOR["last_poll"]), 0)

    def test_first_and_last_hour_are_clamped(self):
        # last poll 15:30: the hour counts to 15:30:30, not to 16:00
        self.assertEqual(shape.hour_capture_s(hour(utc(2026, 10, 10, 15), polls=61, capture=61),
                                              SENSOR["first_poll"], SENSOR["last_poll"]), 1830)
        # first poll 13:53:07 with 413 s of gap after it (2026-09-15, the real first hour)
        s = {"first_poll": utc(2026, 9, 15, 13, 53, 7), "last_poll": utc(2026, 10, 10)}
        self.assertEqual(shape.hour_capture_s(hour(utc(2026, 9, 15, 13), polls=1, capture=1, gap=413),
                                              s["first_poll"], s["last_poll"]), 0)

    def test_bucket_shares(self):
        p = periods.period("24h", TZ, utc(2026, 10, 10, 15, 30))
        rows = [hour(utc(2026, 10, 10, 13)), hour(utc(2026, 10, 10, 14), gap=1800),
                hour(utc(2026, 10, 10, 15), polls=60, capture=60)]
        cov = shape.coverage(p, rows, [SENSOR])["pi-fri"]
        self.assertEqual(len(cov), 24)
        self.assertEqual(cov[-3:], [1.0, 0.5, 1.0])    # the current hour: 30.5 min of 30 -> capped at 1
        self.assertEqual(cov[0], 0.0)                  # no row: not collecting
        buckets, on = shape.buckets(p, {"pi-fri": cov})
        self.assertEqual(buckets[-1]["coverage"], {"pi-fri": 1.0})
        self.assertEqual(buckets[-1]["start"], "2026-10-10T15:00:00Z")
        self.assertEqual(on[-3:], [True, True, True])
        self.assertFalse(on[0])
        self.assertEqual(shape.series([5] * 24, on)[-4:], [None, 5, 5, 5])

    def test_window_share(self):
        rows = [hour(utc(2026, 10, 10, 13)), hour(utc(2026, 10, 10, 14), capture=0),
                hour(utc(2026, 10, 10, 13), sensor_id=2)]
        self.assertEqual(shape.window_share(rows, SENSOR, utc(2026, 10, 10, 13), utc(2026, 10, 10, 15)), 0.5)
        self.assertIsNone(shape.window_share(rows, SENSOR, utc(2026, 10, 10, 13), utc(2026, 10, 10, 13)))


class Clients(unittest.TestCase):
    def test_peaks_shares_and_missing_load(self):
        p = periods.period("24h", TZ, utc(2026, 10, 10, 15, 30))
        h = lambda x: utc(2026, 10, 10, x)  # noqa: E731
        captured = {h(12), h(13), h(14), h(15)}
        rows = [{"hour": h(12), "clients": 40}, {"hour": h(13), "clients": None}, {"hour": h(15), "clients": 55}]
        peak, share, best = shape.clients_by_bucket(p, rows, captured)
        self.assertEqual(peak[-4:], [40, 0, None, 55])     # 13:00 load but no managed AP; 14:00 none recorded
        self.assertEqual(share[-4:], [1.0, 1.0, 0.0, 1.0])
        self.assertEqual(share[0], None)                    # not captured
        self.assertEqual(best, (55, h(15)))

    def test_bucket_of(self):
        p = periods.period("7d", TZ, utc(2026, 10, 10, 15, 52))
        self.assertEqual(shape.bucket_of(p, p.starts[0]), 1)
        self.assertEqual(shape.bucket_of(p, p.ends[-1] - timedelta(seconds=1)), 28)
        self.assertIsNone(shape.bucket_of(p, p.ends[-1]))


class StatusAndLists(unittest.TestCase):
    def test_sensor_status(self):
        now = utc(2026, 10, 11, 12)
        self.assertEqual(shape.sensor_status(None, None, now, 15), ("no_data", None))
        self.assertEqual(shape.sensor_status(now - timedelta(minutes=10), now, now, 15), ("online", 600))
        self.assertEqual(shape.sensor_status(now - timedelta(minutes=16), now, now, 15), ("offline", 960))
        self.assertEqual(shape.sensor_status(None, now, now, 15), ("offline", None))

    def test_security_rows_fixed_order(self):
        rows = shape.security_rows([{"level": "strong", "n": 1}, {"level": "open", "n": 3}])
        self.assertEqual([r["level"] for r in rows], ["open", "legacy", "psk_no_mfp", "strong", "unknown"])
        self.assertEqual([r["aps"] for r in rows], [3, 0, 0, 1, 0])
        self.assertEqual(rows[0]["share"], 0.75)
        self.assertEqual(shape.security_rows([])[0]["share"], 0.0)

    def test_compliance_and_attention(self):
        nets = [{"network": "A", "bssids": 3, "failing": 1, "reason": "MFP not required"},
                {"network": "B", "bssids": 5, "failing": 0, "reason": None},
                {"network": "C", "bssids": 2, "failing": 2, "reason": "not Enterprise"},
                {"network": "D", "bssids": 0, "failing": 0, "reason": None}]
        summary, findings = shape.compliance(nets)
        self.assertEqual((summary["met"], summary["total"]), (1, 4))
        self.assertEqual(summary["first_problem"], {"network": "A", "reason": "MFP not required",
                                                    "failing": 1, "bssids": 3})
        self.assertEqual([f["severity"] for f in findings], ["low", "medium", "low"])
        self.assertEqual(findings[2]["title"], "Managed network D: not seen by the selected sensors")
        ev = lambda i, sev, kind="kismet": {  # noqa: E731
            "id": i, "severity": sev, "kind": kind, "type": "T", "summary": "s%d" % i, "bssid": None,
            "ssid": None, "sensor": "pi-fri", "ts": None, "ts_end": None, "total": 3}
        items, total = shape.attention([ev(9, "high"), ev(7, "medium", "sensor"), ev(5, "medium")], findings)
        self.assertEqual(total, 6)
        self.assertEqual([(x["severity"], x.get("event_id")) for x in items],
                         [("high", 9), ("medium", 7), ("medium", 5), ("medium", None), ("low", None), ("low", None)])
        self.assertEqual(items[1]["link"], "/sensors")
        self.assertEqual(items[0]["link"], "/events?id=9")
        self.assertEqual(len(shape.attention([ev(1, "high")] * 20, [], limit=10)[0]), 10)

    def test_severity_counts_and_parse(self):
        self.assertEqual(shape.severity_counts([{"severity": "high", "n": 2}]),
                         {"critical": 0, "high": 2, "medium": 0, "low": 0, "info": 0})
        self.assertEqual(shape.parse_ts("2026-10-06T19:32:45+00:00"), utc(2026, 10, 6, 19, 32, 45))
        self.assertIsNone(shape.parse_ts(None))


if __name__ == "__main__":
    unittest.main()
