#!/usr/bin/env python3
"""The AP observation rows of given polls, as JSON on stdout (read-only).

For the rare case in which the server stored a poll but not all of its
observation rows, and the uploader cannot send them again: after a power cut
the buffer lost polls the uploader had sent, the next boot gave their row ids
to new rows and the cursor passed them (2026-11-02: 54 AP observations of
polls 123578 and 123579; the boot's clock anchor changed meanwhile, so a
re-send would store the two polls' rows twice, one second apart). The file
goes to the server's ops/merge_poll_rows.sh, which places the rows on the
stored polls' times. The rows leave the buffer with the retention - take the
extract before that.

  python3 /opt/wifi-sensor/analysis/extract_poll_rows.py 123578 123579 > poll_rows.json
  python3 analysis/extract_poll_rows.py --db /path/to/buffer.db 123578 123579

Only rows of devices with AP evidence (the uploader's rule) - no client row
leaves the sensor. The buffer is opened mode=ro; standard library only.
"""

import argparse
import json
import os
import sqlite3
import sys

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"
FORMAT = "wifi-sensor poll rows 1"
# = uploader/batch.py AP_EVIDENCE (a test keeps them equal)
AP_EVIDENCE = "type = 'ap' OR ssid IS NOT NULL OR crypt IS NOT NULL OR adv_channel IS NOT NULL"
POLL_COLS = ("id", "ts", "ok", "mono", "boot_id", "clock_synced", "devices_active", "new_obs")
OBS_COLS = ("id", "ts", "key", "last_time", "rssi", "rssi_floor", "pk_total", "pk_data", "disconnects",
            "disconnects_last", "bss_timestamp", "poll_id")


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
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def extract(db, poll_ids):
    """{"format", "buffer_id", "schema_version", "polls": [...], "observations": [...]}"""
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    have = {r[1] for r in db.execute("PRAGMA table_info(polls)")}
    cols = [c for c in POLL_COLS if c in have]
    polls, obs = [], []
    for pid in sorted(set(poll_ids)):
        p = db.execute("SELECT %s FROM polls WHERE id = ?" % ", ".join(cols), (pid,)).fetchone()
        if p is None:
            sys.exit("poll %d is not in the buffer (pruned, or never written)" % pid)
        polls.append(dict(p))
        # by ts first (indexed; a poll's rows carry its ts), then the poll id
        for r in db.execute(
                "SELECT %s FROM observations o WHERE o.ts = ? AND o.poll_id = ? AND EXISTS ("
                "SELECT 1 FROM devices d WHERE d.key = o.key AND (%s)) ORDER BY o.id"
                % (", ".join("o." + c for c in OBS_COLS), AP_EVIDENCE), (p["ts"], pid)):
            obs.append(dict(r))
    return {"format": FORMAT, "buffer_id": meta.get("buffer_id"), "schema_version": meta.get("schema_version"),
            "polls": polls, "observations": obs}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("polls", nargs="+", type=int, help="poll ids (polls.id = the server's seq)")
    ap.add_argument("--db", help="buffer.db (default: collector config)")
    args = ap.parse_args(argv)
    db = open_ro(db_path(args.db))
    try:
        db.execute("BEGIN")
        out = extract(db, args.polls)
    finally:
        db.close()
    json.dump(out, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")
    per = {}
    for o in out["observations"]:
        per[o["poll_id"]] = per.get(o["poll_id"], 0) + 1
    print("extract_poll_rows: %s" % ", ".join("poll %d: %d AP rows" % (p["id"], per.get(p["id"], 0))
                                              for p in out["polls"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
