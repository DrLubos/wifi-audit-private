#!/usr/bin/env python3
"""Read-only summary of the collector's SQLite buffer.

Prints what the sensor has accumulated so far so it can be eyeballed:
totals, poll health and gaps, per-AP RSSI stability, AP configuration
changes, Kismet alerts, probed SSIDs, and database growth.

No detection, no writes. The database is opened with the SQLite read-only
URI flag and query_only, so the collector can keep running while this runs.

Usage:  python3 analyze.py [DB_PATH] [--top N] [--hist N] [--probes N]
                           [--alerts N] [--utc]

DB_PATH defaults to DB_PATH from the environment, then from
/etc/wifi-sensor/collector.conf (or COLLECTOR_CONF), then
/var/lib/wifi-sensor/buffer.db - the same resolution the collector uses.
Only the standard library is needed.
"""

import argparse
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"
CONFIG_COLS = ("ssid", "cloaked", "crypt", "crypt_bits", "mfp_sup", "mfp_req",
               "adv_channel", "ht_mode", "beacon_rate", "country")

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


def resolve_settings(db_arg):
    """DB path, poll interval and retention days, resolved like collector.py."""
    cfg = {"DB_PATH": DEFAULT_DB, "POLL_INTERVAL": "30", "RETENTION_DAYS": "14"}
    conf = os.environ.get("COLLECTOR_CONF", DEFAULT_CONF)
    if os.path.isfile(conf):
        try:
            cfg.update({k: v for k, v in read_kv_file(conf).items() if k in cfg})
        except OSError:
            pass
    cfg.update({k: v for k, v in os.environ.items() if k in cfg})
    if db_arg:
        cfg["DB_PATH"] = db_arg
    cfg["DB_PATH"] = os.path.expanduser(cfg["DB_PATH"])
    cfg["POLL_INTERVAL"] = int(cfg["POLL_INTERVAL"])
    cfg["RETENTION_DAYS"] = int(cfg["RETENTION_DAYS"])
    return cfg


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


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd %02dh %02dm" % (d, h, m)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm %02ds" % (m, s)


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024


def date_expr(col):
    return "date(%s, 'unixepoch'%s)" % (col, "" if USE_UTC else ", 'localtime'")


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


def one(db, sql, *args):
    row = db.execute(sql, args).fetchone()
    return row[0] if row else None


def label(dev):
    """Short human label for a devices row (or None)."""
    if dev is None:
        return "?"
    ssid = dev["ssid"]
    if ssid:
        return "%s (%s)" % (ssid, dev["mac"])
    return "%s %s" % (dev["mac"], dev["manuf"] or "")


# --- sections -------------------------------------------------------------------

def show_totals(db, cfg):
    section("TOTALS")
    n_polls = one(db, "SELECT COUNT(*) FROM polls")
    n_ok = one(db, "SELECT COUNT(*) FROM polls WHERE ok = 1")
    first, last = db.execute("SELECT MIN(ts), MAX(ts) FROM polls").fetchone()
    span = (last - first) if first is not None and last is not None else 0

    print("  database          %s" % cfg["DB_PATH"])
    for k, v in db.execute("SELECT key, value FROM meta ORDER BY key"):
        if k == "last_kismet_ts":
            v = "%s (%s)" % (v, fmt_ts(int(v)))
        print("  meta.%-13s %s" % (k, v))
    print("  polls             %d total, %d ok, %d failed" % (n_polls, n_ok, n_polls - n_ok))
    print("  first poll        %s" % fmt_ts(first))
    print("  last poll         %s" % fmt_ts(last))
    print("  span              %s (%.2f days)" % (fmt_dur(span), span / 86400.0))
    if n_ok > 1 and span:
        expected = span / cfg["POLL_INTERVAL"] + 1
        print("  coverage          %d ok polls of ~%d expected at %ds -> %.1f%%"
              % (n_ok, expected, cfg["POLL_INTERVAL"], 100.0 * n_ok / expected))

    # Gaps between consecutive polls (outages, restarts, Kismet down).
    gap_limit = 3 * cfg["POLL_INTERVAL"]
    prev = None
    gaps = []
    for (ts,) in db.execute("SELECT ts FROM polls ORDER BY ts"):
        if prev is not None and ts - prev > gap_limit:
            gaps.append((prev, ts))
        prev = ts
    if gaps:
        total = sum(b - a for a, b in gaps)
        print("  gaps > %ds        %d, total %s, largest %s" %
              (gap_limit, len(gaps), fmt_dur(total), fmt_dur(max(b - a for a, b in gaps))))
        for a, b in sorted(gaps, key=lambda g: g[0] - g[1])[:5]:
            print("      %s -> %s  (%s)" % (fmt_ts(a), fmt_ts(b), fmt_dur(b - a)))
    else:
        print("  gaps > %ds        none" % gap_limit)

    print()
    print("  devices by type (devices table, all ever seen):")
    rows = db.execute("SELECT type, COUNT(*) AS n, "
                      "SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS recent "
                      "FROM devices GROUP BY type ORDER BY n DESC",
                      ((last or 0) - 86400,)).fetchall()
    table(["type", "count", "seen in last 24h"],
          [(r["type"], r["n"], r["recent"]) for r in rows], align_right={1, 2})
    n_dev = one(db, "SELECT COUNT(*) FROM devices")
    n_obs_dev = one(db, "SELECT COUNT(DISTINCT key) FROM observations")
    print("  distinct devices  %d in devices, %d with observations still retained"
          % (n_dev, n_obs_dev))

    print()
    print("  rows per table:")
    rows = []
    for t in ("polls", "devices", "device_config_history", "observations",
              "device_freq_hist", "associations", "probes", "alerts"):
        rows.append((t, one(db, "SELECT COUNT(*) FROM %s" % t)))
    table(["table", "rows"], rows, align_right={1})


def show_poll_health(db):
    section("POLL HEALTH")
    r = db.execute(
        "SELECT AVG(duration_ms), MAX(duration_ms), AVG(devices_total), MAX(devices_total), "
        "AVG(devices_active), MAX(devices_active), AVG(new_obs), MAX(new_obs), "
        "SUM(CASE WHEN ds_running = 0 THEN 1 ELSE 0 END), MAX(ds_packets) "
        "FROM polls WHERE ok = 1").fetchone()
    if r[0] is None:
        print("  no successful polls")
        return
    print("  duration          avg %.0f ms, max %d ms" % (r[0], r[1]))
    print("  devices_total     avg %.0f, max %d   (Kismet's whole device table)" % (r[2], r[3]))
    print("  devices_active    avg %.1f, max %d   (returned by the last-time query)" % (r[4], r[5]))
    print("  new_obs           avg %.1f, max %d   (observation rows written per poll)" % (r[6], r[7]))
    print("  ds not running    %d polls" % r[8])
    print("  ds_packets        %s (datasource packet counter at the last poll)" % (r[9],))
    errs = db.execute(
        "SELECT COALESCE(error, ds_error) AS e, COUNT(*), MIN(ts), MAX(ts) FROM polls "
        "WHERE ok = 0 OR ds_error IS NOT NULL GROUP BY e ORDER BY 2 DESC LIMIT 10").fetchall()
    if errs:
        print("  errors:")
        table(["error", "n", "first", "last"],
              [(e[0][:70] if e[0] else "-", e[1], fmt_ts(e[2]), fmt_ts(e[3])) for e in errs],
              align_right={1})


def show_rssi(db, top, hist):
    section("PER-AP RSSI (top %d APs by observation count)" % top)
    aps = db.execute(
        "SELECT d.key, d.mac, d.ssid, d.manuf, d.adv_channel, d.crypt, "
        "COUNT(o.rssi) AS n, MIN(o.ts) AS first, MAX(o.ts) AS last "
        "FROM devices d JOIN observations o ON o.key = d.key "
        "WHERE d.type = 'ap' AND o.rssi IS NOT NULL "
        "GROUP BY d.key ORDER BY n DESC LIMIT ?", (top,)).fetchall()
    if not aps:
        print("  no AP observations with RSSI")
        return

    rows = []
    detail = []
    for a in aps:
        vals = [r[0] for r in db.execute(
            "SELECT rssi FROM observations WHERE key = ? AND rssi IS NOT NULL ORDER BY ts",
            (a["key"],))]
        polls_in_window = one(db, "SELECT COUNT(*) FROM polls WHERE ok = 1 AND ts BETWEEN ? AND ?",
                              a["first"], a["last"]) or 1
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        srt = sorted(vals)
        p10 = srt[int(0.10 * (len(srt) - 1))]
        p90 = srt[int(0.90 * (len(srt) - 1))]
        rows.append((
            (a["ssid"] or "<hidden>")[:24], a["mac"], a["adv_channel"], (a["crypt"] or "")[:18],
            len(vals), "%.0f%%" % (100.0 * len(vals) / polls_in_window),
            min(vals), max(vals), "%.1f" % mean, "%.1f" % sd, "%d..%d" % (p10, p90),
            fmt_dur(a["last"] - a["first"])))
        detail.append((a, vals, mean, sd))
    table(["ssid", "bssid", "ch", "crypt", "n_obs", "seen%", "min", "max", "avg", "sd",
           "p10..p90", "window"], rows, align_right={4, 5, 6, 7, 8, 9})
    print("  seen% = observations / ok polls between the AP's first and last observation.")
    print("  sd = population std dev of RSSI (dBm); p10..p90 = middle 80% of readings.")

    for a, vals, mean, sd in detail[:hist]:
        print()
        print("  %s  RSSI histogram (5 dBm bins) and per-day summary"
              % label(a))
        bins = Counter((v // 5) * 5 for v in vals)
        peak = max(bins.values())
        for b in sorted(bins):
            n = bins[b]
            bar = "#" * max(1, int(40.0 * n / peak))
            print("    %4d..%4d dBm  %6d  %s" % (b, b + 4, n, bar))
        days = db.execute(
            "SELECT %s AS d, COUNT(rssi), MIN(rssi), MAX(rssi), AVG(rssi), "
            "AVG(rssi * rssi) - AVG(rssi) * AVG(rssi) AS var "
            "FROM observations WHERE key = ? AND rssi IS NOT NULL GROUP BY d ORDER BY d"
            % date_expr("ts"), (a["key"],)).fetchall()
        table(["day", "n", "min", "max", "avg", "sd"],
              [(r[0], r[1], r[2], r[3], "%.1f" % r[4], "%.1f" % (max(r[5], 0.0) ** 0.5))
               for r in days], align_right={1, 2, 3, 4, 5})


def show_config_changes(db):
    section("AP CONFIGURATION CHANGES (device_config_history)")
    hist = db.execute(
        "SELECT * FROM device_config_history ORDER BY key, ts, id").fetchall()
    n = len(hist)
    print("  %d history rows over %d devices" % (n, len({h["key"] for h in hist})))
    if not hist:
        return

    # A history row holds the configuration that was *replaced* at ts. The
    # replacement is the next history row of the same device, or the current
    # devices row for the last change.
    by_key = defaultdict(list)
    for h in hist:
        by_key[h["key"]].append(h)
    events = []      # (ts, key, field, old, new)
    field_count = Counter()
    for key, rows in by_key.items():
        cur = db.execute("SELECT * FROM devices WHERE key = ?", (key,)).fetchone()
        for i, old in enumerate(rows):
            new = rows[i + 1] if i + 1 < len(rows) else cur
            if new is None:
                continue
            changed = [c for c in CONFIG_COLS if old[c] != new[c]]
            if not changed:
                events.append((old["ts"], key, "(no column differs)", "", ""))
                continue
            for c in changed:
                events.append((old["ts"], key, c, old[c], new[c]))
                field_count[c] += 1

    print()
    print("  changes by field:")
    table(["field", "changes"], sorted(field_count.items(), key=lambda x: -x[1]),
          align_right={1})

    print()
    print("  devices with most changes:")
    per_dev = Counter(k for k in by_key for _ in by_key[k])
    rows = []
    for key, cnt in per_dev.most_common(10):
        d = db.execute("SELECT * FROM devices WHERE key = ?", (key,)).fetchone()
        rows.append((label(d)[:40], cnt, fmt_ts(min(h["ts"] for h in by_key[key])),
                     fmt_ts(max(h["ts"] for h in by_key[key]))))
    table(["device", "changes", "first", "last"], rows, align_right={1})

    print()
    print("  all changes (chronological):")
    devs = {}
    rows = []
    for ts, key, field, old, new in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if key not in devs:
            devs[key] = db.execute("SELECT * FROM devices WHERE key = ?", (key,)).fetchone()
        rows.append((fmt_ts(ts), label(devs[key])[:36], field,
                     str(old)[:28], str(new)[:28]))
    table(["when", "device", "field", "from", "to"], rows)


def show_alerts(db, limit):
    section("KISMET ALERTS")
    n = one(db, "SELECT COUNT(*) FROM alerts")
    print("  %d alerts stored" % n)
    if not n:
        return
    print()
    print("  by type:")
    rows = db.execute(
        "SELECT header, class, severity, COUNT(*), MIN(ts), MAX(ts) FROM alerts "
        "GROUP BY header ORDER BY 4 DESC").fetchall()
    table(["header", "class", "sev", "n", "first", "last"],
          [(r[0], r[1], r[2], r[3], fmt_ts(r[4]), fmt_ts(r[5])) for r in rows],
          align_right={2, 3})

    print()
    print("  by day:")
    rows = db.execute("SELECT %s AS d, COUNT(*) FROM alerts GROUP BY d ORDER BY d"
                      % date_expr("ts")).fetchall()
    table(["day", "alerts"], rows, align_right={1})

    print()
    print("  last %d alerts:" % limit)
    rows = []
    for a in db.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)):
        dev = None
        if a["device_key"]:
            dev = db.execute("SELECT * FROM devices WHERE key = ?", (a["device_key"],)).fetchone()
        if dev is None and a["source_mac"]:
            dev = db.execute("SELECT * FROM devices WHERE mac = ? ORDER BY last_seen DESC LIMIT 1",
                             (a["source_mac"],)).fetchone()
        who = label(dev) if dev else (a["source_mac"] or a["transmitter_mac"] or "-")
        rows.append((fmt_ts(a["ts"]), a["header"], a["channel"], who[:44],
                     a["dest_mac"] or "-", (a["text"] or "")[:60]))
    table(["when", "header", "ch", "device (src)", "dest", "text"], rows)


def show_probes(db, limit):
    section("PROBE REQUESTS (probes table: client x SSID)")
    n_rows = one(db, "SELECT COUNT(*) FROM probes")
    n_clients = one(db, "SELECT COUNT(DISTINCT key) FROM probes")
    n_wild = one(db, "SELECT COUNT(*) FROM probes WHERE ssid = ''")
    n_named = one(db, "SELECT COUNT(DISTINCT ssid) FROM probes WHERE ssid <> ''")
    only_wild = one(db, "SELECT COUNT(*) FROM (SELECT key FROM probes GROUP BY key "
                        "HAVING SUM(ssid <> '') = 0)")
    print("  %d client x SSID rows from %d probing clients" % (n_rows, n_clients))
    print("  %d clients sent wildcard probes; %d clients probed ONLY wildcard" % (n_wild, only_wild))
    print("  %d distinct named SSIDs probed for" % n_named)
    if not n_named:
        return
    print()
    print("  top %d named SSIDs by number of distinct clients:" % limit)
    rows = db.execute(
        "SELECT ssid, COUNT(*) AS clients, MIN(first_time), MAX(last_time), "
        "(SELECT COUNT(*) FROM devices d WHERE d.type = 'ap' AND d.ssid = p.ssid) AS local_ap "
        "FROM probes p WHERE ssid <> '' GROUP BY ssid ORDER BY clients DESC, ssid LIMIT ?",
        (limit,)).fetchall()
    table(["ssid", "clients", "first", "last", "AP seen here?"],
          [(r[0][:32], r[1], fmt_ts(r[2]), fmt_ts(r[3]), "yes" if r[4] else "no") for r in rows],
          align_right={1})
    print("  'AP seen here?' = an AP advertising that SSID exists in the devices table.")

    print()
    print("  clients probing for the most distinct SSIDs:")
    rows = db.execute(
        "SELECT p.key, d.mac, d.manuf, COUNT(*) AS n, MAX(p.last_time) "
        "FROM probes p LEFT JOIN devices d ON d.key = p.key WHERE p.ssid <> '' "
        "GROUP BY p.key ORDER BY n DESC LIMIT 10").fetchall()
    table(["client mac", "manuf", "ssids", "last probe"],
          [(r[1] or r[0], (r[2] or "")[:20], r[3], fmt_ts(r[4])) for r in rows], align_right={2})

    print()
    print("  probing activity by day (first_time of each client x SSID row):")
    rows = db.execute("SELECT %s AS d, COUNT(*), COUNT(DISTINCT key) FROM probes "
                      "GROUP BY d ORDER BY d" % date_expr("first_time")).fetchall()
    table(["day", "new rows", "clients"], rows, align_right={1, 2})


def show_growth(db, cfg):
    section("DATA GROWTH")
    path = cfg["DB_PATH"]
    sizes = []
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            s = os.path.getsize(p)
            sizes.append((os.path.basename(p), fmt_bytes(s)))
            if suffix != "-shm":
                total += s
    table(["file", "size"], sizes, align_right={1})
    page_size = one(db, "PRAGMA page_size")
    page_count = one(db, "PRAGMA page_count")
    freelist = one(db, "PRAGMA freelist_count")
    used = page_size * (page_count - freelist)
    print("  pages             %d x %d B = %s, %d free (%s reclaimable by VACUUM)"
          % (page_count, page_size, fmt_bytes(page_size * page_count), freelist,
             fmt_bytes(page_size * freelist)))
    print("  retention         %d days (observations/polls pruned hourly)"
          % cfg["RETENTION_DAYS"])

    try:
        rows = db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name "
                          "ORDER BY 2 DESC").fetchall()
        print()
        print("  space per table/index (dbstat):")
        table(["object", "bytes", "share"],
              [(r[0], fmt_bytes(r[1]), "%.1f%%" % (100.0 * r[1] / used)) for r in rows],
              align_right={1, 2})
    except sqlite3.Error:
        print("  (dbstat virtual table not available in this SQLite build)")

    print()
    print("  rows per day:")
    days = defaultdict(dict)
    per_table = (("observations", "ts"), ("polls", "ts"), ("alerts", "ts"),
                 ("associations", "first_seen"), ("probes", "first_time"),
                 ("devices", "first_seen"))
    for t, col in per_table:
        for d, n in db.execute("SELECT %s AS d, COUNT(*) FROM %s GROUP BY d ORDER BY d"
                               % (date_expr(col), t)):
            days[d][t] = n
    hdr = ["day"] + [t for t, _ in per_table]
    table(hdr, [[d] + [days[d].get(t, 0) for t, _ in per_table] for d in sorted(days)],
          align_right=set(range(1, len(hdr))))
    print("  'associations', 'probes' and 'devices' are first-seen dates (never pruned);")
    print("  the first day includes everything Kismet already knew when the collector started.")

    # Growth estimate. Only the retained window is on disk, so bytes/day is
    # derived from the rows currently held and the days they cover.
    first, last = db.execute("SELECT MIN(ts), MAX(ts) FROM observations").fetchone()
    if first and last and last > first:
        obs_days = (last - first) / 86400.0
        n_obs = one(db, "SELECT COUNT(*) FROM observations")
        full_days = [d for d in days if days[d].get("polls", 0) >= 86400 / cfg["POLL_INTERVAL"] * 0.9]
        print()
        print("  observations span %.2f days -> %.0f observation rows/day"
              % (obs_days, n_obs / obs_days))
        if full_days:
            typical = statistics.median(days[d].get("observations", 0) for d in full_days)
            print("  median observations on a full day (%d full days): %.0f" % (len(full_days), typical))
        print("  on-disk bytes per observation row (whole DB / observation rows): %.0f B"
              % (used / max(n_obs, 1)))
        print("  growth estimate   %s/day  (used bytes / observation span)"
              % fmt_bytes(used / obs_days))
        print("  steady state      ~%s at %d days retention, if the current rate holds"
              % (fmt_bytes(used / obs_days * cfg["RETENTION_DAYS"]), cfg["RETENTION_DAYS"]))
        if obs_days > cfg["RETENTION_DAYS"] + 1:
            print("  (span exceeds retention - pruning may not be running; check the collector log)")


# --- main ---------------------------------------------------------------------------

def main():
    global USE_UTC
    ap = argparse.ArgumentParser(description="Read-only summary of the collector buffer.")
    ap.add_argument("db", nargs="?", help="path to buffer.db (default: collector config)")
    ap.add_argument("--top", type=int, default=8, help="APs in the RSSI table (default 8)")
    ap.add_argument("--hist", type=int, default=3, help="APs with RSSI histogram/day table (default 3)")
    ap.add_argument("--probes", type=int, default=30, help="probed SSIDs to list (default 30)")
    ap.add_argument("--alerts", type=int, default=40, help="most recent alerts to list (default 40)")
    ap.add_argument("--utc", action="store_true", help="print times in UTC instead of local time")
    args = ap.parse_args()
    USE_UTC = args.utc

    cfg = resolve_settings(args.db)
    db = open_readonly(cfg["DB_PATH"])
    print("wifi-sensor buffer summary  -  generated %s  (times are %s)"
          % (fmt_ts(datetime.now().timestamp()), "UTC" if USE_UTC else "local"))
    try:
        show_totals(db, cfg)
        show_poll_health(db)
        show_rssi(db, args.top, args.hist)
        show_config_changes(db)
        show_alerts(db, args.alerts)
        show_probes(db, args.probes)
        show_growth(db, cfg)
    finally:
        db.close()
    print()


if __name__ == "__main__":
    main()
