#!/usr/bin/env python3
"""Probing client devices per UTC hour from the collector buffer (read-only).

Counts client devices whose first probe request (any SSID, from the `probes`
table) falls into each UTC hour over the last --days full days, with the share
of locally administered (randomised) MACs. Kismet creates one device per MAC,
so with MAC-per-scan randomisation this counts MACs, not phones. Also prints
failed polls and polls with the datasource down in the window (exclude data
gaps, see docs/findings.md section 6). Counts only; no MACs or SSIDs.

Usage:  python3 buffer_hourly.py [DB_PATH] [--days N]
DB_PATH resolution as for the other analysis scripts: argument, DB_PATH env,
DB_PATH in /etc/wifi-sensor/collector.conf (or COLLECTOR_CONF), then
/var/lib/wifi-sensor/buffer.db.
"""
import argparse
import collections
import os
import sqlite3
import time

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"


def db_path(arg):
    if arg:
        return arg
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    conf = os.environ.get("COLLECTOR_CONF", DEFAULT_CONF)
    try:
        with open(conf) as fh:
            for line in fh:
                if line.strip().startswith("DB_PATH="):
                    return os.path.expanduser(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return DEFAULT_DB


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db", nargs="?")
    ap.add_argument("--days", type=int, default=3, help="full UTC days before today (default 3)")
    a = ap.parse_args()
    db = sqlite3.connect("file:%s?mode=ro" % db_path(a.db), uri=True)
    db.execute("PRAGMA query_only = 1")
    now = int(time.time())
    end = (now // 86400) * 86400
    start = end - a.days * 86400
    rows = db.execute(
        "SELECT d.mac, MIN(p.first_time) FROM probes p JOIN devices d ON d.key = p.key "
        "WHERE p.first_time >= ? AND p.first_time < ? GROUP BY p.key", (start, end)).fetchall()
    per_hour, la_hour = collections.Counter(), collections.Counter()
    for mac, ft in rows:
        hr = (ft % 86400) // 3600
        per_hour[hr] += 1
        la_hour[hr] += bool(int(mac.split(":")[0], 16) & 2)
    print("window (UTC): %s .. %s" % (time.strftime("%Y-%m-%d", time.gmtime(start)),
                                      time.strftime("%Y-%m-%d", time.gmtime(end - 1))))
    print("new probing devices: %d (%.0f per day), locally administered share %.3f"
          % (len(rows), len(rows) / a.days, sum(la_hour.values()) / max(1, len(rows))))
    print("hour_utc  avg_new_per_hour  la_share")
    for hr in range(24):
        print("%8d  %16.1f  %8s" % (hr, per_hour[hr] / a.days,
                                     "%.2f" % (la_hour[hr] / per_hour[hr]) if per_hour[hr] else "-"))
    failed, down, total = db.execute(
        "SELECT SUM(ok = 0), SUM(ds_running = 0), COUNT(*) FROM polls WHERE ts >= ? AND ts < ?",
        (start, end)).fetchone()
    print("polls in window: %d, failed %d, datasource down %d" % (total, failed or 0, down or 0))


if __name__ == "__main__":
    main()
