"""RQ3 one-shot vs continuous simulation (evaluation/rq3): sampling of the
simulated audits and the summary figures, on hand-made rows. Standard
library only:

  cd server && python3 -m unittest discover -s evaluation/tests
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from evaluation.rq3 import sim


class Sampling(unittest.TestCase):
    TS = list(range(1_000_000, 1_000_000 + 30 * 1000, 30))       # 1000 collecting polls, 30 s apart

    def test_deterministic_for_a_seed(self):
        a = sim.sample_starts(self.TS, 20, 900, seed=7)
        self.assertEqual(a, sim.sample_starts(self.TS, 20, 900, seed=7))
        self.assertNotEqual(a, sim.sample_starts(self.TS, 20, 900, seed=8))

    def test_starts_are_collecting_polls_and_the_window_fits(self):
        a = sim.sample_starts(self.TS, 50, 3600, seed=1)
        self.assertEqual(len(a), 50)
        self.assertEqual(len(set(a)), 50)                         # without replacement
        self.assertTrue(set(a) <= set(self.TS))
        self.assertTrue(all(t + 3600 <= self.TS[-1] for t in a))
        self.assertEqual(a, sorted(a))

    def test_more_than_possible_takes_all_that_fit(self):
        a = sim.sample_starts(self.TS[:10], 100, 60, seed=1)
        self.assertEqual(a, [t for t in self.TS[:10] if t + 60 <= self.TS[9]])

    def test_pairs_second_audit_at_the_first_collecting_poll_after_the_gap(self):
        ts = list(range(0, 30 * 100, 30)) + list(range(10_000, 10_000 + 30 * 100, 30))   # an outage between
        pairs = sim.sample_pairs(ts, 30, gap_s=3000, duration_s=300, seed=3)
        self.assertTrue(pairs)
        for t1, t2 in pairs:
            self.assertGreaterEqual(t2, t1 + 3000)
            self.assertIn(t2, ts)
            self.assertLessEqual(t2 + 300, ts[-1])
            self.assertEqual(t2, min(t for t in ts if t >= t1 + 3000))

    def test_pg_array_literal_and_chunks(self):
        self.assertEqual(sim.pg_array([0, 60]), "{1970-01-01T00:00:00Z,1970-01-01T00:01:00Z}")
        self.assertEqual(sim.chunks(list(range(7)), 3), [[0, 1, 2], [3, 4, 5], [6]])


class WorkingHours(unittest.TestCase):
    """Weekdays 08:00-17:00 Europe/Bratislava: the window must lie inside."""
    TZ = "Europe/Bratislava"

    @staticmethod
    def utc(*a):
        return int(datetime(*a, tzinfo=timezone.utc).timestamp())

    def test_window_inside_working_hours(self):
        ok = sim.in_working_hours
        # Monday 2026-10-19, CEST (UTC+2): 08:00 local = 06:00 UTC
        self.assertTrue(ok(self.utc(2026, 10, 19, 6, 0), 900, self.TZ))
        self.assertFalse(ok(self.utc(2026, 10, 19, 5, 59), 900, self.TZ))        # starts 07:59
        self.assertTrue(ok(self.utc(2026, 10, 19, 14, 0), 3600, self.TZ))        # 16:00-17:00
        self.assertFalse(ok(self.utc(2026, 10, 19, 14, 1), 3600, self.TZ))       # ends 17:01
        self.assertFalse(ok(self.utc(2026, 10, 24, 8, 0), 900, self.TZ))         # Saturday
        self.assertFalse(ok(self.utc(2026, 10, 25, 8, 0), 900, self.TZ))         # Sunday
        # after the DST end (CET, UTC+1): 08:00 local = 07:00 UTC
        self.assertFalse(ok(self.utc(2026, 10, 26, 6, 30), 900, self.TZ))
        self.assertTrue(ok(self.utc(2026, 10, 26, 7, 0), 900, self.TZ))
        self.assertTrue(ok(self.utc(2026, 10, 24, 8, 0), 900, self.TZ, hours="24h"))

    def test_sampling_respects_the_variant(self):
        start = self.utc(2026, 10, 19, 0, 0)                                    # a week of polls
        ts = list(range(start, start + 7 * 86400, 30))
        work = sim.sample_starts(ts, 200, 900, seed=1, hours="work", tz=self.TZ)
        self.assertEqual(len(work), 200)
        self.assertTrue(all(sim.in_working_hours(t, 900, self.TZ) for t in work))
        self.assertNotEqual(work, sim.sample_starts(ts, 200, 900, seed=1))       # 24h differs
        pairs = sim.sample_pairs(ts, 20, 86400, 3600, seed=1, hours="work", tz=self.TZ)
        self.assertTrue(pairs)
        for t1, t2 in pairs:
            self.assertTrue(sim.in_working_hours(t1, 3600, self.TZ))
            self.assertTrue(sim.in_working_hours(t2, 3600, self.TZ))
            self.assertGreaterEqual(t2, t1 + 86400)


class Stats(unittest.TestCase):
    def test_quantiles(self):
        q = sim.describe([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual((q["n"], q["mean"], q["median"], q["min"], q["max"]), (10, 5.5, 5.5, 1, 10))
        self.assertAlmostEqual(q["p10"], 1.9)
        self.assertAlmostEqual(q["p90"], 9.1)
        self.assertEqual(sim.describe([])["n"], 0)


def long_rows(rows):
    return [{"t0": t, "metric": m, "key": k, "n": str(n)} for t, m, k, n in rows]


class Summary(unittest.TestCase):
    REF = long_rows([
        ("", "aps", "all", 10), ("", "aps_class", "permanent", 4), ("", "aps_class", "transient", 6),
        ("", "cfg", "security", 4), ("", "cfg", "channel", 40),
        ("", "sec_transitions", "all", 4), ("", "sec_transitions", "level", 2),
        ("", "events", "kismet", 20), ("", "polls", "collecting", 2880),
    ])
    WIN = long_rows([
        ("2026-10-01T00:00:00Z", "aps", "all", 5), ("2026-10-01T00:00:00Z", "aps_class", "permanent", 4),
        ("2026-10-01T00:00:00Z", "aps_class", "transient", 1), ("2026-10-01T00:00:00Z", "cfg", "channel", 2),
        ("2026-10-01T00:00:00Z", "polls", "collecting", 30), ("2026-10-01T00:00:00Z", "events", "kismet", 1),
        ("2026-10-02T00:00:00Z", "aps", "all", 3), ("2026-10-02T00:00:00Z", "aps_class", "permanent", 3),
        ("2026-10-02T00:00:00Z", "polls", "collecting", 15),
    ])

    def summary(self):
        return {(r["metric"], r["key"]): r for r in sim.summarize_windows(self.WIN, self.REF, 900)}

    def test_aps_share_of_the_continuous_inventory(self):
        s = self.summary()
        self.assertEqual(s[("aps", "all")]["mean"], 4.0)
        self.assertEqual(s[("aps", "all")]["continuous"], 10)
        self.assertAlmostEqual(s[("aps", "all")]["mean_share"], 0.4)
        self.assertAlmostEqual(s[("aps_class", "permanent")]["mean_share"], 3.5 / 4)
        self.assertAlmostEqual(s[("aps_class", "transient")]["mean_share"], 0.5 / 6)   # 0 in the second window

    def test_witnessed_changes_and_events(self):
        s = self.summary()
        ch = s[("cfg", "channel")]
        self.assertEqual(ch["mean"], 1.0)
        self.assertAlmostEqual(ch["mean_share"], 1.0 / 40)
        self.assertAlmostEqual(ch["p_any"], 0.5)                  # one of two audits saw any
        sec = s[("cfg", "security")]                              # in the reference, never witnessed
        self.assertEqual((sec["mean"], sec["p_any"], sec["continuous"]), (0.0, 0.0, 4))
        self.assertAlmostEqual(s[("events", "kismet")]["p_any"], 0.5)

    def test_reference_only_rows_are_not_audit_figures(self):
        ref = self.REF + long_rows([("", "hours", "collecting", 24)])
        keys = {(r["metric"], r["key"]) for r in sim.summarize_windows(self.WIN, ref, 900)}
        self.assertNotIn(("hours", "collecting"), keys)

    def test_window_coverage(self):
        s = self.summary()
        self.assertAlmostEqual(s[("polls", "collecting")]["mean_share_of_window"], (30 / 30 + 15 / 30) / 2)

    def test_expected_share_of_time(self):
        s = self.summary()
        # the audit's duration over the continuous record's collecting time
        self.assertAlmostEqual(s[("aps", "all")]["time_share"], 900 / (2880 * 30))


class Pairs(unittest.TestCase):
    ROWS = [
        {"gap_s": "86400", "t1": "a", "t2": "b", "aps_t1": "10", "aps_t2": "8", "aps_both": "7",
         "sec_aps_changed": "2", "sec_transitions": "5", "sec_aps_detected": "1",
         "ch_aps_changed": "4", "ch_transitions": "40", "ch_aps_detected": "3"},
        {"gap_s": "86400", "t1": "c", "t2": "d", "aps_t1": "10", "aps_t2": "10", "aps_both": "9",
         "sec_aps_changed": "0", "sec_transitions": "0", "sec_aps_detected": "0",
         "ch_aps_changed": "2", "ch_transitions": "10", "ch_aps_detected": "0"},
    ]

    def test_detected_share_of_changed_aps(self):
        s = {r["gap_s"]: r for r in sim.summarize_pairs(self.ROWS)}["86400"]
        self.assertEqual(s["pairs"], 2)
        self.assertAlmostEqual(s["sec_detected_share"], 1 / 2)
        self.assertAlmostEqual(s["ch_detected_share"], 3 / 6)
        self.assertAlmostEqual(s["sec_pairs_any_change"], 0.5)
        self.assertAlmostEqual(s["sec_pairs_any_detected"], 0.5)
        self.assertAlmostEqual(s["ch_transitions_per_changed_ap"], 50 / 6)
        self.assertAlmostEqual(s["aps_persist_share"], (7 / 10 + 9 / 10) / 2)

    def test_no_change_gives_empty_share(self):
        rows = [dict(self.ROWS[1])]
        s = sim.summarize_pairs(rows)[0]
        self.assertEqual(s["sec_detected_share"], "")


class Files(unittest.TestCase):
    def test_sample_writes_chunk_files(self):
        with tempfile.TemporaryDirectory() as d:
            tl = os.path.join(d, "timeline.csv")
            with open(tl, "w") as f:
                f.write("ts\n" + "\n".join(str(1_790_000_000 + 30 * i) for i in range(5000)) + "\n")
            plan = sim.write_plan(tl, d, durations=[900, 3600], n=10, gaps=[3600], n_pairs=4,
                                  pair_duration=900, seed=1, chunk=4)
            self.assertEqual(sorted(plan), ["pairs", "windows"])
            files = sorted(os.listdir(os.path.join(d, "plan")))
            self.assertIn("windows_900_000.txt", files)
            self.assertIn("pairs_3600_000_t1.txt", files)
            with open(os.path.join(d, "plan", "windows_900_000.txt")) as f:
                self.assertTrue(f.read().startswith("{"))


class SharedSql(unittest.TestCase):
    """The blocks windows.sql shares with reference.sql (the AP presence
    classes) and with pairs.sql (the APs heard in a window) stay identical."""

    @staticmethod
    def block(name, sql_file):
        text = open(os.path.join(os.path.dirname(sim.__file__), sql_file)).read()
        start, end = "-- %s {" % name, "-- %s }" % name
        return text[text.index(start):text.index(end)]

    def test_apref(self):
        self.assertEqual(self.block("apref", "windows.sql"), self.block("apref", "reference.sql"))

    def test_seen(self):
        self.assertEqual(self.block("seen", "windows.sql"), self.block("seen", "pairs.sql"))


if __name__ == "__main__":
    unittest.main()
