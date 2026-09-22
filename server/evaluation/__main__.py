"""python -m evaluation <command> [options]  -  staged-attack evaluation (read-only).

  # 1. on the attacker/analysis box (needs tshark): verify frame counts + first-frame times
  python -m evaluation truth-check groundtruth.csv -o groundtruth.enriched.csv

  # 2. where the database is reachable: join detections against the ground truth
  python -m evaluation report --groundtruth groundtruth.enriched.csv \\
      --sensor pi-fri --from 2026-09-24 --to 2026-09-25 \\
      --counter-only-json counter_only.json --out docs/evaluation.md

The counterfactual column comes from a detector dry-run, so counter-only never
touches the database:
  python -m detection deauth-flood --from .. --to .. --counter-only --dry-run --json > counter_only.json
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

from detection.common import UTC, POLL_INTERVAL_S, fmt_ts, iso
from . import groundtruth, report
from .metrics import Detection, FP_GUARD, evaluate

# Detections whose episode starts within this of the window edge are fetched too,
# so an episode straddling --from/--to is not missed.
_EDGE = timedelta(hours=1)


# --- database reads (READ ONLY) -------------------------------------------------

_DETECTIONS_SQL = """
SELECT id, ts, severity, device_key, mac::text AS mac, ssid, evidence
FROM detections
WHERE sensor_id = %(sid)s AND type = %(type)s AND ts >= %(lo)s AND ts < %(hi)s
ORDER BY ts"""

_POLLS_SQL = """
SELECT ts FROM polls
WHERE sensor_id = %(sid)s AND ok AND ts >= %(lo)s AND ts < %(hi)s
ORDER BY ts"""


def fetch_detections(conn, sid, dtype, frm, to):
    rows = conn.execute(_DETECTIONS_SQL, {
        "sid": sid, "type": dtype, "lo": frm - _EDGE, "hi": to + _EDGE}).fetchall()
    return [Detection.from_row(r) for r in rows]


def fetch_ok_poll_times(conn, sid, frm, to):
    return [r["ts"] for r in conn.execute(_POLLS_SQL, {"sid": sid, "lo": frm, "hi": to})]


def clean_hours(poll_times, trials):
    """Hours of ok polls that fall outside every attack trial's guard band."""
    attack = [t for t in trials if t.is_attack]
    n = 0
    for ts in poll_times:
        if not any(t.start - FP_GUARD <= ts <= t.stop + FP_GUARD for t in attack):
            n += 1
    return n * POLL_INTERVAL_S / 3600.0


def _detections_from_json(path):
    with open(path, encoding="utf-8") as f:
        findings = json.load(f)
    # a finding dict from deauth_flood --json has id/mac/ssid/device_key/severity/evidence
    return [Detection.from_row({
        "id": f.get("id") or 0, "mac": f.get("mac"), "ssid": f.get("ssid"),
        "device_key": f.get("device_key"), "severity": f["severity"],
        "evidence": f.get("evidence") or {}}) for f in findings]


# --- report command -------------------------------------------------------------

def cmd_report(args):
    trials = groundtruth.load_trials(args.groundtruth)
    dtype = args.detector_type
    bad = groundtruth.overlaps([t for t in trials if t.type == dtype])
    if bad:
        print("warning: overlapping trial windows (ambiguous): %s"
              % ", ".join("%s/%s" % p for p in bad), file=sys.stderr)

    counter = _detections_from_json(args.counter_only_json) if args.counter_only_json else []

    if args.detections_json:
        # DB-less path (dry-run verification, or running away from the box).
        from detection.common import window_from_args
        frm, to = window_from_args(args.from_, args.to)
        detections = _detections_from_json(args.detections_json)
        poll_times, sensor_name = [], args.sensor or "(json)"
    else:
        from detection import common
        conn = common.connect()
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                sensor = common.get_sensor(conn, args.sensor)
                frm, to = common.window_from_args(args.from_, args.to)
                detections = fetch_detections(conn, sensor["id"], dtype, frm, to)
                poll_times = fetch_ok_poll_times(conn, sensor["id"], frm, to)
        finally:
            conn.close()
        sensor_name = sensor["name"]

    cells_v2, per_trial, fps, strays = evaluate(trials, detections, dtype)
    cells_counter = evaluate(trials, counter, dtype)[0] if counter else {}

    attack = [t for t in trials if t.type == dtype and t.is_attack]
    clean = [t for t in trials if t.type == dtype and not t.is_attack]
    ch = clean_hours(poll_times, trials) if poll_times else 0.0
    versions = {d.evidence.get("version") for d in detections if d.evidence.get("version")}
    meta = {
        "generated_at": datetime.now(UTC), "sensor": sensor_name,
        "from": fmt_ts(frm), "to": fmt_ts(to),
        "n_attack": len(attack), "n_clean": len(clean), "n_detections": len(detections),
        "detector_version": ", ".join(str(v) for v in sorted(versions)) if versions else "?",
        "csv_name": args.csv_name, "method_note": None,
    }
    if meta["method_note"] is None:
        meta.pop("method_note")

    md = report.render_markdown(meta, cells_v2, cells_counter, per_trial, fps, strays,
                                ch, args.background_note)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    csv_path = args.out.rsplit("/", 1)[0] + "/" + args.csv_name if "/" in args.out else args.csv_name
    report.write_csv(csv_path, per_trial)

    detected = sum(1 for r in per_trial if r["detected"] and r["variant"] != "clean")
    print("wrote %s and %s" % (args.out, csv_path), file=sys.stderr)
    print("  attack trials %d, detected %d, false positives %d, strays %d"
          % (len(attack), detected, len(fps), len(strays)), file=sys.stderr)
    return 0


# --- truth-check command --------------------------------------------------------

def cmd_truth_check(args):
    trials = groundtruth.load_trials(args.groundtruth)
    n = groundtruth.enrich_from_pcaps(trials, log=lambda m: print(m, file=sys.stderr))
    out = args.out or args.groundtruth
    groundtruth.write_trials(out, trials)
    mism = [t.trial_id for t in trials if t.frame_count_ok == "MISMATCH"]
    print("checked %d pcap(s); wrote %s" % (n, out), file=sys.stderr)
    if mism:
        print("  frame-count MISMATCH on: %s" % ", ".join(mism), file=sys.stderr)
        return 1 if args.strict else 0
    return 0


# --- CLI ------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m evaluation",
        description="Staged-attack evaluation of the batch detectors (read-only on the DB).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    r = sub.add_parser("report", help="join detections against the ground truth")
    r.add_argument("--groundtruth", required=True, metavar="CSV")
    r.add_argument("--sensor", metavar="NAME", help="sensors.name (default: the first sensor)")
    r.add_argument("--from", dest="from_", metavar="WHEN", help="window start, ISO 8601")
    r.add_argument("--to", metavar="WHEN", help="window end (exclusive)")
    r.add_argument("--detector-type", default="deauth_flood", metavar="TYPE")
    r.add_argument("--counter-only-json", metavar="FILE",
                   help="a `deauth-flood --counter-only --dry-run --json` dump for the old-vs-new table")
    r.add_argument("--detections-json", metavar="FILE",
                   help="read detections from this JSON instead of the DB (no DB needed)")
    r.add_argument("--out", default="docs/evaluation.md", metavar="PATH")
    r.add_argument("--csv-name", default="evaluation_trials.csv", metavar="NAME")
    r.add_argument("--background-note", default="2 organic events over ~5 seeded days",
                   metavar="TEXT", help="the real-background FP figure for context")
    r.set_defaults(func=cmd_report)

    t = sub.add_parser("truth-check", help="fill/verify frame counts and first-frame times from pcaps")
    t.add_argument("groundtruth", metavar="CSV")
    t.add_argument("-o", "--out", metavar="CSV", help="write enriched CSV here (default: in place)")
    t.add_argument("--strict", action="store_true", help="exit non-zero on any frame-count mismatch")
    t.set_defaults(func=cmd_truth_check)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
