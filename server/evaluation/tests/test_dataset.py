"""Dataset overview renderer (evaluation/dataset): the CSVs the SQL writes ->
dataset.md. Standard library only (runs on Python 3.9 too):

  cd server && python3 -m unittest discover -s evaluation/tests
"""

import csv
import os
import tempfile
import unittest

from evaluation.dataset import render


class Tables(unittest.TestCase):
    def test_md_table_escapes_pipes_and_pads_nothing(self):
        t = render.md_table(["a", "b"], [["x|y", 1], ["", None]])
        self.assertEqual(t.splitlines(), ["| a | b |", "|---|---|", "| x\\|y | 1 |", "|  |  |"])

    def test_pivot_orders_known_levels_and_keeps_totals_last(self):
        rows = [{"band": "2.4", "security": "strong", "aps": "3"},
                {"band": "2.4", "security": "open", "aps": "5"},
                {"band": "2.4", "security": "*", "aps": "8"},
                {"band": "5", "security": "strong", "aps": "2"},
                {"band": "5", "security": "*", "aps": "2"},
                {"band": "*", "security": "open", "aps": "5"},
                {"band": "*", "security": "strong", "aps": "5"},
                {"band": "*", "security": "*", "aps": "10"}]
        head, body = render.pivot(rows, "band", "security", "aps", col_order=render.SECURITY_ORDER)
        self.assertEqual(head, ["band", "open", "strong", "all"])
        self.assertEqual(body, [["2.4", "5", "3", "8"], ["5", "0", "2", "2"], ["all", "5", "5", "10"]])

    def test_duration(self):
        self.assertEqual(render.fmt_duration(59), "59 s")
        self.assertEqual(render.fmt_duration(3720), "1 h 2 m")
        self.assertEqual(render.fmt_duration(4 * 86400 + 7200 + 60), "4 d 2 h 1 m")
        self.assertEqual(render.fmt_duration(""), "")


class Render(unittest.TestCase):
    def write(self, d, name, rows):
        with open(os.path.join(d, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def test_every_section_is_rendered(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "period.csv", [{"metric": "first_poll", "value": "2026-09-15 13:53:07", "unit": "UTC",
                                          "note": ""},
                                         {"metric": "coverage", "value": "0.8", "unit": "ratio", "note": ""}])
            self.write(d, "outages.csv", [{"kind": "outage", "starts_at": "2026-10-20 10:55:16",
                                           "ends_at": "2026-10-20 19:04:41", "duration_s": "29365", "polls": "1",
                                           "cause": ""}])
            self.write(d, "boundaries.csv", [{"starts_at": "2026-09-26 22:45:26", "ends_at": "", "kind": "change",
                                              "boundary": "t", "label": "Hop list", "ref": "findings 7"}])
            self.write(d, "aps.csv", [{"band": "*", "security": "*", "aps": "10", "networks": "4",
                                       "hidden_aps": "1", "hidden_unnamed_aps": "1", "random_bssid_aps": "2",
                                       "mobile_aps": "2", "managed_aps": "3", "friendly_aps": "0",
                                       "suspicious_aps": "0", "ignored_aps": "1"}])
            self.write(d, "events.csv", [{"kind": "kismet", "type": "*", "severity": "*", "events": "3", "open": "3",
                                          "confirmed": "0", "benign": "0", "false_positive": "0"}])
            self.write(d, "alerts.csv", [{"header": "NOCLIENTMFP", "alerts": "3", "as_event": "t",
                                          "first": "2026-09-15", "last": "2026-10-20"}])
            md = render.render(d, "pi-fri", "2026-09-15", "2026-10-24")
            for title in ("Collection period", "Outages", "Configuration history", "APs and networks",
                          "Events", "Kismet alerts"):
                self.assertIn("## ", md)
                self.assertIn(title, md)
            self.assertIn("8 h 9 m", md)            # the outage's duration, readable
            self.assertIn("| all | 10 |", md)


if __name__ == "__main__":
    unittest.main()
