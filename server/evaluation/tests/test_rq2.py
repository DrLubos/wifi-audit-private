"""RQ2 false-positive analysis (evaluation/rq2): cause attribution and the
rates, on hand-made event rows. Standard library only:

  cd server && python3 -m unittest discover -s evaluation/tests
"""

import csv
import os
import tempfile
import unittest

from evaluation.rq2 import analysis as a


def ev(**kw):
    """One row as events.sql exports it (strings, empty = NULL)."""
    row = {k: "" for k in a.EVENT_COLUMNS}
    row.update({"id": "1", "kind": "kismet", "type": "NOCLIENTMFP", "rule": "NOCLIENTMFP",
                "severity": "low", "ts": "2026-10-01 12:00:00+00", "status": "open"})
    row.update({k: str(v) if v is not None else "" for k, v in kw.items()})
    return row


class Vocabulary(unittest.TestCase):
    def test_same_causes_as_the_api(self):
        import os as _os
        import re as _re
        api = open(_os.path.join(_os.path.dirname(__file__), "..", "..", "api", "app", "v2", "writes.py")).read()
        keys = _re.findall(r'^\s+\("(\w+)", "', api[api.index("CAUSES = ("):api.index("CAUSE_KEYS")], _re.M)
        self.assertEqual(keys, list(a.CAUSES))


class RuleCause(unittest.TestCase):
    def test_noclientmfp_on_an_open_network(self):
        self.assertEqual(a.rule_cause(ev(ap_level_at="open"))[0], "kismet_rule")

    def test_noclientmfp_mfp_not_offered(self):
        c, detail = a.rule_cause(ev(ap_level_at="psk_no_mfp", ap_mfp_at="no"))
        self.assertEqual(c, "kismet_rule")
        self.assertIn("not offered", detail)

    def test_noclientmfp_unknown_or_offered_has_no_rule_cause(self):
        self.assertEqual(a.rule_cause(ev(ap_level_at="", ap_mfp_at=""))[0], None)
        self.assertEqual(a.rule_cause(ev(ap_level_at="strong", ap_mfp_at="required"))[0], None)

    def test_noclientmfp_unknown_security_with_mfp_flags_zero_has_no_rule_cause(self):
        # an AP first seen without a beacon record has mfp stored as 0 ("no"),
        # but its offer is unknown (findings.md 11, 12)
        self.assertEqual(a.rule_cause(ev(ap_level_at="unknown", ap_mfp_at="no"))[0], None)

    def test_retention_risk_for_a_backlog_the_server_held(self):
        row = ev(kind="sensor", type="upload_retention_risk", rule="upload_retention_risk",
                 backlog_on_server="t")
        self.assertEqual(a.rule_cause(row)[0], "pipeline_bug")
        row["backlog_on_server"] = "f"
        self.assertEqual(a.rule_cause(row)[0], None)

    def test_unknown_bssid_whose_oui_was_allowed_later(self):
        row = ev(kind="detection", type="unknown_bssid_managed_ssid", rule="unknown_bssid_managed_ssid",
                 oui_allowed_later="t", classification_now="managed")
        self.assertEqual(a.rule_cause(row)[0], "allow_list_incomplete")
        row.update(oui_allowed_later="f", classification_now="suspicious")
        self.assertEqual(a.rule_cause(row)[0], None)

    def test_evil_twin_on_a_floor_baseline(self):
        row = ev(kind="detection", type="evil_twin", rule="evil_twin:rssi_deviation", baseline_median="-104")
        self.assertEqual(a.rule_cause(row)[0], "rssi_floor")
        row["baseline_median"] = "-70"
        self.assertEqual(a.rule_cause(row)[0], None)

    def test_degraded_capture_windows(self):
        row = ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD", in_limited="t")
        self.assertEqual(a.rule_cause(row)[0], "channel_hopping")
        row = ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD", degraded_kinds="coverage")
        self.assertEqual(a.rule_cause(row)[0], "channel_hopping")
        row = ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD", in_outage="t")
        self.assertEqual(a.rule_cause(row)[0], "sensor_degraded")
        row = ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD", degraded_kinds="no_frames")
        self.assertEqual(a.rule_cause(row)[0], "sensor_degraded")

    def test_a_sensor_outage_is_not_a_degraded_capture_cause(self):
        row = ev(kind="sensor", type="sensor_outage", rule="sensor_outage", in_outage="t")
        self.assertEqual(a.rule_cause(row)[0], None)

    def test_channel_change_nearby_is_an_indicator_not_a_cause(self):
        self.assertEqual(a.rule_cause(ev(type="BEACONRATE", rule="BEACONRATE", channel_change_1h="t"))[0], None)


class Assign(unittest.TestCase):
    def test_the_label_cause_wins_and_the_rule_is_kept(self):
        r = a.assign(ev(ap_level_at="open", status="benign", status_reason="expected_behaviour",
                        status_cause="client_behaviour", note="cause:kismet_rule is only text now"))
        self.assertEqual((r["cause"], r["cause_source"], r["rule_cause"]),
                         ("client_behaviour", "label", "kismet_rule"))

    def test_label_unknown_is_not_traced(self):
        r = a.assign(ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD", status="benign", status_reason="accepted_risk",
                        status_cause="unknown"))
        self.assertEqual((r["cause"], r["cause_source"]), ("unknown", "label"))
        self.assertFalse(a.traced(r))

    def test_rule_only(self):
        r = a.assign(ev(ap_level_at="open"))
        self.assertEqual((r["cause"], r["cause_source"]), ("kismet_rule", "rule"))

    def test_none(self):
        r = a.assign(ev(type="DEAUTHFLOOD", rule="DEAUTHFLOOD"))
        self.assertEqual((r["cause"], r["cause_source"]), ("", "none"))

    def test_scope(self):
        self.assertEqual(a.assign(ev(kind="sensor", type="sensor_outage"))["scope"], "sensor")
        self.assertEqual(a.assign(ev(kind="config", type="security_change"))["scope"], "security")


class Aggregate(unittest.TestCase):
    def rows(self):
        return [a.assign(r) for r in [
            ev(id=1, ap_level_at="open", status="false_positive", status_reason="rule_defect",
               status_cause="kismet_rule"),
            ev(id=2, ap_level_at="open", status="benign", status_reason="expected_behaviour", status_cause="kismet_rule"),
            ev(id=3, ap_level_at="open"),
            ev(id=4, ap_level_at="strong", ap_mfp_at="required", status="confirmed",
               status_reason="security_relevant"),
            ev(id=5, kind="sensor", type="upload_retention_risk", rule="upload_retention_risk",
               backlog_on_server="t", status="false_positive", status_reason="artefact", status_cause="pipeline_bug"),
        ]]

    def test_rates_per_rule(self):
        g = {(r["kind"], r["type"], r["rule"]): r for r in a.by_rule(self.rows())}
        m = g[("kismet", "NOCLIENTMFP", "NOCLIENTMFP")]
        self.assertEqual((m["n"], m["open"], m["labelled"], m["confirmed"], m["benign"], m["false_positive"]),
                         (4, 1, 3, 1, 1, 1))
        self.assertAlmostEqual(m["fp_rate"], 1 / 3)
        self.assertAlmostEqual(m["not_security_rate"], 2 / 3)
        self.assertAlmostEqual(m["label_coverage"], 0.75)
        self.assertEqual(m["traced"], 3)                 # the three open-network alerts
        self.assertTrue(0 < m["fp_rate_lo"] < m["fp_rate"] < m["fp_rate_hi"] < 1)

    def test_totals_row_per_scope(self):
        g = {(r["kind"], r["type"], r["rule"]): r for r in a.by_rule(self.rows())}
        self.assertEqual(g[("*security", "*", "*")]["n"], 4)
        self.assertEqual(g[("*sensor", "*", "*")]["n"], 1)
        self.assertEqual(g[("*all", "*", "*")]["n"], 5)

    def test_no_labels_gives_empty_rates_not_zero(self):
        m = a.by_rule([a.assign(ev())])[0]
        self.assertEqual(m["labelled"], 0)
        self.assertEqual(m["fp_rate"], "")
        self.assertEqual(m["fp_rate_lo"], "")

    def test_reasons_grouped(self):
        rs = {(r["kind"], r["type"], r["status"], r["status_reason"]): r["n"] for r in a.by_reason(self.rows())}
        self.assertEqual(rs[("kismet", "NOCLIENTMFP", "false_positive", "rule_defect")], 1)
        self.assertEqual(rs[("kismet", "NOCLIENTMFP", "open", "")], 1)
        self.assertEqual(rs[("*all", "*", "false_positive", "artefact")], 1)

    def test_causes_and_traced_share(self):
        cs = {(r["kind"], r["type"], r["cause"], r["source"]): r for r in a.by_cause(self.rows())}
        self.assertEqual(cs[("kismet", "NOCLIENTMFP", "kismet_rule", "label")]["n"], 2)
        self.assertEqual(cs[("kismet", "NOCLIENTMFP", "kismet_rule", "rule")]["n"], 1)
        self.assertEqual(cs[("kismet", "NOCLIENTMFP", "", "none")]["n"], 1)
        s = {r["metric"]: r["value"] for r in a.summary(self.rows())}
        self.assertEqual(s["events"], 5)
        self.assertEqual(s["events_traced"], 4)
        self.assertAlmostEqual(s["traced_share"], 0.8)
        self.assertEqual(s["security_events"], 4)
        self.assertAlmostEqual(s["security_traced_share"], 0.75)
        # labelled as not a security event (benign + false positive) and traced
        self.assertEqual(s["not_security_labelled"], 3)
        self.assertAlmostEqual(s["not_security_traced_share"], 1.0)

    def test_rule_vs_label_agreement(self):
        lab = dict(status="benign", status_reason="expected_behaviour")
        rows = [a.assign(ev(id=1, ap_level_at="open", status_cause="kismet_rule", **lab)),
                a.assign(ev(id=2, ap_level_at="open", status_cause="client_behaviour", **lab)),
                a.assign(ev(id=3, status_cause="other", note="x", **lab)),
                a.assign(ev(id=4, ap_level_at="open"))]
        ag = {(r["rule_cause"], r["label_cause"]): r["n"] for r in a.agreement(rows)}
        self.assertEqual(len(ag), 3)
        self.assertEqual(ag[("kismet_rule", "kismet_rule")], 1)
        self.assertEqual(ag[("kismet_rule", "client_behaviour")], 1)
        self.assertEqual(ag[("", "other")], 1)


class Files(unittest.TestCase):
    def test_round_trip_and_no_mac_in_output(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "events_raw.csv")
            with open(src, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=a.EVENT_COLUMNS)
                w.writeheader()
                w.writerow(ev(ap_level_at="open", status="benign", status_reason="expected_behaviour",
                              status_cause="kismet_rule", note="seen from aa:bb:cc:dd:ee:ff"))
            out = os.path.join(d, "out")
            a.run(src, out)
            names = sorted(os.listdir(out))
            self.assertEqual(names, sorted(a.OUTPUTS))
            for n in names:
                with open(os.path.join(out, n)) as f:
                    text = f.read()
                self.assertNotRegex(text, a.MAC_RE.pattern, n)


if __name__ == "__main__":
    unittest.main()
