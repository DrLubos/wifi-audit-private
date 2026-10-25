"""python3 -m evaluation.rq3 plan TIMELINE_CSV --out DIR [options]
python3 -m evaluation.rq3 report DIR

plan: draw the simulated one-shot audits (windows) and repeat audits (pairs)
from the collecting-poll timeline; report: summarize what the SQL measured in
them against the continuous record. export.sh runs both around its psql
calls. Standard library only; touches no database."""

import argparse
import sys

from . import sim


def _ints(text):
    return [int(x) for x in text.split(",") if x.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m evaluation.rq3", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("timeline_csv")
    pl.add_argument("--out", required=True)
    pl.add_argument("--durations", type=_ints, default=[900, 3600], help="audit lengths, s (900,3600)")
    pl.add_argument("--n", type=int, default=100, help="audits per duration (100)")
    pl.add_argument("--gaps", type=_ints, default=[86400, 604800], help="repeat-audit gaps, s (1 d, 7 d)")
    pl.add_argument("--pairs", type=int, default=60, help="repeat-audit pairs per gap (60)")
    pl.add_argument("--pair-duration", type=int, default=3600, help="length of each audit of a pair, s")
    pl.add_argument("--seed", default="20261024")
    pl.add_argument("--chunk", type=int, default=25, help="windows per SQL statement (25)")
    pl.add_argument("--hours", choices=sim.HOURS, default="24h",
                    help="audit starts at any time (24h) or on weekdays 08:00-17:00 local (work)")
    pl.add_argument("--tz", default=sim.TZ)
    rp = sub.add_parser("report")
    rp.add_argument("out")
    a = p.parse_args(argv)
    if a.cmd == "plan":
        plan = sim.write_plan(a.timeline_csv, a.out, a.durations, a.n, a.gaps, a.pairs, a.pair_duration,
                              a.seed, a.chunk, a.hours, a.tz)
        print("planned: %s windows, %s pairs" % (
            {d: len(v) for d, v in plan["windows"].items()}, {g: len(v) for g, v in plan["pairs"].items()}))
        return 0
    sim.report(a.out)
    print("-> %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
