"""ops/dump_observations.py: pg_restore's devices + observations COPY streams ->
the restore psql script (standard library, no database):

  cd server && python3 -m unittest discover -s tests -p 'test_*.py'
"""

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ops"))

import dump_observations as do  # noqa: E402

DEVICES = "\n".join([
    "COPY public.devices (sensor_id, device_key, mac, type, manuf) FROM stdin;",
    "1\tAP1\tA8:00:00:00:00:01\tap\t\\N",
    "1\tLATE\tA8:00:00:00:00:02\tbridged\t\\N",      # an AP only after the migration
    "1\tCL1\t11:00:00:00:00:01\tclient\t\\N",
    "2\tOTHER\tA8:00:00:00:00:03\tclient\t\\N",      # another sensor
    "\\.",
])
OBS_HEADER = ("COPY public.observations (ts, sensor_id, device_key, last_time, rssi, rssi_floor, "
              "pk_total, pk_data, n_clients, disconnects, disconnects_last, bss_timestamp, bssid) "
              "FROM stdin;")


def obs(ts, sid, key, rssi="-60"):
    return "\t".join([ts, str(sid), key, "1", rssi, "\\N", "10", "2", "\\N", "0", "\\N", "99", "\\N"])


OBSERVATIONS = "\n".join([
    OBS_HEADER,
    obs("2026-09-20 10:00:00+00", 1, "AP1"),     # copied by the migration already
    obs("2026-09-20 10:00:00+00", 1, "LATE"),
    obs("2026-09-20 10:00:30+00", 1, "LATE", "\\N"),
    obs("2026-09-20 10:00:00+00", 1, "CL1"),      # staged; the SQL keeps only today's APs
    obs("2026-09-20 10:00:00+00", 2, "OTHER"),
    "\\.",
])


def run(text, sid=1):
    out = io.StringIO()
    result = do.convert(io.StringIO(text + "\n"), out, sid, lambda msg: None)
    return result, out.getvalue()


class DumpObservations(unittest.TestCase):
    def test_rows_of_devices_that_were_not_aps(self):
        (n_keys, read, staged), sql = run(DEVICES + "\n" + OBSERVATIONS)
        self.assertEqual((n_keys, read, staged), (2, 5, 3))
        body = sql.split("COPY stage_restore FROM STDIN;\n")[1].split("\\.\n")[0]
        # ts, pk_total, pk_data, bss_timestamp, disconnects_last, disconnects, rssi, rssi_floor, key
        self.assertEqual(body.splitlines(), [
            "2026-09-20 10:00:00+00\t10\t2\t99\t\\N\t0\t-60\t\\N\tLATE",
            "2026-09-20 10:00:30+00\t10\t2\t99\t\\N\t0\t\\N\t\\N\tLATE",
            "2026-09-20 10:00:00+00\t10\t2\t99\t\\N\t0\t-60\t\\N\tCL1"])

    def test_one_transaction_then_vacuum_and_refresh(self):
        _, sql = run(DEVICES + "\n" + OBSERVATIONS)
        commit = sql.index("COMMIT;")
        self.assertLess(sql.index("BEGIN;"), sql.index("COPY stage_restore"))
        self.assertIn("JOIN devices d ON d.sensor_id = 1 AND d.device_key = s.device_key "
                      "AND d.type = 'ap'", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertLess(sql.index("refresh_ap_baselines(1, 200, 2)"), commit)
        self.assertLess(commit, sql.index("VACUUM (ANALYZE) observations;"))
        self.assertLess(sql.index("VACUUM (ANALYZE) observations;"),
                        sql.index("SELECT * FROM refresh_rollups(1, "))

    def test_observations_before_devices(self):
        with self.assertRaisesRegex(ValueError, "before devices"):
            run(OBSERVATIONS + "\n" + DEVICES)

    def test_missing_devices_block(self):
        with self.assertRaisesRegex(ValueError, "no COPY block for devices"):
            run("SET x = 1;")

    def test_missing_column(self):
        with self.assertRaisesRegex(ValueError, "lack rssi_floor"):
            run(DEVICES + "\n" + OBSERVATIONS.replace(" rssi_floor,", ""))

    def test_truncated_input(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            run(DEVICES + "\n" + OBSERVATIONS.rsplit("\\.", 1)[0])


if __name__ == "__main__":
    unittest.main()
