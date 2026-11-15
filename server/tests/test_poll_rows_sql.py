"""ops/poll_rows_sql.py: an extract of the sensor's buffer -> the psql script
that merges its rows through ingest_merge() (standard library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ops"))

import poll_rows_sql as prs  # noqa: E402

K1, K2 = "4202770D00000000_0000000000A1", "4202770D00000000_0000000000A2"


def obs(i, poll, ts, key, **over):
    row = {"id": i, "ts": ts, "key": key, "last_time": ts - 7, "rssi": -67, "rssi_floor": None, "pk_total": 3,
           "pk_data": 0, "disconnects": 0, "disconnects_last": None, "bss_timestamp": 31135389, "poll_id": poll}
    row.update(over)
    return row


def extract(observations, polls=((123578, 1793602145), (123579, 1793602175))):
    return {"format": prs.FORMAT, "buffer_id": "b", "schema_version": "9",
            "polls": [{"id": i, "ts": ts, "ok": 1} for i, ts in polls], "observations": observations}


class Rows(unittest.TestCase):
    def test_rows_carry_the_polls_seq_and_raw_ts(self):
        got = prs.rows(extract([obs(1, 123578, 1793602145, K1), obs(2, 123579, 1793602175, K1, rssi=None,
                                                                    rssi_floor=1, disconnects_last=1793602100)]))
        self.assertEqual(got, [(123578, 1793602145, K1, -67, None, 3, 0, 0, None, 31135389),
                               (123579, 1793602175, K1, None, 1, 3, 0, 0, 1793602100, 31135389)])

    def test_what_is_not_such_an_extract_is_refused(self):
        bad = [
            {"polls": [], "observations": []},                                             # no format
            extract([]),                                                                   # no row
            extract([obs(1, 5, 1793602145, K1)]),                                          # a poll not in the extract
            extract([obs(1, 123578, 1793602175, K1)]),                                     # not its poll's ts
            extract([obs(1, 123578, 1793602145, K1), obs(2, 123578, 1793602145, K1)]),     # the same AP twice
            extract([obs(1, 123578, 1793602145, "K\t1")]),                                 # would break COPY
            extract([obs(1, 123578, 1793602145, K1, rssi="-67")]),
            extract([obs(1, 123578, 1793602145, K1, pk_total=3.5)]),
            extract([obs(1, 123578, 1793602145, None)]),
        ]
        for e in bad:
            with self.assertRaises(prs.Bad):
                prs.rows(e)


class Script(unittest.TestCase):
    def setUp(self):
        self.sql = prs.script(extract([obs(1, 123578, 1793602145, K1), obs(2, 123578, 1793602145, K2, rssi=None)]), 7)

    def test_the_rows_go_in_as_copy_data(self):
        lines = self.sql.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith("COPY merge_poll_rows"))
        self.assertEqual(lines[start], "COPY merge_poll_rows (%s) FROM STDIN;" % ", ".join(prs.STAGE_COLS))
        self.assertEqual(lines[start + 1], "\t".join(["123578", "1793602145", K1, "-67", "\\N", "3", "0", "0", "\\N",
                                                      "31135389"]))
        self.assertEqual(lines[start + 2].split("\t")[3], "\\N")
        self.assertEqual(lines[start + 3], "\\.")

    def test_one_transaction_through_the_merge_for_the_given_sensor(self):
        self.assertEqual(self.sql.count("BEGIN;"), 1)
        self.assertIn("SELECT ingest_stage_create();", self.sql)
        self.assertIn("ingest_merge(7, 'snapshot')", self.sql)
        self.assertIn("p.sensor_id = 7 AND p.seq = m.seq", self.sql)
        self.assertNotIn("%(", self.sql)
        # the only direct write is the stage table; observations is written by the merge alone
        self.assertNotIn("INSERT INTO observations", self.sql)
        self.assertIn("INSERT INTO stage_observations", self.sql)

    def test_a_dry_run_unless_apply_is_given(self):
        tail = self.sql[self.sql.index("\\if :{?apply}"):]
        self.assertLess(tail.index("COMMIT;"), tail.index("\\else"))
        self.assertLess(tail.index("\\else"), tail.index("ROLLBACK;"))

    def test_a_poll_without_exactly_one_stored_twin_stops_the_script(self):
        self.assertIn("WHERE stored_polls <> 1", self.sql)
        self.assertIn("RAISE EXCEPTION 'poll_rows_sql.py: poll % (sensor clock %) matches % stored polls", self.sql)


if __name__ == "__main__":
    unittest.main()
