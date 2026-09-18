#!/usr/bin/env python3
"""Read-only characterisation of AP inventory changes in the collector buffer.

Purpose: find out whether "a network appeared where it should not" is a usable
signal for a fixed passive sensor, by measuring the noise floor first and the
targeted signal second.

  1. Baseline vs later: the first --baseline-hours of the capture form the
     baseline inventory; report APs that appeared only afterwards and baseline
     APs that vanished, each split by how long they persisted.
  2. Raw churn: distinct APs active per day, APs first seen per day, and how
     many of those are transient (< --transient-min minutes or
     < --transient-polls observations) vs intermediate vs sustained
     (>= --persistent-hours). Lifetime histogram, share of randomised
     (locally administered) BSSIDs. This is why raw "new AP" is unusable.
  3. Protected SSIDs (--protected PREFIX[,PREFIX...]): every BSSID advertising
     a protected SSID (now or at any time in the config history), with OUI,
     manufacturer, encryption, channel, first/last seen, typical RSSI, and
     flags: oui (differs from the infrastructure OUI, given with --infra-oui or
     inferred as the dominant OUI), late (appeared after the baseline window),
     strong (median RSSI more than --strong-db above the group's typical
     level), crypt (encryption differs from the group), rand (randomised
     BSSID), adopted (BSSID carried a different SSID before). Also look-alike
     SSIDs (normalised or one-edit match of a protected prefix).
  4. Persistence x strength: every post-baseline AP cross-tabulated by
     persistence class and by median RSSI (close: >= --close-dbm), and the
     candidate list (flagged protected BSSIDs, look-alikes, sustained close
     newcomers) with duration, seen%, RSSI and active days.

Nothing is detected live and nothing is written; the database is opened with
the SQLite read-only URI flag and query_only, so the collector keeps running.

Usage:  python3 inventory_changes.py [DB_PATH] --protected IK-WIFI,FRI_wifi
            [--infra-oui 00:11:22,AA:BB:CC] [--baseline-hours 24]
            [--absent-hours 24] [--transient-min 60] [--transient-polls 10]
            [--persistent-hours 24] [--close-dbm -60] [--strong-db 10]
            [--list 25] [--csv FILE] [--utc]

DB_PATH defaults to DB_PATH from the environment, then from
/etc/wifi-sensor/collector.conf (or COLLECTOR_CONF), then
/var/lib/wifi-sensor/buffer.db - the same resolution the collector uses.
Only the standard library is needed.

Data notes: first_seen/last_seen are Kismet's first_time/last_time kept as
MIN/MAX in `devices` (never pruned); RSSI and per-poll activity come from
`observations`, which are pruned after RETENTION_DAYS, so RSSI/seen%/active
days cover at most the retention window. Hidden-SSID APs cannot be matched to
a protected SSID (they are counted separately).
"""

import argparse
import csv
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"
LIFETIME_BINS = ((5 * 60, "< 5 min"), (3600, "5 min .. 1 h"), (6 * 3600, "1 .. 6 h"),
                 (86400, "6 .. 24 h"), (3 * 86400, "1 .. 3 d"), (7 * 86400, "3 .. 7 d"),
                 (None, ">= 7 d"))

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
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def day_of(ts):
    if USE_UTC:
        return datetime.fromtimestamp(ts, timezone.utc).date()
    return datetime.fromtimestamp(ts).date()


def date_expr(col):
    return "date(%s, 'unixepoch'%s)" % (col, "" if USE_UTC else ", 'localtime'")


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "%dd %02dh" % (d, h)
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


def pc(n, total):
    return "%d (%.0f%%)" % (n, 100.0 * n / total) if total else "0"


def oui_of(mac):
    return mac.upper()[:8]


def is_random_mac(mac):
    """Locally administered bit set -> randomised BSSID (phone hotspot etc.)."""
    try:
        return bool(int(mac[0:2], 16) & 0x02)
    except ValueError:
        return False


def norm_ssid(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def lifetime_bin(span):
    for limit, name in LIFETIME_BINS:
        if limit is None or span < limit:
            return name
    return LIFETIME_BINS[-1][1]


# --- data loading -------------------------------------------------------------------

def load_aps(db, args, capture):
    """One dict per AP with aggregate observation stats (single query)."""
    rows = db.execute(
        "SELECT d.key, d.mac, d.manuf, d.ssid, d.cloaked, d.crypt, d.adv_channel, "
        "d.first_seen, d.last_seen, "
        "COUNT(o.id) AS n_obs, COUNT(o.rssi) AS n_rssi, MIN(o.ts) AS obs_first, "
        "MAX(o.ts) AS obs_last, AVG(o.rssi) AS rssi_avg, MAX(o.rssi) AS rssi_max, "
        "COUNT(DISTINCT %s) AS days "
        "FROM devices d LEFT JOIN observations o ON o.key = d.key "
        "WHERE d.type = 'ap' GROUP BY d.key" % date_expr("o.ts")).fetchall()
    aps = {}
    for r in rows:
        a = dict(r)
        a["span"] = max(0, a["last_seen"] - a["first_seen"])
        a["random_mac"] = is_random_mac(a["mac"])
        a["oui"] = oui_of(a["mac"])
        a["late"] = a["first_seen"] > capture["baseline_end"]
        a["absent"] = a["last_seen"] < capture["end"] - args.absent_hours * 3600
        if a["span"] < args.transient_min * 60 or a["n_obs"] < args.transient_polls:
            a["cls"] = "transient"
        elif a["span"] >= args.persistent_hours * 3600:
            a["cls"] = "sustained"
        else:
            a["cls"] = "intermediate"
        a["rssi_med"] = None
        a["seen_pct"] = None
        a["flags"] = []
        a["protected"] = None
        aps[a["key"]] = a
    return aps


def enrich(db, a):
    """Median RSSI and seen% for one AP (only done for the APs that get listed)."""
    if a["rssi_med"] is not None or a["seen_pct"] is not None:
        return
    vals = [r[0] for r in db.execute(
        "SELECT rssi FROM observations WHERE key = ? AND rssi IS NOT NULL", (a["key"],))]
    a["rssi_med"] = statistics.median(vals) if vals else None
    if a["obs_first"] is not None:
        polls = db.execute("SELECT COUNT(*) FROM polls WHERE ok = 1 AND ts BETWEEN ? AND ?",
                           (a["obs_first"], a["obs_last"])).fetchone()[0] or 1
        a["seen_pct"] = 100.0 * a["n_obs"] / polls


def strength(a, args):
    r = a["rssi_med"] if a["rssi_med"] is not None else a["rssi_avg"]
    if r is None:
        return "no-rssi"
    return "close" if r >= args.close_dbm else "far"


def ap_row(a, args):
    r = a["rssi_med"]
    return ((a["ssid"] or ("<hidden>" if a["cloaked"] else "<none>"))[:22], a["mac"],
            (a["manuf"] or "")[:16], a["adv_channel"], (a["crypt"] or "")[:14],
            fmt_ts(a["first_seen"]), fmt_ts(a["last_seen"]), fmt_dur(a["span"]), a["cls"],
            a["n_obs"], "%.0f%%" % a["seen_pct"] if a["seen_pct"] is not None else "-",
            a["days"], "%.0f" % r if r is not None else "-",
            a["rssi_max"], strength(a, args), "R" if a["random_mac"] else "",
            ",".join(a["flags"]))


AP_HEAD = ["ssid", "bssid", "manuf", "ch", "crypt", "first_seen", "last_seen", "span",
           "class", "n_obs", "seen%", "days", "rssi_med", "max", "dist", "rand", "flags"]
AP_RIGHT = {9, 10, 11, 12, 13}


def list_aps(db, aps, args, sort_key, reverse=True):
    sel = sorted(aps, key=sort_key, reverse=reverse)[:args.list]
    for a in sel:
        enrich(db, a)
    table(AP_HEAD, [ap_row(a, args) for a in sel], AP_RIGHT)
    if len(aps) > len(sel):
        print("  ... %d more not listed (--list N)" % (len(aps) - len(sel)))


def cls_counts(aps):
    c = Counter(a["cls"] for a in aps)
    n = len(aps)
    return "%d  [transient %s, intermediate %s, sustained %s]" % (
        n, pc(c["transient"], n), pc(c["intermediate"], n), pc(c["sustained"], n))


# --- sections -----------------------------------------------------------------------

def show_dataset(db, aps, capture, args):
    section("DATASET")
    polls = db.execute("SELECT COUNT(*), SUM(ok) FROM polls").fetchone()
    obs = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations").fetchone()
    print("  capture: %s .. %s (%s), polls %d (%d ok)" % (
        fmt_ts(capture["start"]), fmt_ts(capture["end"]),
        fmt_dur(capture["end"] - capture["start"]), polls[0] or 0, polls[1] or 0))
    print("  observations retained: %d, %s .. %s" % (obs[0] or 0, fmt_ts(obs[1]), fmt_ts(obs[2])))
    print("  baseline window: first %d h, until %s; 'absent' = not seen in the last %d h" % (
        args.baseline_hours, fmt_ts(capture["baseline_end"]), args.absent_hours))
    print("  persistence classes: transient < %d min or < %d observations; sustained >= %d h" % (
        args.transient_min, args.transient_polls, args.persistent_hours))
    print("  strength: close = median RSSI >= %d dBm" % args.close_dbm)
    print("  AP devices: %d (hidden SSID: %d, randomised BSSID: %d, no observation retained: %d)" % (
        len(aps), sum(1 for a in aps.values() if not a["ssid"]),
        sum(1 for a in aps.values() if a["random_mac"]),
        sum(1 for a in aps.values() if a["n_obs"] == 0)))
    print("  day boundaries: %s" % ("UTC" if USE_UTC else "local time"))


def show_baseline(db, aps, args):
    section("1. BASELINE INVENTORY VS LATER")
    base = [a for a in aps.values() if not a["late"]]
    later = [a for a in aps.values() if a["late"]]
    print("  APs first seen within the baseline window: %s" % cls_counts(base))
    print("  APs first seen after the baseline window:  %s" % cls_counts(later))
    vanished = [a for a in base if a["absent"]]
    present = [a for a in base if not a["absent"]]
    print()
    print("  baseline APs still present (seen in last %d h): %d" % (args.absent_hours, len(present)))
    print("  baseline APs vanished:                         %s" % cls_counts(vanished))
    print("  later APs already gone again:                  %s" % cls_counts(
        [a for a in later if a["absent"]]))
    print("  later APs still present:                       %s" % cls_counts(
        [a for a in later if not a["absent"]]))

    print()
    print("  sustained APs that appeared after the baseline (the only 'new AP' events worth a look):")
    list_aps(db, [a for a in later if a["cls"] == "sustained"], args,
             lambda a: (a["rssi_avg"] or -999, a["span"]))
    print()
    print("  sustained baseline APs that vanished (inventory loss / AP replaced?):")
    list_aps(db, [a for a in vanished if a["cls"] == "sustained"], args,
             lambda a: (a["span"], a["rssi_avg"] or -999))


def show_churn(db, aps, capture, args):
    section("2. RAW CHURN - THE NOISE FLOOR OF 'NEW AP APPEARED'")
    days_active = defaultdict(set)
    for key, d in db.execute(
            "SELECT o.key, %s AS d FROM observations o JOIN devices dv USING(key) "
            "WHERE dv.type = 'ap' GROUP BY o.key, d" % date_expr("o.ts")):
        days_active[d].add(key)
    first_day = defaultdict(list)
    last_day = defaultdict(list)
    for a in aps.values():
        first_day[str(day_of(a["first_seen"]))].append(a)
        last_day[str(day_of(a["last_seen"]))].append(a)
    all_days = sorted(set(days_active) | set(first_day) | set(last_day))
    last_capture_day = str(day_of(capture["end"]))
    rows = []
    post = []   # new APs on full days after the baseline window
    for d in all_days:
        new = first_day.get(d, [])
        c = Counter(a["cls"] for a in new)
        rnd = sum(1 for a in new if a["random_mac"])
        gone = [a for a in last_day.get(d, []) if d != last_capture_day]
        rows.append((d, len(days_active.get(d, ())), len(new), c["transient"],
                     c["intermediate"], c["sustained"], pc(rnd, len(new)) if new else "-",
                     len(gone), sum(1 for a in gone if a["cls"] == "sustained")))
        if new and d > str(day_of(capture["baseline_end"])) and d != last_capture_day:
            post.append(new)
    table(["day", "active", "new", "transient", "interm.", "sustained", "new rand", "last seen",
           "of which sust."], rows, align_right={1, 2, 3, 4, 5, 6, 7, 8})
    print("  active = distinct APs with an observation that day (retained observations only);")
    print("  new = APs whose Kismet first_time falls on that day (baseline day(s) included);")
    print("  last seen = APs whose last_time falls on that day (final capture day excluded).")

    if post:
        per_day = [len(n) for n in post]
        trans = [sum(1 for a in n if a["cls"] == "transient") for n in post]
        sust = [sum(1 for a in n if a["cls"] == "sustained") for n in post]
        print()
        print("  full days after the baseline window: %d" % len(post))
        print("    new APs per day: mean %.1f, median %.0f, max %d" % (
            statistics.fmean(per_day), statistics.median(per_day), max(per_day)))
        print("    of which transient: %.1f/day (%.0f%%); sustained: %.1f/day (%.0f%%)" % (
            statistics.fmean(trans), 100.0 * sum(trans) / sum(per_day),
            statistics.fmean(sust), 100.0 * sum(sust) / sum(per_day)))

    print()
    print("  lifetime (last_time - first_time) of every AP:")
    total = len(aps)
    bins = Counter(lifetime_bin(a["span"]) for a in aps.values())
    rnd_bins = Counter(lifetime_bin(a["span"]) for a in aps.values() if a["random_mac"])
    hid_bins = Counter(lifetime_bin(a["span"]) for a in aps.values() if not a["ssid"])
    rows = []
    for _, name in LIFETIME_BINS:
        n = bins.get(name, 0)
        bar = "#" * int(40.0 * n / max(bins.values())) if n else ""
        rows.append((name, n, "%.0f%%" % (100.0 * n / total) if total else "-",
                     pc(rnd_bins.get(name, 0), n) if n else "-",
                     pc(hid_bins.get(name, 0), n) if n else "-", bar))
    table(["lifetime", "APs", "share", "randomised BSSID", "hidden SSID", ""], rows,
          align_right={1, 2, 3, 4})

    print()
    by_cls = defaultdict(list)
    for a in aps.values():
        by_cls[a["cls"]].append(a)
    rows = []
    for cls in ("transient", "intermediate", "sustained"):
        g = by_cls.get(cls, [])
        strong = [a for a in g if a["rssi_avg"] is not None and a["rssi_avg"] >= args.close_dbm]
        manuf = Counter((a["manuf"] or "?") for a in g).most_common(3)
        rows.append((cls, len(g), pc(sum(1 for a in g if a["random_mac"]), len(g)),
                     pc(sum(1 for a in g if not a["ssid"]), len(g)), pc(len(strong), len(g)),
                     "; ".join("%s %d" % (m[:14], n) for m, n in manuf)))
    table(["class", "APs", "randomised BSSID", "hidden SSID", "close (avg RSSI)", "top manufacturers"],
          rows, align_right={1, 2, 3, 4})


def protected_matches(aps, history, prefixes):
    """{prefix: [ap...]} - current SSID matches, or any historical SSID matched."""
    groups = {p: [] for p in prefixes}
    for a in aps.values():
        ssids = {(a["ssid"] or "")} | history.get(a["key"], set())
        for p in prefixes:
            hits = [s for s in ssids if s.lower().startswith(p.lower())]
            if hits:
                a["protected"] = p
                a["ssid_hist"] = sorted(history.get(a["key"], set()) - {a["ssid"] or ""})
                a["dropped"] = not (a["ssid"] or "").lower().startswith(p.lower())
                groups[p].append(a)
                break
    return groups


def show_protected(db, aps, args, capture):
    section("3. PROTECTED SSIDs - IMPOSTOR CANDIDATES")
    if not args.protected:
        print("  no --protected prefixes given; skipped")
        return []
    prefixes = [p.strip() for p in args.protected.split(",") if p.strip()]
    infra = {o.strip().upper()[:8] for o in args.infra_oui.split(",") if o.strip()} \
        if args.infra_oui else None

    history = defaultdict(set)
    for key, ssid in db.execute(
            "SELECT key, ssid FROM device_config_history WHERE ssid IS NOT NULL AND ssid != ''"):
        history[key].add(ssid)
    groups = protected_matches(aps, history, prefixes)

    flagged = []
    for p in prefixes:
        g = groups[p]
        print()
        print("  --- %s : %d BSSID(s) ---" % (p, len(g)))
        if not g:
            continue
        for a in g:
            enrich(db, a)
        ouis = Counter(a["oui"] for a in g)
        if infra:
            infra_set, how = infra, "given"
        else:
            top = max(ouis.values())
            infra_set, how = {o for o, n in ouis.items() if n == top}, "inferred (most BSSIDs)"
        crypts = Counter(a["crypt"] for a in g if a["crypt"])
        dom_crypt = crypts.most_common(1)[0][0] if crypts else None
        meds = [a["rssi_med"] for a in g if a["rssi_med"] is not None]
        group_med = statistics.median(meds) if meds else None
        print("  infrastructure OUI: %s  %s;  OUIs seen: %s" % (
            ", ".join(sorted(infra_set)), how,
            ", ".join("%s x%d (%s)" % (o, n, next(a["manuf"] for a in g if a["oui"] == o) or "?")
                      for o, n in ouis.most_common())))
        print("  dominant encryption: %s;  group median RSSI: %s dBm;  BSSIDs first seen in "
              "baseline: %d, later: %d" % (
                  dom_crypt or "-", "%.0f" % group_med if group_med is not None else "-",
                  sum(1 for a in g if not a["late"]), sum(1 for a in g if a["late"])))
        for a in g:
            if a["oui"] not in infra_set:
                a["flags"].append("oui")
            if a["late"]:
                a["flags"].append("late")
            if group_med is not None and a["rssi_med"] is not None and len(g) > 1 \
                    and a["rssi_med"] >= group_med + args.strong_db:
                a["flags"].append("strong")
            if dom_crypt and a["crypt"] and a["crypt"] != dom_crypt:
                a["flags"].append("crypt")
            if a["random_mac"]:
                a["flags"].append("rand")
            if a["ssid_hist"]:
                a["flags"].append("dropped" if a["dropped"] else "adopted")
            if a["flags"]:
                flagged.append(a)
        list_aps(db, g, args, lambda a: (len(a["flags"]), a["rssi_med"] or -999))
        hist = [a for a in g if a["ssid_hist"]]
        if hist:
            print("  SSID history of BSSIDs in this group:")
            for a in hist:
                print("    %s: previously %s%s" % (
                    a["mac"], ", ".join(repr(s) for s in a["ssid_hist"]),
                    "; now %r" % a["ssid"] if a["dropped"] else ""))
        print("  flags: %s" % ("; ".join("%s x%d" % (f, n) for f, n in Counter(
            f for a in g for f in a["flags"]).most_common()) or "none"))

    # look-alike SSIDs
    print()
    print("  --- look-alike SSIDs (normalised or one-edit match of a protected prefix, not exact) ---")
    look = []
    for a in aps.values():
        if a["protected"] or not a["ssid"]:
            continue
        ns = norm_ssid(a["ssid"])
        for p in prefixes:
            np_ = norm_ssid(p)
            if len(np_) < 3:
                continue
            head = ns[:len(np_)]
            if ns.startswith(np_) or (len(np_) >= 4 and levenshtein(head, np_) <= 1):
                a["flags"].append("lookalike:%s" % p)
                look.append(a)
                break
    for a in look:
        enrich(db, a)
    list_aps(db, look, args, lambda a: (a["span"], a["rssi_med"] or -999))
    hidden = [a for a in aps.values() if not a["ssid"] and a["cls"] == "sustained"]
    print()
    print("  note: %d sustained APs have a hidden/empty SSID and cannot be matched to a protected "
          "SSID." % len(hidden))
    return flagged + look


def show_candidates(db, aps, protected_hits, args):
    section("4. PERSISTENCE x STRENGTH OF NEW / IMPOSTOR APs")
    later = [a for a in aps.values() if a["late"]]
    print("  all %d APs that appeared after the baseline window, by persistence and average RSSI:"
          % len(later))
    grid = Counter((a["cls"], strength(a, args)) for a in later)
    rows = []
    for cls in ("transient", "intermediate", "sustained"):
        n = sum(v for (c, _), v in grid.items() if c == cls)
        rows.append((cls, grid[(cls, "close")], grid[(cls, "far")], grid[(cls, "no-rssi")], n))
    rows.append(("total", sum(1 for a in later if strength(a, args) == "close"),
                 sum(1 for a in later if strength(a, args) == "far"),
                 sum(1 for a in later if strength(a, args) == "no-rssi"), len(later)))
    table(["persistence", "close (>= %d dBm)" % args.close_dbm, "far", "no RSSI", "total"], rows,
          align_right={1, 2, 3, 4})
    print("  -> the 'sustained + close' cell is the population a stationary sensor could act on;")
    print("     everything transient is neighbour/hotspot churn.")

    newcomers = [a for a in later if a["cls"] == "sustained"]
    for a in newcomers:
        enrich(db, a)
    close_new = [a for a in newcomers if strength(a, args) == "close"]
    for a in close_new:
        if "new-close" not in a["flags"]:
            a["flags"].append("new-close")

    cand = {a["key"]: a for a in protected_hits}
    for a in close_new:
        cand.setdefault(a["key"], a)
    cands = list(cand.values())
    print()
    print("  candidate list: %d = flagged protected-SSID BSSIDs (%d) + look-alikes (%d) + "
          "sustained close newcomers (%d)" % (
              len(cands), sum(1 for a in protected_hits if a["protected"]),
              sum(1 for a in protected_hits if not a["protected"]), len(close_new)))
    c = Counter((a["cls"], strength(a, args)) for a in cands)
    print("  by class: " + ", ".join("%s/%s %d" % (k[0], k[1], n) for k, n in sorted(c.items())))
    print()
    args_all = argparse.Namespace(**vars(args))
    args_all.list = max(args.list, len(cands))
    list_aps(db, cands, args_all,
             lambda a: (a["protected"] is not None, a["span"], a["rssi_med"] or -999))
    print("  span/n_obs/seen%/days describe persistence; rssi_med/max describe proximity.")
    return cands


def write_csv(path, cands, args):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "mac", "oui", "manuf", "ssid", "protected_prefix", "ssid_history",
                    "crypt", "adv_channel", "first_seen", "last_seen", "span_s", "class",
                    "n_obs", "seen_pct", "active_days", "rssi_med", "rssi_avg", "rssi_max",
                    "strength", "random_mac", "late", "absent", "flags"])
        for a in cands:
            w.writerow([a["key"], a["mac"], a["oui"], a["manuf"] or "", a["ssid"] or "",
                        a["protected"] or "", "|".join(a.get("ssid_hist") or []),
                        a["crypt"] or "", a["adv_channel"] or "", a["first_seen"], a["last_seen"],
                        a["span"], a["cls"], a["n_obs"],
                        "%.1f" % a["seen_pct"] if a["seen_pct"] is not None else "",
                        a["days"], a["rssi_med"] if a["rssi_med"] is not None else "",
                        "%.1f" % a["rssi_avg"] if a["rssi_avg"] is not None else "",
                        a["rssi_max"] if a["rssi_max"] is not None else "", strength(a, args),
                        int(a["random_mac"]), int(a["late"]), int(a["absent"]),
                        ",".join(a["flags"])])
    print("\n  candidate list written to %s" % path)


# --- main -------------------------------------------------------------------------------

def main():
    global USE_UTC
    ap = argparse.ArgumentParser(description="Read-only AP inventory change characterisation.")
    ap.add_argument("db", nargs="?", help="path to buffer.db (default: collector config)")
    ap.add_argument("--protected", default="",
                    help="comma-separated protected SSID prefixes, e.g. IK-WIFI,FRI_wifi")
    ap.add_argument("--infra-oui", default="",
                    help="comma-separated known infrastructure OUIs (AA:BB:CC); default: infer")
    ap.add_argument("--baseline-hours", type=float, default=24,
                    help="length of the initial baseline window (default 24)")
    ap.add_argument("--absent-hours", type=float, default=24,
                    help="an AP not seen for this long before the end counts as vanished (default 24)")
    ap.add_argument("--transient-min", type=float, default=60,
                    help="lifetime below this many minutes is transient (default 60)")
    ap.add_argument("--transient-polls", type=int, default=10,
                    help="fewer observations than this is transient (default 10)")
    ap.add_argument("--persistent-hours", type=float, default=24,
                    help="lifetime of at least this many hours is sustained (default 24)")
    ap.add_argument("--close-dbm", type=int, default=-60,
                    help="median RSSI at or above this is 'close' (default -60)")
    ap.add_argument("--strong-db", type=float, default=10,
                    help="dB above the group median RSSI to flag 'strong' (default 10)")
    ap.add_argument("--list", type=int, default=25, help="rows per list (default 25)")
    ap.add_argument("--csv", metavar="FILE", help="also write the candidate list as CSV")
    ap.add_argument("--utc", action="store_true", help="day boundaries and times in UTC")
    args = ap.parse_args()
    USE_UTC = args.utc

    path = resolve_db_path(args.db)
    db = open_readonly(path)
    print("wifi-sensor AP inventory change report  -  %s  -  %s" % (
        path, fmt_ts(datetime.now().timestamp())))

    span = db.execute("SELECT MIN(ts), MAX(ts) FROM polls").fetchone()
    if not span[0]:
        sys.exit("no polls in the buffer yet")
    capture = {"start": span[0], "end": span[1],
               "baseline_end": span[0] + args.baseline_hours * 3600}
    if capture["baseline_end"] >= capture["end"]:
        print("\n  WARNING: the capture (%s) is shorter than the baseline window; everything "
              "counts as baseline." % fmt_dur(capture["end"] - capture["start"]))

    aps = load_aps(db, args, capture)
    show_dataset(db, aps, capture, args)
    if not aps:
        print("\n  no AP devices in the buffer yet")
        return
    show_baseline(db, aps, args)
    show_churn(db, aps, capture, args)
    hits = show_protected(db, aps, args, capture)
    cands = show_candidates(db, aps, hits, args)
    if args.csv:
        write_csv(args.csv, cands, args)


if __name__ == "__main__":
    main()
