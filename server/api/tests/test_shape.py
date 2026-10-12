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
        # unknown security is not assessed (decided 2026-10-12): counted, never failing
        summary, f2 = shape.compliance([
            {"network": "E", "bssids": 26, "failing": 0, "reason": None, "not_assessed": 2},
            {"network": "F", "bssids": 3, "failing": 1, "reason": "not Enterprise", "not_assessed": 1},
            {"network": "G", "bssids": 0, "failing": 0, "reason": None, "not_assessed": 2}])
        self.assertEqual((summary["met"], summary["total"]), (1, 3))
        self.assertEqual([(f["network"], f["detail"]) for f in f2],
                         [("F", "1 of 3 BSSIDs, 1 not assessed"), ("G", "2 not assessed")])
        self.assertEqual(f2[1]["title"], "Managed network G: security not assessed (no beacon record)")
        ev = lambda i, sev, kind="kismet", ongoing=False: {  # noqa: E731
            "id": i, "severity": sev, "kind": kind, "type": "T", "summary": "s%d" % i, "bssid": None,
            "ssid": None, "sensor": "pi-fri", "ts": None, "ts_end": None, "total": 3, "ongoing": ongoing}
        # the api's order: the ongoing outage first, then the recent events
        items, total = shape.attention([ev(7, "medium", "sensor", True), ev(9, "high"), ev(5, "medium")], findings)
        self.assertEqual(total, 6)
        # compliance findings and the ongoing sensor problem first (by severity), then the events
        self.assertEqual([(x["severity"], x.get("event_id")) for x in items],
                         [("medium", 7), ("medium", None), ("low", None), ("low", None), ("high", 9), ("medium", 5)])
        self.assertEqual(items[0]["link"], "/sensors")
        self.assertEqual(items[4]["link"], "/events?id=9")
        self.assertEqual(len(shape.attention([ev(1, "high")] * 20, [], limit=10)[0]), 10)

    def test_severity_counts_and_parse(self):
        self.assertEqual(shape.severity_counts([{"severity": "high", "n": 2}]),
                         {"critical": 0, "high": 2, "medium": 0, "low": 0, "info": 0})
        self.assertEqual(shape.parse_ts("2026-10-06T19:32:45+00:00"), utc(2026, 10, 6, 19, 32, 45))
        self.assertIsNone(shape.parse_ts(None))


class Networks(unittest.TestCase):
    def test_presence_classes(self):
        net = lambda h, dh, d, mobile=False: {"hours": h, "day_hours": dh, "days": d, "mobile": mobile}  # noqa: E731
        self.assertEqual(shape.presence(net(150, 70, 7), 166), "permanent")          # 150 >= 0.9 x 166
        self.assertEqual(shape.presence(net(40, 36, 3), 166), "daytime")
        self.assertEqual(shape.presence(net(40, 36, 1), 166), "intermittent")        # one day only
        self.assertEqual(shape.presence(net(5, 5, 1, mobile=True), 166), "transient")
        self.assertEqual(shape.presence(net(160, 100, 7, mobile=True), 166), "permanent")
        self.assertEqual(shape.presence(net(0, 0, 0), 0), "intermittent")

    def test_group_changes(self):
        t = utc(2026, 10, 6, 9, 5, 10)
        row = lambda i, bssid, ts, sev="info", f="WPA2-PSK", to="WPA3-SAE", net="A": {  # noqa: E731
            "id": i, "ts": ts, "severity": sev, "bssid": bssid, "network": net, "from_label": f, "to_label": to,
            "from_mfp": "no", "to_mfp": "capable"}
        out = shape.group_changes([row(1, "B1", t), row(2, "B2", t.replace(second=40)),
                                   row(3, "B3", t.replace(minute=6)), row(4, "B4", t, "low", "WPA2-PSK", "WPA2-PSK"),
                                   row(5, "B5", t, "high", "WPA2-PSK", "Open", "C")])
        self.assertEqual([(c["network"], c["kind"], c["bssids"], c["from"], c["to"]) for c in out], [
            ("A", "upgrade", ["B1", "B2"], "WPA2-PSK", "WPA3-SAE"),
            ("A", "upgrade", ["B3"], "WPA2-PSK", "WPA3-SAE"),
            ("A", "change", ["B4"], "MFP no", "MFP capable"),
            ("C", "downgrade", ["B5"], "WPA2-PSK", "Open")])

    def test_group_changes_leave_out_mfp_only_changes_of_mobile_aps(self):
        t = utc(2026, 10, 11, 9, 42)
        row = lambda i, mobile, f="WPA2-PSK", to="WPA2-PSK", fm="no", tm="capable": {  # noqa: E731
            "id": i, "ts": t, "severity": "low" if f == to else "medium", "bssid": "B%d" % i, "network": "N%d" % i,
            "from_label": f, "to_label": to, "from_mfp": fm, "to_mfp": tm, "mobile": mobile}
        out = shape.group_changes([row(1, True), row(2, False), row(3, True, "WPA2-PSK", "Open"),
                                   row(4, True, fm="no", tm="no")])
        # the hotspot's MFP toggle goes; a fixed AP's stays, and so does every label change of a hotspot
        self.assertEqual([(c["network"], c["from"], c["to"]) for c in out], [
            ("N2", "MFP no", "MFP capable"), ("N3", "WPA2-PSK", "Open"), ("N4", "MFP no", "MFP no")])

    def test_sighting_median(self):
        self.assertEqual(shape.sighting_median([0, 180, 4116]), 180)
        self.assertEqual(shape.sighting_median([0, 60, 120, 600]), 90)
        self.assertIsNone(shape.sighting_median([]))

    def test_ramp_from_the_30_day_peak(self):
        self.assertEqual(shape.ramp(199), ((10, 30, 70, 110, 160, 200), 200))
        self.assertEqual(shape.ramp(112), ((10, 20, 40, 70, 90, 120), 120))
        self.assertEqual(shape.ramp(1234), ((100, 200, 400, 700, 1000, 1300), 1300))
        self.assertEqual(shape.ramp(7), ((1, 2, 3, 4, 5, 7), 7))
        self.assertEqual(shape.ramp(3)[0], (1, 2, 3, 4, 5, 6))          # strictly increasing even above top
        self.assertEqual(shape.ramp(None), (shape.RAMP, None))          # no data: the board's bins
        self.assertEqual(shape.ramp(0), (shape.RAMP, None))

    def test_heatmap_days_and_dst_end(self):
        # 30d ending 26 Oct 01:30 local: the local day 25 Oct has 25 hours (02:00 twice)
        p = periods.period("30d", TZ, utc(2026, 10, 26, 0, 30))
        h1, h2 = utc(2026, 10, 25, 0), utc(2026, 10, 25, 1)        # 02:00 CEST and 02:00 CET
        captured = {h1, h2, utc(2026, 10, 25, 5), utc(2026, 10, 25, 6)}
        clients = {h1: {"total": 10, "by_network": {"A": 10}}, h2: {"total": 30, "by_network": {"A": 30}}}
        load = {h1, h2, utc(2026, 10, 25, 5)}
        rows, peak = shape.heatmap(p, TZ, clients, load, captured, "days")
        self.assertEqual(len(rows), 30)                      # 27 Sep .. 26 Oct (today, partial)
        day = {r["date"]: r for r in rows}["2026-10-25"]
        self.assertEqual(day["label"], "Sun 25 Oct")
        self.assertEqual((day["cells"][2]["value"], day["cells"][2]["start"]), (30, "2026-10-25T01:00:00Z"))
        self.assertEqual(day["cells"][6]["state"], "ok")      # 05:00 UTC = 06:00 CET: load, no managed AP
        self.assertEqual(day["cells"][6]["value"], 0)
        self.assertEqual(day["cells"][7]["state"], "load_missing")
        self.assertEqual(day["cells"][8]["state"], "no_data")
        self.assertEqual(rows[-1]["cells"][2]["state"], "outside")   # after now
        self.assertEqual(peak["value"], 30)

    def test_heatmap_week(self):
        p = periods.period("7d", TZ, utc(2026, 10, 10, 15, 52))
        hs = [utc(2026, 10, 5, 8), utc(2026, 10, 5, 9)]      # Mon 10:00 and 11:00 local
        clients = {hs[0]: {"total": 40, "by_network": {}}, hs[1]: {"total": 20, "by_network": {}}}
        rows, peak = shape.heatmap(p, TZ, clients, set(hs), set(hs), "week")
        self.assertEqual([r["label"] for r in rows], list(shape.WEEKDAYS))
        self.assertEqual(rows[0]["cells"][10], {"value": 40, "days": 1, "state": "ok"})
        self.assertEqual(rows[1]["cells"][10]["state"], "no_data")
        self.assertEqual(peak["label"], "Mon 10:00")


class SensorDays(unittest.TestCase):
    def test_day_shares(self):
        tz = "UTC"
        s = {"id": 1, "first_poll": utc(2026, 10, 1, 6), "last_poll": utc(2026, 10, 2, 11, 59, 30)}
        days = periods.local_days(tz, utc(2026, 10, 3, 12), 3)
        rows = [hour(utc(2026, 10, 1, h)) for h in range(6, 24)]
        rows += [hour(utc(2026, 10, 2, h), capture=0 if h == 3 else 120) for h in range(12)]
        rows[-2] = dict(rows[-2], limited_polls=60)                     # 2 Oct 10:00, half the polls limited
        out = shape.day_coverage(days, rows, s, utc(2026, 10, 3, 12),
                                 [(utc(2026, 10, 2, 8), utc(2026, 10, 2, 9))])  # documented: 08:00-09:00 limited
        d1, d2, d3 = out
        self.assertEqual((d1["ok"], d1["not_installed"], d1["outage"], d1["no_data"]), (0.75, 0.25, 0.0, 0.0))
        self.assertEqual(d2["limited"], round((3600 + 1800) / 86400, 3))
        self.assertEqual(d2["outage"], round(3600 / 86400, 3))           # 03:00 not collecting
        self.assertEqual(d2["no_data"], 0.5)                              # after 12:00:00 (last poll + 30 s)
        self.assertAlmostEqual(d2["ok"] + d2["limited"] + d2["outage"] + d2["no_data"], 1.0, places=2)
        self.assertEqual(d3, {"date": "2026-10-03", "ok": 0.0, "limited": 0.0, "outage": 0.0,
                              "not_installed": 0.0, "no_data": 1.0})


class Spectrum(unittest.TestCase):
    def test_band_dfs_iqr(self):
        self.assertEqual([shape.band_of(c) for c in ("1", "13", "36", "165", "", None, "6e")],
                         ["2.4", "2.4", "5", "5", None, None, None])
        self.assertEqual([shape.dfs(c) for c in ("6", "48", "52", "144", "149")], [False, False, True, True, False])
        self.assertEqual(shape.iqr([1, 2, 3, 4, 5]), [2.0, 4.0])
        self.assertEqual(shape.iqr([7.25]), [7.2, 7.2])
        self.assertIsNone(shape.iqr([]))

    def test_four_hour_blocks(self):
        p = periods.period("30d", TZ, utc(2026, 10, 30, 12))
        blocks = periods.blocks(p, 4)
        self.assertEqual(len(blocks), 180)
        self.assertEqual(blocks[0][0], p.starts[0])
        self.assertEqual(blocks[-1][1], p.ends[-1])
        self.assertEqual([b[0] for b in blocks[1:]], [b[1] for b in blocks[:-1]])
        dst = [b for b in blocks if b[0] == utc(2026, 10, 24, 22)][0]      # 25 Oct 00:00-04:00 local
        self.assertEqual(dst[1] - dst[0], timedelta(hours=5))


if __name__ == "__main__":
    unittest.main()
