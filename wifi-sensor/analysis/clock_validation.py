#!/usr/bin/env python3
"""Evaluate a clock-validation run (docs/clock-validation.md) on the Pi:
how far the live upload's timestamp correction (server/docs/live-upload.md
8.3) is from the true time.

    python3 /opt/wifi-sensor/analysis/clock_validation.py --truth ~/clock-truth-A30.csv
    python3 .../clock_validation.py --truth ~/clock-truth-D30.csv --compare-anchors
    python3 .../clock_validation.py --unverified

The correction is computed by the uploader's own code (`uploader/batch.py`
boots() and `uploader/timeline.py` place_poll(), found next to analysis/ in
the repo and under /opt/wifi-sensor), so this measures exactly what is sent.

Per boot of the truth file (default) or --boot:
- the anchors: NTP (buffer `clock_anchors`) and server (the uploader's
  state file), each as the boot's true epoch (anchor wall - anchor mono);
- the stale epoch (first unsynchronised poll: ts - mono) and the offset
  applied (true epoch - stale epoch, ~ -21600 s for a clock 6 h behind);
- per truth sample: corrected time of its monotonic instant (true epoch +
  pi_mono) minus the true time (the Mac's midpoint) - the anchor's error,
  for every anchor available;
- per unsynchronised poll inside the sampled stretch: the corrected ts the
  uploader sends (integer seconds) minus its true time interpolated from the
  truth samples on the monotonic clock - the end-to-end error;
- max |error| against the target (10 s; expected < 1 s).

--unverified lists boots whose unsynchronised polls never got an anchor
(run B): never uploaded, counted. --csv writes the per-sample errors.
Read-only (buffer opened mode=ro); standard library only.
"""

import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"
DEFAULT_STATE = "/var/lib/wifi-sensor-uploader/state.json"
TARGET_S = 10.0


def load_uploader(path):
    sys.path.insert(0, path)
    try:
        import batch  # noqa: F401
        import timeline  # noqa: F401
    except ImportError as e:
        sys.exit("cannot import the uploader's batch/timeline from %s: %s (--uploader-dir)" % (path, e))
    return sys.modules["batch"], sys.modules["timeline"]


def db_path(arg):
    if arg:
        return os.path.expanduser(arg)
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    conf = os.environ.get("COLLECTOR_CONF", DEFAULT_CONF)
    try:
        with open(conf) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == "DB_PATH" and v.strip():
                    return v.strip().strip("\"'")
    except OSError:
        pass
    return DEFAULT_DB


def open_ro(path):
    if not os.path.isfile(path):
        sys.exit("no database at %s" % path)
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    return db


def read_truth(path):
    rows = []
    with open(os.path.expanduser(path), newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "ok":
                continue
            rows.append({"true": float(r["true_ts"]), "half_rtt": float(r["half_rtt_s"]),
                         "mono": float(r["pi_mono"]), "wall": float(r["pi_wall"]), "boot": r["boot_id"]})
    return rows


def server_anchors(path):
    try:
        with open(path) as f:
            return (json.load(f) or {}).get("server_anchors") or {}
    except (OSError, ValueError):
        return {}


def current_boot():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def stats(errs):
    a = [abs(e) for e in errs]
    return {"n": len(errs), "mean": statistics.fmean(errs), "max_abs": max(a),
            "p95_abs": sorted(a)[min(len(a) - 1, int(round(0.95 * (len(a) - 1))))]}


def fmt_stats(s):
    return "n=%d mean %+.3f s, p95 |err| %.3f s, max |err| %.3f s" % (s["n"], s["mean"], s["p95_abs"], s["max_abs"])


def true_at(mono, samples):
    """True time of a monotonic instant from the truth samples (linear between
    the two around it; None outside the sampled stretch)."""
    for a, b in zip(samples, samples[1:]):
        if a["mono"] <= mono <= b["mono"]:
            if b["mono"] == a["mono"]:
                return a["true"]
            return a["true"] + (mono - a["mono"]) * (b["true"] - a["true"]) / (b["mono"] - a["mono"])
    return None


def anchors_of(db, boot, sanchors):
    out = []
    a = db.execute("SELECT wall, mono, source FROM clock_anchors WHERE boot_id = ?", (boot,)).fetchone()
    if a:
        out.append(("ntp", a["wall"] - a["mono"], a["wall"], a["mono"]))
    s = sanchors.get(boot)
    if s:
        out.append(("server", s["wall"] - s["mono"], s["wall"], s["mono"]))
    return out


def report_boot(db, batch, timeline, boot, truth, sanchors, compare, rows_out):
    print("\n== boot %s" % boot)
    n_all, n_uns = db.execute("SELECT count(*), coalesce(sum(clock_synced = 0), 0) FROM polls WHERE boot_id = ?",
                              (boot,)).fetchone()
    first = db.execute("SELECT min(ts), max(ts) FROM polls WHERE boot_id = ?", (boot,)).fetchone()
    print("polls: %d (%d unsynchronised), raw ts %s .. %s" % (n_all, n_uns, first[0], first[1]))
    b = batch.boots(db, {boot}, sanchors).get(boot)
    anchors = anchors_of(db, boot, sanchors)
    if b is not None and b.epoch_stale is not None:
        print("stale epoch (first unsynchronised poll): %.3f" % b.epoch_stale)
    if not anchors:
        print("no anchor (NTP or server): its unsynchronised polls are %s"
              % ("held back (current boot)" if boot == current_boot() else "UNVERIFIED - never uploaded"))
    for src, epoch, wall, mono in anchors:
        delta = None if b is None or b.epoch_stale is None else epoch - b.epoch_stale
        print("anchor %-6s: wall %.3f at mono %.3f -> true epoch %.3f%s" % (
            src, wall, mono, epoch, "" if delta is None else ", offset applied %+.1f s" % delta))
    if compare:
        by = {a[0]: a[1] for a in anchors}
        if "ntp" in by and "server" in by:
            print("anchors compared: server - ntp = %+.3f s" % (by["server"] - by["ntp"]))
        else:
            print("anchors compared: needs both an NTP and a server anchor (have: %s)" % (", ".join(by) or "none"))

    samples = sorted((t for t in truth if t["boot"] == boot), key=lambda t: t["mono"])
    worst = None
    if samples:
        print("truth: %d samples, mono %.0f .. %.0f, half round trip <= %.3f s" % (
            len(samples), samples[0]["mono"], samples[-1]["mono"], max(t["half_rtt"] for t in samples)))
        print("  the Pi's own clock vs true: %s" % fmt_stats(stats([t["wall"] - t["true"] for t in samples])))
        for src, epoch, _w, _m in anchors:
            errs = [epoch + t["mono"] - t["true"] for t in samples]
            s = stats(errs)
            print("  anchor %-6s corrected - true: %s" % (src, fmt_stats(s)))
            worst = s["max_abs"] if worst is None else max(worst, s["max_abs"])
            for t, e in zip(samples, errs):
                rows_out.append({"boot_id": boot, "anchor": src, "true_ts": "%.6f" % t["true"],
                                 "pi_mono": "%.6f" % t["mono"], "corrected_ts": "%.6f" % (epoch + t["mono"]),
                                 "error_s": "%.6f" % e, "half_rtt_s": "%.6f" % t["half_rtt"],
                                 "pi_wall_err_s": "%.3f" % (t["wall"] - t["true"])})
    elif truth:
        print("truth: no samples of this boot")

    if b is not None and b.anchored:
        polls = db.execute("SELECT id, ts, mono, boot_id, clock_synced FROM polls WHERE boot_id = ? "
                           "AND clock_synced = 0 ORDER BY id", (boot,)).fetchall()
        offsets, errs = [], []
        for p in polls:
            ts, off = timeline.place_poll(dict(p), b)
            if off is not None:
                offsets.append(off)
            t = true_at(p["mono"], samples) if samples and p["mono"] is not None else None
            if t is not None:
                errs.append(ts - t)
        if offsets:
            print("polls corrected (%s anchor, as the uploader sends them): %d, offset %+.0f .. %+.0f s"
                  % (b.source, len(offsets), min(offsets), max(offsets)))
        if errs:
            s = stats(errs)
            print("  sent ts - true (interpolated from the truth, integer ts): %s" % fmt_stats(s))
            worst = s["max_abs"] if worst is None else max(worst, s["max_abs"])
    return worst


def report_unverified(db, sanchors):
    cur = current_boot()
    rows = db.execute(
        "SELECT p.boot_id, count(*) AS n, min(p.ts) AS t0, max(p.ts) AS t1, min(p.mono) AS m0, max(p.mono) AS m1 "
        "FROM polls p LEFT JOIN clock_anchors a ON a.boot_id = p.boot_id "
        "WHERE p.clock_synced = 0 AND a.boot_id IS NULL GROUP BY p.boot_id ORDER BY min(p.id)").fetchall()
    print("\n== boots with unsynchronised polls and no NTP anchor")
    if not rows:
        print("none")
    for r in rows:
        if r["boot_id"] in sanchors:
            fate = "server anchor - uploaded"
        elif r["boot_id"] == cur:
            fate = "current boot - held back"
        else:
            fate = "UNVERIFIED - never uploaded"
        print("%s: %d polls, raw ts %s .. %s, mono %.0f .. %.0f - %s" % (
            r["boot_id"], r["n"], r["t0"], r["t1"], r["m0"] or 0, r["m1"] or 0, fate))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", nargs="?", help="buffer.db (default: collector config)")
    ap.add_argument("--truth", help="CSV from clock_truth.py")
    ap.add_argument("--boot", action="append", help="boot id to evaluate (repeatable; default: the truth's boots)")
    ap.add_argument("--state", default=DEFAULT_STATE, help="uploader state file (server anchors)")
    ap.add_argument("--uploader-dir", default=os.path.join(os.path.dirname(HERE), "uploader"))
    ap.add_argument("--compare-anchors", action="store_true", help="NTP vs server anchor (run D)")
    ap.add_argument("--unverified", action="store_true", help="boots that never got an anchor (run B)")
    ap.add_argument("--csv", help="write the per-sample errors here")
    args = ap.parse_args(argv)

    batch, timeline = load_uploader(args.uploader_dir)
    db = open_ro(db_path(args.db))
    if int(db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]) < 8:
        sys.exit("buffer older than v8: no clock data")
    sanchors = server_anchors(args.state)
    truth = read_truth(args.truth) if args.truth else []
    boots = args.boot or sorted({t["boot"] for t in truth}, key=lambda b: min(t["true"] for t in truth if t["boot"] == b))
    if not boots and not args.unverified:
        r = db.execute("SELECT boot_id FROM polls WHERE clock_synced = 0 AND boot_id IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
        boots = [r[0]] if r else []
        if not boots:
            print("no unsynchronised polls in the buffer")
    rows_out, worst = [], None
    for boot in boots:
        w = report_boot(db, batch, timeline, boot, truth, sanchors, args.compare_anchors, rows_out)
        if w is not None:
            worst = w if worst is None else max(worst, w)
    if args.unverified:
        report_unverified(db, sanchors)
    if args.csv and rows_out:
        with open(os.path.expanduser(args.csv), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0]))
            w.writeheader()
            w.writerows(rows_out)
        print("\nper-sample errors: %s" % args.csv)
    if worst is not None:
        print("\nmax |error| %.3f s - %s (target %.0f s, expected < 1 s)"
              % (worst, "PASS" if worst < TARGET_S else "FAIL", TARGET_S))
    return 0


if __name__ == "__main__":
    sys.exit(main())
