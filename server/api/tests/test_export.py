"""The label export (app/v2/export.py): the RQ2 dataset as CSV. Standard library only."""

import csv
import io
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import export  # noqa: E402

T = datetime(2026, 10, 11, 15, 31, 15, 123456, tzinfo=timezone.utc)


def parse(text):
    return list(csv.reader(io.StringIO(text, newline="")))


class Labels(unittest.TestCase):
    ROW = {"id": 372, "ts": T, "ts_end": None, "sensor": "pi-fri", "kind": "kismet", "type": "NOCLIENTMFP",
           "type_label": "Association request without MFP", "severity": "low", "bssid": "EC:58:EA:54:45:8C",
           "ssid": "IK-WIFI", "status": "benign", "status_reason": "expected_behaviour",
           "status_note": "open guest network, as expected", "status_at": T}

    def test_columns_and_a_labelled_row(self):
        out = parse(export.labels_csv([self.ROW]))
        self.assertEqual(out[0], list(export.LABEL_COLUMNS))
        self.assertEqual(out[1], ["372", "2026-10-11T15:31:15Z", "", "pi-fri", "kismet", "NOCLIENTMFP",
                                  "Association request without MFP", "low", "EC:58:EA:54:45:8C", "IK-WIFI", "benign",
                                  "expected_behaviour", "open guest network, as expected", "2026-10-11T15:31:15Z"])

    def test_an_open_event_has_empty_label_cells_and_a_hidden_bssid_stays_empty(self):
        open_row = dict(self.ROW, status="open", status_reason=None, status_note=None, status_at=None, bssid=None, ssid=None)
        row = parse(export.labels_csv([open_row]))[1]
        self.assertEqual((row[8], row[9], row[10], row[11], row[12], row[13]), ("", "", "open", "", "", ""))

    def test_quoting_and_formula_guard(self):
        tricky = dict(self.ROW, status_note='said "ok", then\nleft', ssid="=HYPERLINK(1)", type_label="+x")
        out = parse(export.labels_csv([tricky]))
        self.assertEqual(out[1][12], 'said "ok", then\nleft')                  # quotes, comma and newline survive
        self.assertEqual(out[1][9], "'=HYPERLINK(1)")                         # a spreadsheet never runs a cell
        self.assertEqual(out[1][6], "'+x")
        self.assertEqual(export.safe_cell("-12 dBm"), "'-12 dBm")
        self.assertEqual(export.safe_cell("IK-WIFI"), "IK-WIFI")
        self.assertEqual(export.safe_cell(None), "")
        self.assertEqual(export.safe_cell(7), "7")

    def test_no_client_mac_can_get_in(self):
        # the columns are fixed; a note is the operator's text - a MAC typed in it is the operator's, but
        # nothing else carries one (the BSSID is blanked by the query unless it is an account AP)
        self.assertNotIn("summary", export.LABEL_COLUMNS)
        self.assertNotIn("evidence", export.LABEL_COLUMNS)


class History(unittest.TestCase):
    def test_a_change_of_label(self):
        row = {"id": 5, "at": T, "event_id": 372, "kind": "kismet", "type": "NOCLIENTMFP",
               "old_status": "benign", "old_reason": "expected_behaviour", "old_note": None,
               "new_status": "false_positive", "new_reason": "rule_defect", "new_note": "misattributed"}
        out = parse(export.history_csv([row]))
        self.assertEqual(out[0], list(export.HISTORY_COLUMNS))
        self.assertEqual(out[1], ["372", "2026-10-11T15:31:15Z", "kismet", "NOCLIENTMFP", "benign", "expected_behaviour",
                                  "", "false_positive", "rule_defect", "misattributed"])

    def test_filename_has_the_date(self):
        self.assertEqual(export.filename("labels", T), "wifi-audit-labels-20261011.csv")
        self.assertEqual(export.filename("label-history", T), "wifi-audit-label-history-20261011.csv")


if __name__ == "__main__":
    unittest.main()
