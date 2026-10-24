"""RQ3 (continuous vs one-shot): simulated one-shot audits drawn from the
stored data and compared with the continuous record. Pure functions plus the
file plumbing between export.sh's psql calls; standard library only.
README.md describes the method and every figure.

  plan    collecting-poll timeline -> the sampled audit windows and pairs,
          as Postgres array literals in chunk files (psql -v starts=...)
  report  the per-window / per-pair rows the SQL wrote + the continuous
          reference -> summary CSVs
"""

import bisect
import csv
import glob
import os
import random
import statistics
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone

POLL_S = 30                      # the collector's poll interval


# --- sampling ------------------------------------------------------------------------------------

def sample_starts(ts, n, duration_s, seed):
    """n audit starts drawn without replacement from the collecting polls ts
    (sorted unix seconds) whose window [t, t + duration) ends by the last poll.
    Uniform over collecting time, since polls are evenly spaced."""
    if not ts:
        return []
    cand = [t for t in ts if t + duration_s <= ts[-1]]
    if n >= len(cand):
        return cand
    return sorted(random.Random("%s:%s" % (seed, duration_s)).sample(cand, n))


def sample_pairs(ts, n, gap_s, duration_s, seed):
    """n (t1, t2) pairs: t1 a random collecting poll, t2 the first collecting
    poll at or after t1 + gap (a repeat audit comes back after the gap; when
    the sensor was down then, at its next collecting poll)."""
    if not ts:
        return []
    out = []
    for t1 in ts:
        i = bisect.bisect_left(ts, t1 + gap_s)
        if i < len(ts) and ts[i] + duration_s <= ts[-1]:
            out.append((t1, ts[i]))
    if n >= len(out):
        return out
    return sorted(random.Random("%s:pair:%s" % (seed, gap_s)).sample(out, n))


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pg_array(epochs):
    return "{" + ",".join(iso(t) for t in epochs) + "}"


def chunks(xs, size):
    return [xs[i:i + size] for i in range(0, len(xs), size)]


def read_timeline(path):
    with open(path, newline="") as f:
        return sorted(int(float(r["ts"])) for r in csv.DictReader(f))


def write_plan(timeline_csv, out_dir, durations, n, gaps, n_pairs, pair_duration, seed, chunk=25):
    """The sampled windows and pairs: plan/windows_<d>_<k>.txt, plan/pairs_<gap>_<k>_t1.txt and
    _t2.txt (array literals), and sampled.csv (every audit, for the record)."""
    ts = read_timeline(timeline_csv)
    pdir = os.path.join(out_dir, "plan")
    os.makedirs(pdir, exist_ok=True)
    plan = {"windows": OrderedDict(), "pairs": OrderedDict()}
    rows = []
    for d in durations:
        starts = sample_starts(ts, n, d, seed)
        plan["windows"][d] = []
        for k, c in enumerate(chunks(starts, chunk)):
            p = os.path.join(pdir, "windows_%d_%03d.txt" % (d, k))
            with open(p, "w") as f:
                f.write(pg_array(c))
            plan["windows"][d].append(p)
        rows += [("window", d, d, iso(t), "") for t in starts]
    for g in gaps:
        pairs = sample_pairs(ts, n_pairs, g, pair_duration, seed)
        plan["pairs"][g] = []
        for k, c in enumerate(chunks(pairs, chunk)):
            p1 = os.path.join(pdir, "pairs_%d_%03d_t1.txt" % (g, k))
            p2 = os.path.join(pdir, "pairs_%d_%03d_t2.txt" % (g, k))
            with open(p1, "w") as f:
                f.write(pg_array([a for a, _ in c]))
            with open(p2, "w") as f:
                f.write(pg_array([b for _, b in c]))
            plan["pairs"][g].append((p1, p2))
        rows += [("pair", g, pair_duration, iso(a), iso(b)) for a, b in pairs]
    with open(os.path.join(out_dir, "sampled.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["audit", "duration_or_gap_s", "window_s", "t1", "t2"])
        w.writerows(rows)
    return plan


# --- statistics ----------------------------------------------------------------------------------

def _q(xs, q):
    """Linear-interpolation quantile of sorted xs (numpy's default)."""
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def describe(values):
    xs = sorted(values)
    if not xs:
        return OrderedDict(n=0, mean="", median="", p10="", p90="", min="", max="")
    return OrderedDict(n=len(xs), mean=statistics.fmean(xs), median=statistics.median(xs),
                       p10=_q(xs, 0.1), p90=_q(xs, 0.9), min=xs[0], max=xs[-1])


def _mean(xs):
    return statistics.fmean(xs) if xs else ""


def _ratio(a, b):
    return a / b if b else ""


# --- report --------------------------------------------------------------------------------------

def read_long(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            rows += list(csv.DictReader(f))
    return rows


def summarize_windows(win_rows, ref_rows, duration_s):
    """Per (metric, key): the distribution over the simulated audits of one
    duration, against the continuous record's total (README.md)."""
    ref = {(r["metric"], r["key"]): float(r["n"]) for r in ref_rows}
    per = defaultdict(dict)
    starts = set()
    for r in win_rows:
        starts.add(r["t0"])
        per[(r["metric"], r["key"])][r["t0"]] = float(r["n"])
    starts = sorted(starts)
    collecting_s = ref.get(("polls", "collecting"), 0) * POLL_S
    out = []
    for key in sorted(set(per) | set(ref)):
        if key[0] == "hours":                     # a reference-only figure (the denominator of the classes)
            continue
        vals = [per[key].get(t, 0.0) for t in starts]
        cont = ref.get(key)
        d = describe(vals)
        row = OrderedDict(duration_s=duration_s, metric=key[0], key=key[1], windows=d.pop("n"),
                          continuous=int(cont) if cont is not None else "")
        row.update(d)
        row["mean_share"] = _mean([v / cont for v in vals]) if cont else ""
        row["p_any"] = _mean([1.0 if v > 0 else 0.0 for v in vals])
        row["mean_share_of_window"] = (_mean([v / (duration_s / POLL_S) for v in vals])
                                       if key == ("polls", "collecting") else "")
        row["time_share"] = _ratio(duration_s, collecting_s)
        out.append(row)
    return out


PAIR_INT = ("aps_t1", "aps_t2", "aps_both", "sec_aps_changed", "sec_transitions", "sec_aps_detected",
            "ch_aps_changed", "ch_transitions", "ch_aps_detected")


def summarize_pairs(rows):
    """Per gap: what a repeat audit (state at t1 vs state at t2, APs seen
    both times) detects of the changes the continuous record holds."""
    by = OrderedDict()
    for r in sorted(rows, key=lambda r: int(r["gap_s"])):
        by.setdefault(r["gap_s"], []).append({k: int(r[k]) for k in PAIR_INT})
    out = []
    for gap, rs in by.items():
        s = {k: sum(r[k] for r in rs) for k in PAIR_INT}
        row = OrderedDict(gap_s=gap, pairs=len(rs))
        row["aps_t1_mean"] = _mean([r["aps_t1"] for r in rs])
        row["aps_persist_share"] = _mean([r["aps_both"] / r["aps_t1"] for r in rs if r["aps_t1"]])
        row["aps_new_mean"] = _mean([r["aps_t2"] - r["aps_both"] for r in rs])
        row["aps_gone_mean"] = _mean([r["aps_t1"] - r["aps_both"] for r in rs])
        for p in ("sec", "ch"):
            row[p + "_aps_changed"] = s[p + "_aps_changed"]
            row[p + "_aps_detected"] = s[p + "_aps_detected"]
            row[p + "_transitions"] = s[p + "_transitions"]
            row[p + "_detected_share"] = _ratio(s[p + "_aps_detected"], s[p + "_aps_changed"])
            row[p + "_transitions_per_changed_ap"] = _ratio(s[p + "_transitions"], s[p + "_aps_changed"])
            row[p + "_pairs_any_change"] = _mean([1.0 if r[p + "_aps_changed"] else 0.0 for r in rs])
            row[p + "_pairs_any_detected"] = _mean([1.0 if r[p + "_aps_detected"] else 0.0 for r in rs])
        out.append(row)
    return out


def _fmt(v):
    return "%.6f" % v if isinstance(v, float) else v


def write_csv(path, rows):
    fields = list(rows[0].keys()) if rows else ["empty"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(v) for k, v in r.items()})


def report(out_dir):
    """raw/windows_<d>_<k>.csv, raw/pairs_<gap>_<k>.csv, reference.csv -> summary_windows.csv,
    summary_pairs.csv, windows.csv (wide, one row per audit), pairs.csv."""
    raw = os.path.join(out_dir, "raw")
    ref = read_long([os.path.join(out_dir, "reference.csv")])
    durations = sorted({int(os.path.basename(p).split("_")[1]) for p in glob.glob(os.path.join(raw, "windows_*.csv"))})
    summary, wide = [], []
    for d in durations:
        rows = read_long(sorted(glob.glob(os.path.join(raw, "windows_%d_*.csv" % d))))
        summary += summarize_windows(rows, ref, d)
        cols = sorted({(r["metric"], r["key"]) for r in rows})
        per = defaultdict(dict)
        for r in rows:
            per[r["t0"]][(r["metric"], r["key"])] = r["n"]
        for t0 in sorted(per):
            w = OrderedDict(duration_s=d, t0=t0)
            w.update(("%s:%s" % c, per[t0].get(c, "0")) for c in cols)
            wide.append(w)
    write_csv(os.path.join(out_dir, "summary_windows.csv"), summary)
    if wide:
        allcols = []
        for w in wide:
            allcols += [k for k in w if k not in allcols]
        wide = [OrderedDict((k, w.get(k, "0")) for k in allcols) for w in wide]
    write_csv(os.path.join(out_dir, "windows.csv"), wide)
    prows = read_long(sorted(glob.glob(os.path.join(raw, "pairs_*.csv"))))
    write_csv(os.path.join(out_dir, "pairs.csv"), prows)
    write_csv(os.path.join(out_dir, "summary_pairs.csv"), summarize_pairs(prows))
    return summary
