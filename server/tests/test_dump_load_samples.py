"""ops/dump_load_samples.py: pg_restore's COPY stream -> the load-samples psql
script (standard library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'
"""

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ops"))

import dump_load_samples as dls  # noqa: E402

HEADER = ("COPY public.observations (ts, sensor_id, device_key, last_time, freq_khz, rssi, "
          "n_clients, disconnects, qbss_stations, util_pct, bssid) FROM stdin;")


def row(ts, sid, key, n, q, u, bssid="\\N"):
    return "\t".join([ts, str(sid), key, "1758981600", "2412000", "-60", n, "0", q, u, bssid])


RESTORE = "\n".join([
    "--",
    "-- PostgreSQL database dump",
    "SET statement_timeout = 0;",
    HEADER,
    row("2026-09-20 10:00:00+00", 1, "AP1", "3", "2", "12.5"),
    row("2026-09-20 10:00:30+00", 1, "AP1", "\\N", "\\N", "\\N"),      # a v6-style row
    row("2026-09-20 10:00:00+00", 1, "CL1", "\\N", "\\N", "\\N", "A1:B2:C3:D4:E5:F6"),
    row("2026-09-20 10:00:00+00", 2, "AP9", "1", "0", "0"),           # another sensor
    row("2026-09-20 10:01:00+00", 1, "AP2", "0", "0", "0"),
    "\\.",
    "",
]) + "\n"


def run(text, sid=1):
    out = io.StringIO()
    result = dls.convert(io.StringIO(text), out, sid, lambda msg: None)
    return result, out.getvalue()


class DumpLoadSamples(unittest.TestCase):
    def test_rows_with_load_of_the_sensor(self):
        (read, written), sql = run(RESTORE)
        self.assertEqual((read, written), (5, 2))
        body = sql.split("COPY stage_load_samples FROM STDIN;\n")[1].split("\\.\n")[0]
        self.assertEqual(body.splitlines(),
                         ["2026-09-20 10:00:00+00\tAP1\t3\t2\t12.5",
                          "2026-09-20 10:01:00+00\tAP2\t0\t0\t0"])

    def test_one_transaction_calls_the_loader(self):
        _, sql = run(RESTORE)
        self.assertLess(sql.index("BEGIN;"), sql.index("CREATE TEMP TABLE stage_load_samples"))
        self.assertLess(sql.index("\\.\n"), sql.index("SELECT * FROM load_ap_load_samples(1);"))
        self.assertLess(sql.index("load_ap_load_samples(1)"), sql.index("COMMIT;"))
        self.assertIn("\\set ON_ERROR_STOP on", sql)

    def test_no_observations_block(self):
        with self.assertRaisesRegex(ValueError, "no COPY block"):
            run("SET x = 1;\n")

    def test_missing_column(self):
        with self.assertRaisesRegex(ValueError, "lack util_pct"):
            run(HEADER.replace(", util_pct", "") + "\n\\.\n")

    def test_truncated_input(self):
        with self.assertRaisesRegex(ValueError, "end marker"):
            run(RESTORE.split("\\.")[0])


if __name__ == "__main__":
    unittest.main()
