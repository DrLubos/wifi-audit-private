#!/usr/bin/env python3
"""Read-only RSSI baseline stability report for the collector buffer.

Answers, with numbers across ALL access points that have enough data:

  1. How stable is a fixed AP's RSSI as seen by a fixed sensor?
     -> distribution of per-AP std dev (plain and robust/MAD), quartiles,
        share of APs within 2/3/5/8 dB, broken down by band and by signal
        strength.
  2. Does the baseline drift from day to day?
     -> per-AP range and step of daily mean/median RSSI, distribution
        across APs.
  3. How often do genuine readings fall outside the baseline, and by how
     much?
     -> baseline (median + robust sigma) is fitted on the first part of each
        AP's history and evaluated on the rest; threshold sweep giving the
        pooled and per-AP false-alarm rate for "|rssi - baseline| > Y dB",
        the size of exceedances, their direction, and whether they come as
        single samples or runs of consecutive observations.

Nothing is detected or flagged; this is a description of the data so that a
threshold can be justified. No writes: the database is opened with the
SQLite read-only URI flag and query_only, so the collector can keep running.

Usage:  python3 rssi_stability.py [DB_PATH] [--min-obs N] [--min-days N]
                                  [--min-day-obs N] [--train-frac F]
                                  [--thresholds 3,5,8,...] [--no-table]
                                  [--csv FILE] [--utc]

DB_PATH defaults to DB_PATH from the environment, then from
/etc/wifi-sensor/collector.conf (or COLLECTOR_CONF), then
/var/lib/wifi-sensor/buffer.db - the same resolution the collector uses.
Only the standard library is needed.

Caveats printed with the report: an observation's `rssi` is Kismet's
`sig_last`, i.e. the RSSI of the most recent frame at poll time (a single
sample, not a poll-window average), and an observation exists only for polls
in which the device was active. `rssi_min`/`rssi_max` are Kismet's lifetime
extremes and are not used here.
"""

import argparse
import csv
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"
DEFAULT_THRESHOLDS = (3, 5, 6, 8, 10, 12, 15, 20)
MAD_TO_SIGMA = 1.4826          # MAD -> sigma for a normal distribution
RSSI_BUCKETS = ((-60, "strong  > -60 dBm"), (-70, "medium  -60..-70"),
                (-80, "weak    -70..-80"), (None, "faint   < -80"))

USE_UTC = False


# --- helpers ------------------------------------------------------------------

def read_kv_file(path):
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


def resolve_db_path(db_arg):
    """Same resolution order as collector.py / analyze.py."""
    path = DEFAULT_DB
    conf = os.environ.get("COLLECTOR_CONF", DEFAULT_CONF)
    if os.path.isfile(conf):
        try:
            path = read_kv_file(conf).get("DB_PATH", path)
        except OSError:
            pass
    path = os.environ.get("DB_PATH", path)
    if db_arg:
        path = db_arg
    return os.path.expanduser(path)


def open_readonly(path):
    if not os.path.isfile(path):
        sys.exit("no database at %s" % path)
    # mode=ro: SQLite refuses every write. For a WAL database the -shm file must
    # still be readable/writable (run as the sensor user, or via sudo -u).
    uri = "file:%s?mode=ro" % path
    db = sqlite3.connect(uri, uri=True, timeout=30)
    db.execute("PRAGMA query_only = 1")
    db.row_factory = sqlite3.Row
    return db


def fmt_ts(ts):
    if ts is None:
        return "-"
    ts = float(ts)
    if USE_UTC:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def day_of(ts):
    if USE_UTC:
        return datetime.fromtimestamp(ts, timezone.utc).date()
    return datetime.fromtimestamp(ts).date()


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "%dd %02dh %02dm" % (d, h, m)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


def section(title):
    print()
    print("=" * 78)
    print(" " + title)
    print("=" * 78)


def table(headers, rows, align_right=()):
    """Print a plain fixed-width table. align_right: set of column indexes."""
    rows = [["-" if v is None else str(v) for v in r] for r in rows]
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))

    def line(r):
        cells = []
        for i, v in enumerate(r):
            cells.append(v.rjust(widths[i]) if i in align_right else v.ljust(widths[i]))
        return "  " + "  ".join(cells).rstrip()
    print(line(headers))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def pct(sorted_vals, p):
    """Percentile 0..100 with linear interpolation; sorted_vals non-empty."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize(vals):
    """min, Q1, median, Q3, p90, max of a list (or None)."""
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(s), "min": s[0], "q1": pct(s, 25), "med": pct(s, 50),
            "q3": pct(s, 75), "p90": pct(s, 90), "max": s[-1]}


def summary_row(name, vals, fmt="%.1f"):
    s = summarize(vals)
    if s is None:
        return (name, 0, "-", "-", "-", "-", "-", "-")
    return (name, s["n"]) + tuple(fmt % s[k] for k in ("min", "q1", "med", "q3", "p90", "max"))


def within_counts(vals, limits):
    return tuple("%d (%.0f%%)" % (sum(1 for v in vals if v <= lim),
                                   100.0 * sum(1 for v in vals if v <= lim) / len(vals))
                 if vals else "-" for lim in limits)


def robust_sigma(vals, med=None):
    if med is None:
        med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    return MAD_TO_SIGMA * mad


def band_of(freq_khz):
    if not freq_khz:
        return "?"
    if freq_khz < 3_000_000:
        return "2.4"
    if freq_khz < 5_925_000:
        return "5"
    return "6"


def bucket_of(mean_rssi):
    for limit, name in RSSI_BUCKETS:
        if limit is None or mean_rssi > limit:
            return name
    return RSSI_BUCKETS[-1][1]


def runs_of(flags):
    """Lengths of runs of consecutive True values."""
    runs, cur = [], 0
    for f in flags:
        if f:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


# --- per-AP analysis --------------------------------------------------------------

def load_ap(db, key):
    """Observations of one AP with an RSSI reading, in time order."""
    return db.execute(
        "SELECT ts, rssi, freq_khz FROM observations "
        "WHERE key = ? AND rssi IS NOT NULL ORDER BY ts", (key,)).fetchall()


def analyze_ap(dev, obs, args, thresholds):
    ts = [r["ts"] for r in obs]
    rssi = [r["rssi"] for r in obs]
    n = len(rssi)

    freqs = defaultdict(int)
    for r in obs:
        if r["freq_khz"]:
            freqs[r["freq_khz"]] += 1
    main_freq = max(freqs, key=freqs.get) if freqs else None

    mean = statistics.fmean(rssi)
    med = statistics.median(rssi)
    sd = statistics.pstdev(rssi)
    rsd = robust_sigma(rssi, med)
    srt = sorted(rssi)

    # daily means/medians over days with enough observations
    per_day = defaultdict(list)
    for t, v in zip(ts, rssi):
        per_day[day_of(t)].append(v)
    days = sorted(d for d, vs in per_day.items() if len(vs) >= args.min_day_obs)
    daily_mean = [statistics.fmean(per_day[d]) for d in days]
    daily_med = [statistics.median(per_day[d]) for d in days]
    drift = None
    if len(days) >= 2:
        steps = [abs(a - b) for a, b in zip(daily_mean, daily_mean[1:])]
        drift = {
            "days": len(days),
            "mean_range": max(daily_mean) - min(daily_mean),
            "med_range": max(daily_med) - min(daily_med),
            "max_step": max(steps),
            "sd_of_means": statistics.pstdev(daily_mean),
        }

    # baseline on the first train_frac of the history, evaluated on the rest
    split = int(n * args.train_frac)
    outl = None
    if split >= args.min_train_obs and n - split >= args.min_test_obs:
        train, test = rssi[:split], rssi[split:]
        b_med = statistics.median(train)
        b_sig = robust_sigma(train, b_med)
        devs = [v - b_med for v in test]
        absdev = [abs(d) for d in devs]
        outl = {
            "b_med": b_med, "b_sig": b_sig, "n_test": len(test),
            "test_from": ts[split],
            "dev": devs, "absdev": absdev,
            "exceed": {y: sum(1 for d in absdev if d > y) for y in thresholds},
            "runs": {y: runs_of([d > y for d in absdev]) for y in thresholds},
            "exceed_3sig": sum(1 for d in absdev if b_sig > 0 and d > 3 * b_sig),
        }

    return {
        "key": dev["key"], "mac": dev["mac"], "ssid": dev["ssid"],
        "band": band_of(main_freq), "nfreq": len(freqs),
        "n": n, "first": ts[0], "last": ts[-1], "ndays": len(per_day),
        "mean": mean, "med": med, "sd": sd, "rsd": rsd,
        "iqr": pct(srt, 75) - pct(srt, 25),
        "p5": pct(srt, 5), "p95": pct(srt, 95), "min": srt[0], "max": srt[-1],
        "bucket": bucket_of(mean),
        "drift": drift, "outl": outl,
    }


# --- report sections --------------------------------------------------------------

def show_dataset(db, aps, skipped, args):
    section("DATASET")
    polls = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts), SUM(ok) FROM polls").fetchone()
    n_ap = db.execute("SELECT COUNT(*) FROM devices WHERE type = 'ap'").fetchone()[0]
    n_obs = db.execute(
        "SELECT COUNT(*) FROM observations o JOIN devices d USING(key) "
        "WHERE d.type = 'ap' AND o.rssi IS NOT NULL").fetchone()[0]
    print("  polls: %d (%d ok), %s .. %s (%s)" % (
        polls[0] or 0, polls[3] or 0, fmt_ts(polls[1]), fmt_ts(polls[2]),
        fmt_dur((polls[2] or 0) - (polls[1] or 0))))
    print("  APs in buffer: %d; AP observations with an RSSI reading: %d" % (n_ap, n_obs))
    print("  qualifying APs (>= %d readings on >= %d distinct days): %d; skipped: %d" % (
        args.min_obs, args.min_days, len(aps), skipped))
    print("  qualifying readings: %d" % sum(a["n"] for a in aps))
    print("  baseline split: first %.0f%% of each AP's readings -> baseline, rest -> test"
          % (100 * args.train_frac))
    print("  day boundaries: %s" % ("UTC" if USE_UTC else "local time"))


def show_per_ap(aps):
    section("PER-AP RSSI STABILITY (all %d qualifying APs, sorted by std dev)" % len(aps))
    rows = []
    for a in sorted(aps, key=lambda a: a["sd"]):
        d = a["drift"]
        rows.append((
            (a["ssid"] or "<hidden>")[:22], a["mac"], a["band"], a["nfreq"],
            a["ndays"], a["n"], "%.1f" % a["mean"], "%.0f" % a["med"],
            "%.1f" % a["sd"], "%.1f" % a["rsd"], "%.0f" % a["iqr"],
            "%.0f..%.0f" % (a["p5"], a["p95"]), "%d..%d" % (a["min"], a["max"]),
            "%.1f" % d["mean_range"] if d else "-",
            "%.1f" % d["max_step"] if d else "-",
        ))
    table(["ssid", "bssid", "band", "nfq", "days", "n", "mean", "med", "sd", "rsd",
           "iqr", "p5..p95", "min..max", "dayrng", "daystep"],
          rows, align_right={3, 4, 5, 6, 7, 8, 9, 10, 13, 14})
    print("  sd = population std dev; rsd = 1.4826 * MAD (robust sigma, ignores outliers);")
    print("  iqr = interquartile range; nfq = distinct frequencies heard on (channel changes);")
    print("  dayrng = max - min of daily mean RSSI; daystep = largest day-to-day change of it.")


def show_sd_distribution(aps):
    section("DISTRIBUTION OF PER-AP STABILITY (one value per AP)")
    sds = [a["sd"] for a in aps]
    rsds = [a["rsd"] for a in aps]
    iqrs = [a["iqr"] for a in aps]
    spans = [a["p95"] - a["p5"] for a in aps]
    table(["metric (dB)", "APs", "min", "Q1", "median", "Q3", "p90", "max"], [
        summary_row("std dev", sds),
        summary_row("robust sd (MAD)", rsds),
        summary_row("IQR", iqrs),
        summary_row("p95 - p5 span", spans),
    ], align_right={1, 2, 3, 4, 5, 6, 7})

    limits = (2, 3, 5, 8)
    print()
    table(["APs with metric <=", "2 dB", "3 dB", "5 dB", "8 dB"], [
        ("std dev",) + within_counts(sds, limits),
        ("robust sd",) + within_counts(rsds, limits),
        ("p95 - p5 span",) + within_counts(spans, limits),
    ], align_right={1, 2, 3, 4})

    print()
    print("  by band:")
    groups = defaultdict(list)
    for a in aps:
        groups[a["band"]].append(a)
    table(["band", "APs", "readings", "median sd", "Q3 sd", "median rsd", "Q3 rsd",
           "sd<=3dB", "sd<=5dB"],
          [group_row(g + " GHz", v) for g, v in sorted(groups.items())],
          align_right={1, 2, 3, 4, 5, 6, 7, 8})

    print()
    print("  by mean signal strength (weak signals sit closer to the noise floor):")
    groups = defaultdict(list)
    for a in aps:
        groups[a["bucket"]].append(a)
    order = [name for _, name in RSSI_BUCKETS]
    table(["mean RSSI bucket", "APs", "readings", "median sd", "Q3 sd", "median rsd", "Q3 rsd",
           "sd<=3dB", "sd<=5dB"],
          [group_row(g, groups[g]) for g in order if g in groups],
          align_right={1, 2, 3, 4, 5, 6, 7, 8})

    print()
    print("  by channel behaviour:")
    fixed = [a for a in aps if a["nfreq"] <= 1]
    hopping = [a for a in aps if a["nfreq"] > 1]
    table(["channel", "APs", "readings", "median sd", "Q3 sd", "median rsd", "Q3 rsd",
           "sd<=3dB", "sd<=5dB"],
          [group_row("single frequency", fixed), group_row("several frequencies", hopping)],
          align_right={1, 2, 3, 4, 5, 6, 7, 8})


def group_row(name, aps):
    if not aps:
        return (name, 0, 0, "-", "-", "-", "-", "-", "-")
    sds = sorted(a["sd"] for a in aps)
    rsds = sorted(a["rsd"] for a in aps)
    return (name, len(aps), sum(a["n"] for a in aps),
            "%.1f" % pct(sds, 50), "%.1f" % pct(sds, 75),
            "%.1f" % pct(rsds, 50), "%.1f" % pct(rsds, 75),
            within_counts(sds, (3,))[0], within_counts(sds, (5,))[0])


def show_drift(aps, args):
    section("DAY-TO-DAY DRIFT OF THE BASELINE (daily mean RSSI per AP)")
    with_drift = [a for a in aps if a["drift"]]
    print("  APs with >= 2 days of >= %d readings: %d of %d" % (
        args.min_day_obs, len(with_drift), len(aps)))
    if not with_drift:
        print("  (not enough multi-day data yet)")
        return
    rng = [a["drift"]["mean_range"] for a in with_drift]
    mrng = [a["drift"]["med_range"] for a in with_drift]
    step = [a["drift"]["max_step"] for a in with_drift]
    sdm = [a["drift"]["sd_of_means"] for a in with_drift]
    print()
    table(["metric (dB, one value per AP)", "APs", "min", "Q1", "median", "Q3", "p90", "max"], [
        summary_row("range of daily means (max - min)", rng),
        summary_row("range of daily medians", mrng),
        summary_row("largest day-to-day step of the mean", step),
        summary_row("std dev of daily means", sdm),
    ], align_right={1, 2, 3, 4, 5, 6, 7})
    print()
    limits = (1, 2, 3, 5)
    table(["APs with metric <=", "1 dB", "2 dB", "3 dB", "5 dB"], [
        ("range of daily means",) + within_counts(rng, limits),
        ("largest day step",) + within_counts(step, limits),
    ], align_right={1, 2, 3, 4})
    print()
    print("  drift vs within-day noise: median (range of daily means) / median (robust sd) = %.2f"
          % (statistics.median(rng) / max(statistics.median(a["rsd"] for a in with_drift), 0.01)))
    print("  (a ratio well below ~2 means the day-to-day movement of the mean is small compared")
    print("   with the sample-to-sample scatter, i.e. one static baseline per AP is adequate)")


def show_outliers(aps, thresholds, args):
    section("OUT-OF-BASELINE READINGS (baseline from first %.0f%%, evaluated on the rest)"
            % (100 * args.train_frac))
    ev = [a for a in aps if a["outl"]]
    print("  APs with >= %d baseline and >= %d test readings: %d of %d" % (
        args.min_train_obs, args.min_test_obs, len(ev), len(aps)))
    if not ev:
        print("  (not enough data yet)")
        return
    n_test = sum(a["outl"]["n_test"] for a in ev)
    pooled_abs = sorted(d for a in ev for d in a["outl"]["absdev"])
    pooled_dev = [d for a in ev for d in a["outl"]["dev"]]
    print("  test readings: %d across %d APs" % (n_test, len(ev)))
    print("  baseline = median of the training part; deviation = reading - baseline median")
    print()
    print("  size of |deviation| over all test readings (dB):")
    table(["p50", "p90", "p95", "p99", "p99.9", "max"],
          [tuple("%.1f" % pct(pooled_abs, p) for p in (50, 90, 95, 99, 99.9)) + (pooled_abs[-1],)],
          align_right={0, 1, 2, 3, 4, 5})

    print()
    print("  threshold sweep - what a rule '|reading - baseline| > Y dB' would flag on GENUINE data:")
    rows = []
    for y in thresholds:
        exc = [a["outl"]["exceed"][y] for a in ev]
        rates = [100.0 * e / a["outl"]["n_test"] for e, a in zip(exc, ev)]
        pooled = 100.0 * sum(exc) / n_test
        runs = [r for a in ev for r in a["outl"]["runs"][y]]
        clean = sum(1 for e in exc if e == 0)
        high = sum(1 for a in ev for d in a["outl"]["dev"] if d > y)
        low = sum(exc) - high
        rows.append((
            "%d" % y, "%.3f%%" % pooled, "%.3f%%" % statistics.median(rates),
            "%.2f%%" % pct(sorted(rates), 90), "%.2f%%" % max(rates),
            "%d/%d" % (clean, len(ev)),
            len(runs), sum(1 for r in runs if r >= 2), sum(1 for r in runs if r >= 3),
            max(runs) if runs else 0,
            "%d/%d" % (high, low),
        ))
    table(["Y dB", "pooled", "median AP", "p90 AP", "worst AP", "clean APs",
           "runs", ">=2", ">=3", "maxrun", "stronger/weaker"],
          rows, align_right={0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10})
    print("  pooled = share of all test readings beyond Y; median/p90/worst AP = per-AP share;")
    print("  clean APs = APs with no reading beyond Y; runs = separate excursions beyond Y,")
    print("  >=2/>=3 = excursions lasting that many consecutive observations of the AP")
    print("  (consecutive observations, not necessarily consecutive polls); stronger/weaker =")
    print("  deviations above vs below the baseline (an impostor nearer the sensor would be stronger).")

    print()
    print("  sigma-based rule for comparison: |deviation| > 3 * robust sigma of the AP's baseline:")
    exc3 = sum(a["outl"]["exceed_3sig"] for a in ev)
    bsig = sorted(a["outl"]["b_sig"] for a in ev)
    print("    flags %.3f%% of test readings; baseline robust sigma per AP: median %.1f, Q3 %.1f,"
          " max %.1f dB" % (100.0 * exc3 / n_test, pct(bsig, 50), pct(bsig, 75), bsig[-1]))
    print("    -> 3 sigma corresponds to a median threshold of %.1f dB" % (3 * pct(bsig, 50)))

    print()
    print("  how far beyond a threshold do genuine exceedances go (excess over Y, dB):")
    rows = []
    for y in thresholds:
        excess = sorted(d - y for d in pooled_abs if d > y)
        if not excess:
            rows.append(("%d" % y, 0, "-", "-", "-"))
            continue
        rows.append(("%d" % y, len(excess), "%.1f" % pct(excess, 50),
                     "%.1f" % pct(excess, 90), "%.1f" % excess[-1]))
    table(["Y dB", "count", "median excess", "p90 excess", "max excess"], rows,
          align_right={0, 1, 2, 3, 4})

    print()
    print("  APs with the highest out-of-baseline share at %d dB:" % args.focus)
    y = args.focus
    worst = sorted(ev, key=lambda a: -a["outl"]["exceed"][y] / a["outl"]["n_test"])[:10]
    table(["ssid", "bssid", "band", "baseline", "bsig", "n_test", ">%ddB" % y, "share", "maxrun"],
          [((a["ssid"] or "<hidden>")[:22], a["mac"], a["band"], "%.0f" % a["outl"]["b_med"],
            "%.1f" % a["outl"]["b_sig"], a["outl"]["n_test"], a["outl"]["exceed"][y],
            "%.2f%%" % (100.0 * a["outl"]["exceed"][y] / a["outl"]["n_test"]),
            max(a["outl"]["runs"][y]) if a["outl"]["runs"][y] else 0) for a in worst],
          align_right={3, 4, 5, 6, 7, 8})


def show_conclusion(aps, thresholds, args):
    section("HEADLINE NUMBERS")
    sds = sorted(a["sd"] for a in aps)
    rsds = sorted(a["rsd"] for a in aps)
    print("  Across %d fixed APs observed by the fixed sensor:" % len(aps))
    print("    per-AP RSSI std dev: median %.1f dB, Q3 %.1f dB, p90 %.1f dB (robust: %.1f / %.1f / %.1f)"
          % (pct(sds, 50), pct(sds, 75), pct(sds, 90), pct(rsds, 50), pct(rsds, 75), pct(rsds, 90)))
    for lim in (3, 5):
        print("    APs with std dev <= %d dB: %s" % (lim, within_counts(sds, (lim,))[0]))
    with_drift = [a for a in aps if a["drift"]]
    if with_drift:
        rng = sorted(a["drift"]["mean_range"] for a in with_drift)
        print("    day-to-day range of the mean: median %.1f dB, p90 %.1f dB, max %.1f dB (%d APs)"
              % (pct(rng, 50), pct(rng, 90), rng[-1], len(with_drift)))
    ev = [a for a in aps if a["outl"]]
    if ev:
        n_test = sum(a["outl"]["n_test"] for a in ev)
        print("    genuine readings beyond the AP's own baseline median (%d test readings):"
              % n_test)
        for y in thresholds:
            exc = sum(a["outl"]["exceed"][y] for a in ev)
            runs = [r for a in ev for r in a["outl"]["runs"][y]]
            print("      > %2d dB: %7.3f%% of readings, %5d excursions, %4d lasting >= 3 observations"
                  % (y, 100.0 * exc / n_test, len(runs), sum(1 for r in runs if r >= 3)))
    print()
    print("  Caveats: `rssi` is Kismet sig_last (one frame per poll, not an average); readings")
    print("  exist only for polls where the AP was active; APs that moved (phone hotspots) are")
    print("  not filtered beyond the min-days rule; baseline/test split is by time within each AP.")


def write_csv(path, aps, thresholds):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        head = ["key", "mac", "ssid", "band", "nfreq", "ndays", "n", "first_ts", "last_ts",
                "mean", "median", "sd", "robust_sd", "iqr", "p5", "p95", "min", "max",
                "drift_days", "daily_mean_range", "daily_median_range", "daily_max_step",
                "sd_of_daily_means", "baseline_median", "baseline_robust_sd", "n_test",
                "test_absdev_p99", "test_absdev_max"]
        head += ["exceed_%ddb" % y for y in thresholds]
        head += ["runs3_%ddb" % y for y in thresholds]
        w.writerow(head)
        for a in aps:
            d, o = a["drift"], a["outl"]
            row = [a["key"], a["mac"], a["ssid"] or "", a["band"], a["nfreq"], a["ndays"], a["n"],
                   a["first"], a["last"], "%.2f" % a["mean"], a["med"], "%.2f" % a["sd"],
                   "%.2f" % a["rsd"], a["iqr"], a["p5"], a["p95"], a["min"], a["max"]]
            row += ([d["days"], "%.2f" % d["mean_range"], "%.2f" % d["med_range"],
                     "%.2f" % d["max_step"], "%.2f" % d["sd_of_means"]] if d else [""] * 5)
            if o:
                row += [o["b_med"], "%.2f" % o["b_sig"], o["n_test"],
                        "%.1f" % pct(sorted(o["absdev"]), 99), max(o["absdev"])]
                row += [o["exceed"][y] for y in thresholds]
                row += [sum(1 for r in o["runs"][y] if r >= 3) for y in thresholds]
            else:
                row += [""] * (5 + 2 * len(thresholds))
            w.writerow(row)
    print("\n  per-AP table written to %s" % path)


# --- main ---------------------------------------------------------------------------

def main():
    global USE_UTC
    ap = argparse.ArgumentParser(description="Read-only RSSI baseline stability report.")
    ap.add_argument("db", nargs="?", help="path to buffer.db (default: collector config)")
    ap.add_argument("--min-obs", type=int, default=200,
                    help="minimum RSSI readings for an AP to qualify (default 200)")
    ap.add_argument("--min-days", type=int, default=2,
                    help="minimum distinct days an AP must be seen on (default 2)")
    ap.add_argument("--min-day-obs", type=int, default=20,
                    help="readings a day needs to count in the drift analysis (default 20)")
    ap.add_argument("--train-frac", type=float, default=0.5,
                    help="share of each AP's history used as baseline (default 0.5)")
    ap.add_argument("--min-train-obs", type=int, default=100,
                    help="minimum baseline readings for the outlier analysis (default 100)")
    ap.add_argument("--min-test-obs", type=int, default=50,
                    help="minimum test readings for the outlier analysis (default 50)")
    ap.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
                    help="dB thresholds for the sweep (default %s)"
                         % ",".join(str(t) for t in DEFAULT_THRESHOLDS))
    ap.add_argument("--focus", type=int, default=8,
                    help="threshold for the worst-AP list (default 8 dB)")
    ap.add_argument("--no-table", action="store_true", help="skip the long per-AP table")
    ap.add_argument("--csv", metavar="FILE", help="also write the per-AP table as CSV")
    ap.add_argument("--utc", action="store_true", help="day boundaries and times in UTC")
    args = ap.parse_args()
    USE_UTC = args.utc
    if not 0.1 <= args.train_frac <= 0.9:
        sys.exit("--train-frac must be between 0.1 and 0.9")
    thresholds = tuple(sorted({int(t) for t in args.thresholds.split(",") if t.strip()}))
    if args.focus not in thresholds:
        thresholds = tuple(sorted(thresholds + (args.focus,)))

    path = resolve_db_path(args.db)
    db = open_readonly(path)
    print("wifi-sensor RSSI stability report  -  %s  -  %s" % (path, fmt_ts(datetime.now().timestamp())))

    candidates = db.execute(
        "SELECT d.key, d.mac, d.ssid, COUNT(o.rssi) AS n "
        "FROM devices d JOIN observations o ON o.key = d.key "
        "WHERE d.type = 'ap' AND o.rssi IS NOT NULL "
        "GROUP BY d.key HAVING n >= ? ORDER BY n DESC", (args.min_obs,)).fetchall()
    skipped = db.execute(
        "SELECT COUNT(*) FROM (SELECT key FROM observations o JOIN devices d USING(key) "
        "WHERE d.type = 'ap' AND o.rssi IS NOT NULL GROUP BY key)").fetchone()[0] - len(candidates)

    aps = []
    for dev in candidates:                 # one AP in memory at a time
        obs = load_ap(db, dev["key"])
        a = analyze_ap(dev, obs, args, thresholds)
        if a["ndays"] < args.min_days:
            skipped += 1
            continue
        aps.append(a)

    show_dataset(db, aps, skipped, args)
    if not aps:
        print("\n  no AP qualifies yet - lower --min-obs/--min-days or let the sensor run longer")
        return
    if not args.no_table:
        show_per_ap(aps)
    show_sd_distribution(aps)
    show_drift(aps, args)
    show_outliers(aps, thresholds, args)
    show_conclusion(aps, thresholds, args)
    if args.csv:
        write_csv(args.csv, aps, thresholds)


if __name__ == "__main__":
    main()
