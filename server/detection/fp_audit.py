"""fp_audit - read-only false-positive audit of evil_twin signal (b).

    python -m detection fp-audit --from 2026-09-15 --to 2026-09-28
    python -m detection fp-audit --from 2026-09-15 --to 2026-09-28 --mode floor-share

No rogue AP is present in the seeded data, so every (b) detection is a false
positive. Nothing is written: the analysis runs in a READ ONLY transaction and
the split baselines are computed with the same SQL as refresh_ap_baselines()
but as a SELECT (the older README method called refresh_ap_baselines(), which
rewrites ap_baselines).

--mode audit (default), one streaming pass over the readings (bounded memory,
one AP at a time):
  circular   the stored ap_baselines (fitted on the whole span) over
             [--from, --to) with the given parameters - what evil-twin reports;
  circular2  the stored baselines evaluated on the second half only, per k;
  split      baselines refitted on the first half [data start, split point)
             and evaluated on the second half, per k.
Each emitted detection is attributed to a cause: a documented degraded window
(degraded_windows.csv), an advertised-channel change of the AP within an hour,
else "open"; plus the AP kind (campus / randomised / other).

--mode floor-share: per AP the share f of censored floor readings and the
bias of the valid-only median, Q(0.5) - Q((0.5 - f) / (1 - f)) of the valid
readings; the admissible --max-floor-share interval is [largest f whose bias
stays within max(robust sd, 1 dB), smallest f whose bias does not) - see
wifi-sensor/docs/findings.md section 11.
"""

import csv
import itertools
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import common, evil_twin as et
from .common import fmt_ts

DEGRADED_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "degraded_windows.csv")

# refresh_ap_baselines() as a SELECT (schema.sql, schema 4): valid readings for
# the statistics, floors counted in n_floor, same qualification.
SPLIT_SQL = """
WITH obs AS (
  SELECT o.device_key, rssi_valid(o.rssi) AS rssi, rssi_is_floor(o.rssi, o.rssi_floor) AS is_floor,
         (o.ts AT TIME ZONE %(tz)s)::date AS obs_day
  FROM observations o
  JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
  WHERE o.sensor_id = %(sid)s AND d.type = 'ap' AND (o.rssi IS NOT NULL OR o.rssi_floor)
    AND o.ts >= %(p_from)s AND o.ts < %(p_until)s
), stats AS (
  SELECT device_key, count(rssi)::integer AS n_obs,
         count(*) FILTER (WHERE is_floor)::integer AS n_floor,
         count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL)::integer AS n_days,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi) AS med
  FROM obs GROUP BY device_key
  HAVING count(rssi) >= %(min_obs)s
     AND count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL) >= %(min_days)s
), mad AS (
  SELECT o.device_key, percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(o.rssi - s.med)) AS mad
  FROM obs o JOIN stats s ON s.device_key = o.device_key
  WHERE o.rssi IS NOT NULL GROUP BY o.device_key
)
SELECT s.device_key, s.med AS rssi_median, 1.4826 * m.mad AS rssi_robust_sd,
       s.n_obs, s.n_floor, s.n_days
FROM stats s JOIN mad m ON m.device_key = s.device_key"""

# floor-share mode: every AP reading (valid and floor), one AP at a time
ALL_READINGS_SQL = """
SELECT o.device_key, rssi_valid(o.rssi) AS rssi, rssi_is_floor(o.rssi, o.rssi_floor) AS is_floor,
       (o.ts AT TIME ZONE %(tz)s)::date AS obs_day
FROM observations o
JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
WHERE o.sensor_id = %(sid)s AND d.type = 'ap' AND (o.rssi IS NOT NULL OR o.rssi_floor)
  AND o.ts >= %(from_)s AND o.ts < %(to)s
ORDER BY o.device_key"""

AP_KIND_SQL = """
SELECT device_key, trunc(mac)::text AS oui,
       (mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00' AS random_bssid
FROM devices WHERE sensor_id = %(sid)s AND type = 'ap'"""


# --- pure helpers (unit-tested) -------------------------------------------------------

def quantile(sorted_vals, q):
    """Linear-interpolated quantile like percentile_cont; sorted_vals non-empty."""
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def censoring_bias(valid_sorted, f):
    """How far dropping a share f of floor (censored, lowest) readings moved the
    median of the rest: Q_valid(0.5) - Q_valid((0.5 - f) / (1 - f)). None when
    f >= 0.5 (the true median is itself censored) or no valid readings."""
    if not valid_sorted or f >= 0.5:
        return None
    return quantile(valid_sorted, 0.5) - quantile(valid_sorted, (0.5 - f) / (1.0 - f))


def admissible_floor_share(rows):
    """rows: (f, bias, robust_sd) of qualifying APs. Returns (lo, hi): lo = the
    largest f whose bias stays within max(robust_sd, 1 dB), hi = the smallest f
    above lo that violates it (bias beyond tolerance, or f >= 0.5)."""
    rows = sorted(rows)
    lo, hi = 0.0, 0.5
    for f, bias, rsd in rows:
        ok = bias is not None and bias <= max(rsd or 0.0, 1.0)
        if not ok:
            hi = min(hi, f)
            break
        lo = f
    return lo, hi


def load_degraded(path, sensor_name):
    out = []
    with open(path) as fh:
        for row in csv.DictReader(line for line in fh if not line.startswith("#")):
            if row["sensor"] != sensor_name:
                continue
            p = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)  # noqa: E731
            out.append((p(row["start_utc"]), p(row["end_utc"]), row["kind"], row["label"]))
    return out


def classify(ep, degraded, changes, kinds, near_s=3600):
    """Cause class of one emitted episode."""
    for s, e, kind, label in degraded:
        if ep.first_ts <= e and ep.last_ts >= s:
            return "degraded: " + label
    near = timedelta(seconds=near_s)
    for t in changes.get(ep.device_key, ()):
        if ep.first_ts - near <= t <= ep.last_ts + near:
            return "channel change +-1 h"
    return "open"


def ap_kind(k):
    if k is None:
        return "?"
    if k["random_bssid"]:
        return "randomised"
    return "campus Ruckus" if k["oui"].upper().startswith("EC:58:EA") else "other"


# --- audit ---------------------------------------------------------------------------

def _evaluate(devs, meta, params, frm, to):
    eps = [e for e in et.build_episodes(devs, params.gap_s) if e.overlaps(frm, to)]
    out = []
    for e in eps:
        m = meta[e.device_key]
        et.assess(e, params, m, m["bssid"], m["ssid"], m["trusted"])
        if e.emitted:
            out.append(e)
    return out


def run_audit(conn, sensor, frm, to, params, ks, split_at):
    sid = sensor["id"]
    gap = timedelta(seconds=params.gap_s)
    t0, tend = conn.execute("SELECT min(ts) AS a, max(ts) AS b FROM observations WHERE sensor_id = %s",
                            (sid,)).fetchone().values()
    tmid = split_at or (t0 + (tend - t0) / 2)
    baselined = {r["device_key"]: r for r in conn.execute(et.AP_META_SQL, {"sid": sid})}
    meta, excluded = et.eligible_aps(baselined, params)
    split = {r["device_key"]: r for r in conn.execute(SPLIT_SQL, {
        "sid": sid, "tz": sensor["tz"], "p_from": t0, "p_until": tmid, "min_obs": 200, "min_days": 2})
        if r["rssi_robust_sd"] is not None}
    split_meta = {k: dict(baselined[k], rssi_median=v["rssi_median"], rssi_robust_sd=v["rssi_robust_sd"],
                          base_n_obs=v["n_obs"], base_n_floor=v["n_floor"], base_n_days=v["n_days"])
                  for k, v in split.items() if k in baselined}
    split_meta, split_excluded = et.eligible_aps(split_meta, params)
    changes = defaultdict(list)
    guard = timedelta(seconds=max(params.channel_guard_s, 3600))
    for r in conn.execute(et.CHANNEL_CHANGES_SQL, {"sid": sid, "from_": frm - gap - guard, "to": to + gap + guard}):
        changes[r["device_key"]].append(r["ts"])
    kinds = {r["device_key"]: r for r in conn.execute(AP_KIND_SQL, {"sid": sid})}
    degraded = load_degraded(DEGRADED_CSV, sensor["name"])

    pk = {k: replace(params, k=k) for k in ks}
    circ, circ2, spl = [], defaultdict(list), defaultdict(list)
    keys = set(meta) | set(split_meta)
    rows = et._stream_readings(conn, et.READINGS_SQL, {"sid": sid, "scan_from": frm - gap, "scan_to": to + gap})
    for key, group in itertools.groupby(rows, key=lambda r: r[0]):
        if key not in keys:
            continue
        readings = [(r[1], r[2]) for r in group]       # one AP (bounded by one AP's window)
        second = [r for r in readings if r[0] >= tmid - gap]
        if key in meta:
            m = meta[key]
            circ += et.windowed_deviations(readings, float(m["rssi_median"]), float(m["rssi_robust_sd"]),
                                           params, key)
            for k, p in pk.items():
                circ2[k] += et.windowed_deviations(second, float(m["rssi_median"]),
                                                   float(m["rssi_robust_sd"]), p, key)
        if key in split_meta:
            s = split_meta[key]
            for k, p in pk.items():
                spl[k] += et.windowed_deviations(second, float(s["rssi_median"]), float(s["rssi_robust_sd"]),
                                                 p, key)

    def summarise(label, k, devs, mt, lo, hi, n_aps):
        devs, guarded = et.apply_channel_guard(devs, changes, params.channel_guard_s)
        p = pk.get(k, params)
        found = _evaluate(devs, mt, p, lo, hi)
        c = Counter()
        causes = Counter()
        for e in found:
            c["sev_" + e.severity] += 1
            c["facet_" + e.facet] += 1
            c["dir_" + e.direction] += 1
            causes[classify(e, degraded, changes, kinds)] += 1
            c["kind_" + ap_kind(kinds.get(e.device_key))] += 1
        return {"variant": label, "k": k, "window": [lo, hi], "aps_evaluated": n_aps,
                "detections": len(found), "aps_with_detections": len({e.device_key for e in found}),
                "windows_channel_guarded": guarded, "counts": dict(c), "causes": dict(causes)}

    results = [summarise("circular", params.k, circ, meta, frm, to, len(meta))]
    for k in ks:
        results.append(summarise("circular2", k, circ2[k], meta, tmid, to, len(meta)))
        results.append(summarise("split", k, spl[k], split_meta, tmid, to, len(split_meta)))
    info = {"split_at": tmid, "data_from": t0, "data_to": tend, "aps_baselined": len(baselined),
            "excluded_circular": dict(excluded), "aps_split_qualifying": len(split),
            "excluded_split": dict(split_excluded), "params": params.as_dict()}
    return info, results


def run_floor_share(conn, sensor, frm, to):
    sid = sensor["id"]
    kinds = {r["device_key"]: r for r in conn.execute(AP_KIND_SQL, {"sid": sid})}
    out = []
    with conn.cursor(name="fp_floor_share") as cur:
        cur.itersize = 20000
        cur.execute(ALL_READINGS_SQL, {"sid": sid, "tz": sensor["tz"], "from_": frm, "to": to})
        for key, grp in itertools.groupby(cur, key=lambda r: r["device_key"]):
            valid, n_floor, days = [], 0, set()
            for r in grp:
                if r["is_floor"]:
                    n_floor += 1
                elif r["rssi"] is not None:
                    valid.append(r["rssi"])
                    days.add(r["obs_day"])
            n = len(valid) + n_floor
            if not n:
                continue
            valid.sort()
            f = n_floor / float(n)
            rsd = None
            if valid:
                med = quantile(valid, 0.5)
                rsd = 1.4826 * quantile(sorted(abs(v - med) for v in valid), 0.5)
            out.append({"device_key": key, "kind": ap_kind(kinds.get(key)), "n_valid": len(valid),
                        "n_floor": n_floor, "f": f, "robust_sd": rsd, "bias_db": censoring_bias(valid, f),
                        "qualifies": len(valid) >= 200 and len(days) >= 2})
    q = [r for r in out if r["qualifies"]]
    lo, hi = admissible_floor_share([(r["f"], r["bias_db"], r["robust_sd"]) for r in q])
    return {"aps": len(out), "qualifying": len(q), "admissible": [lo, hi],
            "removed_at": {str(t): dict(Counter(r["kind"] for r in q if r["f"] > t))
                           for t in (0.1, 0.2, 0.35, lo, hi)},
            "floor_share_quantiles": [round(quantile(sorted(r["f"] for r in q), p), 4)
                                      for p in (0.5, 0.9, 0.99)] if q else []}, out


# --- CLI -----------------------------------------------------------------------------

def add_arguments(p):
    et.add_arguments(p)
    p.add_argument("--mode", choices=("audit", "floor-share"), default="audit")
    p.add_argument("--ks", default="3,4,6", help="k values for the split audit (default 3,4,6)")
    p.add_argument("--split-at", metavar="WHEN", help="split point (default: middle of the data)")


def run(args):
    params = et.Params.from_args(args)
    try:
        frm, to = common.window_from_args(args.from_, args.to)
    except ValueError as e:
        sys.exit(str(e))
    split_at = common.parse_when(args.split_at) if args.split_at else None
    conn = common.connect("wifi-audit-fp-audit")
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            sensor = common.get_sensor(conn, args.sensor)
            if args.mode == "floor-share":
                summary, rows = run_floor_share(conn, sensor, frm, to)
                print(json.dumps({"mode": "floor-share", "sensor": sensor["name"], "window": [frm, to],
                                  "summary": summary}, default=str, indent=1))
                if args.verbose:
                    for r in sorted(rows, key=lambda r: r["f"]):
                        if r["qualifies"] and r["f"] > 0.01:
                            print(json.dumps(r, default=str))
                return 0
            ks = tuple(float(k) for k in args.ks.split(","))
            info, results = run_audit(conn, sensor, frm, to, params, ks, split_at)
    finally:
        conn.close()
    print("fp-audit  sensor %s  window %s .. %s  split at %s" % (
        sensor["name"], fmt_ts(frm), fmt_ts(to), fmt_ts(info["split_at"])))
    print("  baselined APs %d, excluded (circular) %s; split-qualifying %d, excluded (split) %s" % (
        info["aps_baselined"], info["excluded_circular"] or "none", info["aps_split_qualifying"],
        info["excluded_split"] or "none"))
    for r in results:
        print("  %-9s k=%-4g %4d detections on %3d APs  guarded %5d  sev %s  causes %s" % (
            r["variant"], r["k"], r["detections"], r["aps_with_detections"], r["windows_channel_guarded"],
            {k[4:]: v for k, v in r["counts"].items() if k.startswith("sev_")}, r["causes"]))
    if args.json:
        print(json.dumps({"info": info, "results": results}, default=str, indent=1))
    return 0
