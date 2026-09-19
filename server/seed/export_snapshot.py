#!/usr/bin/env python3
"""Take a consistent, compact snapshot of the collector's SQLite buffer.

Runs on the Pi while the collector keeps writing; the source is opened
read-only and only the new file is written. Standard library only, so it can
be piped through ssh without installing anything on the sensor:

  ssh pi 'python3 - ~/buffer-snapshot.db' < server/seed/export_snapshot.py
  scp pi:~/buffer-snapshot.db .

Run it as the sensor user (the one that owns buffer.db and its -shm file).
Write the snapshot to the home directory or /var/tmp, not /tmp: on recent Pi
OS /tmp is a RAM disk and the buffer is a few hundred MB.

The buffer path is resolved like the collector does: --db, then $DB_PATH, then
DB_PATH from /etc/wifi-sensor/collector.conf ($COLLECTOR_CONF), then
/var/lib/wifi-sensor/buffer.db.

VACUUM INTO produces a compact copy (it drops the free pages the v1 -> v2
migration left behind) in rollback-journal mode, i.e. one self-contained file.
On an SQLite too old for VACUUM INTO (< 3.27) the backup API is used instead,
followed by a VACUUM of the copy.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

DEFAULT_CONF = "/etc/wifi-sensor/collector.conf"
DEFAULT_DB = "/var/lib/wifi-sensor/buffer.db"


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


def fmt_ts(ts):
    if ts is None:
        return "-"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def snapshot(src_path, out_path):
    # mode=ro: the source file is opened read-only; VACUUM INTO only reads it.
    # (PRAGMA query_only would also refuse the VACUUM, so it is not set.)
    src = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(src_path)),
                          uri=True, isolation_level=None)
    try:
        try:
            src.execute("VACUUM INTO ?", (out_path,))
            return "VACUUM INTO"
        except sqlite3.OperationalError as e:
            if "syntax error" not in str(e).lower():
                raise
        # SQLite < 3.27: page-level backup, then compact the copy.
        dst = sqlite3.connect(out_path, isolation_level=None)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("VACUUM")
        finally:
            dst.close()
        return "backup + VACUUM"
    finally:
        src.close()


def describe(path):
    db = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)
    try:
        version = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        print("  schema version: %s" % (version[0] if version else "?"))
        for t in ("polls", "devices", "device_config_history", "observations",
                  "device_freq_hist", "associations", "probes", "alerts"):
            print("  %-22s %9d" % (t, db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
        lo, hi = db.execute("SELECT MIN(ts), MAX(ts) FROM polls").fetchone()
        print("  polls span: %s .. %s" % (fmt_ts(lo), fmt_ts(hi)))
        print("  journal mode: %s" % db.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        db.close()


def main(argv):
    ap = argparse.ArgumentParser(
        description="Consistent, compact snapshot of the collector buffer (read-only on the source).")
    ap.add_argument("out", nargs="?", default="~/buffer-snapshot.db",
                    help="output file (default ~/buffer-snapshot.db; not /tmp, see the docstring)")
    ap.add_argument("--db", metavar="PATH", help="buffer to snapshot (default: collector config)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = ap.parse_args(argv)

    src = resolve_db_path(args.db)
    out = os.path.abspath(os.path.expanduser(args.out))
    if not os.path.isfile(src):
        sys.exit("no database at %s" % src)
    if os.path.exists(out):
        if not args.force:
            sys.exit("%s exists (use --force to overwrite)" % out)
        os.remove(out)

    print("snapshot of %s -> %s" % (src, out))
    how = snapshot(src, out)
    size = os.path.getsize(out)
    print("  method: %s; size: %.1f MB (source file %.1f MB)"
          % (how, size / 1e6, os.path.getsize(src) / 1e6))
    describe(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
