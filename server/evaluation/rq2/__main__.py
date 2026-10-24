"""python3 -m evaluation.rq2 EVENTS_RAW_CSV --out DIR

Reads the per-event export of events.sql (export.sh runs both) and writes the
RQ2 tables as CSV into DIR. Standard library only; touches no database."""

import argparse
import sys

from . import analysis


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m evaluation.rq2", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("events_csv")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    rows = analysis.run(args.events_csv, args.out)
    s = {r["metric"]: r["value"] for r in analysis.summary(rows)}
    print("%d events (%d security, %d sensor), %d labelled, %d traced to a known cause -> %s" % (
        s["events"], s["security_events"], s["sensor_events"], s["labelled"], s["events_traced"], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
