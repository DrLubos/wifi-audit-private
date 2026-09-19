#!/usr/bin/env python3
"""Load a snapshot of the sensor's SQLite buffer into the server's PostgreSQL.

Reads the snapshot (see export_snapshot.py) and writes ONE SQL stream to stdout
that psql executes in a single transaction:

  python3 import_snapshot.py buffer-snapshot.db --sensor pi-fri \\
      | psql "$DATABASE_URL" -v ON_ERROR_STOP=1

Only the standard library is needed; psql does the talking to PostgreSQL, so
the script runs wherever psql runs (the server box, or a laptop with a tunnel).

What the stream does
  1. upserts the sensors row (--sensor NAME, optional --location/--description/--tz),
  2. per buffer table: CREATE TEMP TABLE stage_<x>, COPY ... FROM STDIN with the
     rows streamed straight out of SQLite (bounded memory), then
     INSERT INTO <x> ... SELECT ... FROM stage_<x> with the type conversions
     (unix seconds -> timestamptz, text -> macaddr, 0/1 -> boolean, JSON -> jsonb)
     and merge rules that make a re-import - or a later, overlapping snapshot -
     idempotent and accumulating:
       polls, observations, device_config_history, alerts: ON CONFLICT DO NOTHING
       devices:  first_seen = LEAST, last_seen = GREATEST, configuration and
                 type from whichever row was seen last
       associations, probes: LEAST / GREATEST of first / last
       device_freq_hist: the row with the newer updated_ts wins
  3. refreshes ap_baselines for the sensor (unless --no-baselines),
  4. prints per-table row counts for the sensor.

Everything runs between BEGIN and COMMIT, so a failure (bad MAC, FK violation,
lost connection) leaves the database untouched. psql's own "COPY n" / "INSERT 0 n"
command tags show what was staged and what was actually inserted.

Column and semantics reference: wifi-sensor/collector/store.py (buffer schema v2)
and server/schema.sql.
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

EXPECTED_SCHEMA_VERSION = "2"

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# COPY text format: backslash, tab, newline and carriage return are escaped;
# NUL cannot be stored in PostgreSQL text at all and is dropped.
_COPY_ESCAPE = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r", "\x00": ""})

# The advertised-configuration columns shared by devices and device_config_history.
_CONFIG_COLS = ("ssid", "cloaked", "crypt", "crypt_bits", "mfp_sup", "mfp_req",
                "adv_channel", "ht_mode", "beacon_rate", "country")


# --- table specs -----------------------------------------------------------------
#
# stage:  (pg_column, pg_type[, sqlite_expression]) - the staging table keeps the
#         buffer's representation (bigint seconds, text MACs, 0/1 flags); the
#         conversions happen in the INSERT ... SELECT.
# macs:   staging columns that must be a MAC address (validated before COPY).
# insert: the merge statement; :sid is the psql variable holding sensors.id.

def _stage_ddl(spec):
    return ", ".join("%s %s" % (c[0], c[1]) for c in spec["stage"])


def _stage_select(spec):
    cols = ", ".join((c[2] if len(c) > 2 else c[0]) for c in spec["stage"])
    return "SELECT %s FROM %s%s" % (cols, spec["name"], spec.get("order", ""))


def _newer_wins(col):
    """devices merge: take the column from the row whose last_seen is newer."""
    return ("%s = CASE WHEN EXCLUDED.last_seen >= devices.last_seen "
            "THEN EXCLUDED.%s ELSE devices.%s END" % (col, col, col))


_CONFIG_STAGE = [("ssid", "text"), ("cloaked", "integer"), ("crypt", "text"),
                 ("crypt_bits", "bigint"), ("mfp_sup", "integer"), ("mfp_req", "integer"),
                 ("adv_channel", "text"), ("ht_mode", "text"), ("beacon_rate", "integer"),
                 ("country", "text")]
_CONFIG_SELECT = ("ssid, cloaked::boolean, crypt, crypt_bits, mfp_sup::boolean, "
                  "mfp_req::boolean, adv_channel, ht_mode, beacon_rate, country")

TABLES = [
    {
        "name": "devices",
        "stage": [("device_key", "text", "key"), ("mac", "text"), ("type", "text"),
                  ("manuf", "text"), ("first_seen", "bigint"), ("last_seen", "bigint")]
                 + _CONFIG_STAGE + [("config_changed_at", "bigint")],
        "macs": ("mac",),
        "insert": (
            "INSERT INTO devices (sensor_id, device_key, mac, type, manuf, first_seen, last_seen, "
            + ", ".join(_CONFIG_COLS) + ", config_changed_at)\n"
            "SELECT :sid, device_key, mac::macaddr, type, manuf, to_timestamp(first_seen), "
            "to_timestamp(last_seen), " + _CONFIG_SELECT + ", to_timestamp(config_changed_at)\n"
            "FROM stage_devices\n"
            "ON CONFLICT (sensor_id, device_key) DO UPDATE SET\n  "
            + ",\n  ".join(
                [_newer_wins("mac"), _newer_wins("type"),
                 "manuf = COALESCE(CASE WHEN EXCLUDED.last_seen >= devices.last_seen "
                 "THEN EXCLUDED.manuf END, devices.manuf, EXCLUDED.manuf)",
                 "first_seen = LEAST(devices.first_seen, EXCLUDED.first_seen)"]
                + [_newer_wins(c) for c in _CONFIG_COLS + ("config_changed_at",)]
                # All SET expressions see the stored row, so the CASEs above
                # compare against the old last_seen regardless of order.
                + ["last_seen = GREATEST(devices.last_seen, EXCLUDED.last_seen)"])
            + ";"),
    },
    {
        "name": "polls",
        "order": " ORDER BY ts",
        "stage": [("ts", "bigint"), ("kismet_ts", "bigint"), ("devices_total", "integer"),
                  ("devices_active", "integer"), ("new_obs", "integer"), ("ds_running", "integer"),
                  ("ds_error", "text"), ("ds_packets", "bigint"), ("duration_ms", "integer"),
                  ("ok", "integer"), ("error", "text")],
        "macs": (),
        "insert": (
            "INSERT INTO polls (sensor_id, ts, kismet_ts, devices_total, devices_active, new_obs, "
            "ds_running, ds_error, ds_packets, duration_ms, ok, error)\n"
            "SELECT :sid, to_timestamp(ts), to_timestamp(kismet_ts), devices_total, devices_active, "
            "new_obs, ds_running::boolean, ds_error, ds_packets, duration_ms, ok::boolean, error\n"
            "FROM stage_polls\n"
            "ON CONFLICT DO NOTHING;"),
    },
    {
        "name": "device_config_history",
        "order": " ORDER BY key, ts",
        "stage": [("device_key", "text", "key"), ("ts", "bigint")] + _CONFIG_STAGE,
        "macs": (),
        "insert": (
            "INSERT INTO device_config_history (sensor_id, device_key, ts, "
            + ", ".join(_CONFIG_COLS) + ")\n"
            "SELECT :sid, device_key, to_timestamp(ts), " + _CONFIG_SELECT + "\n"
            "FROM stage_device_config_history\n"
            "ON CONFLICT DO NOTHING;"),
    },
    {
        "name": "observations",
        "order": " ORDER BY ts, key",
        "stage": [("ts", "bigint"), ("device_key", "text", "key"), ("last_time", "bigint"),
                  ("freq_khz", "integer"), ("channel", "text"), ("rssi", "smallint"),
                  ("rssi_min", "smallint"), ("rssi_max", "smallint"), ("pk_total", "bigint"),
                  ("pk_tx", "bigint"), ("pk_rx", "bigint"), ("pk_data", "bigint"),
                  ("bytes", "bigint"), ("n_clients", "integer"), ("disconnects", "integer"),
                  ("qbss_stations", "integer"), ("util_pct", "real"), ("bss_timestamp", "bigint"),
                  ("ie_checksum", "bigint"), ("beacon_fp", "bigint"), ("bssid", "text")],
        "macs": ("bssid",),
        "insert": (
            "INSERT INTO observations (ts, sensor_id, device_key, last_time, freq_khz, channel, "
            "rssi, rssi_min, rssi_max, pk_total, pk_tx, pk_rx, pk_data, bytes, n_clients, "
            "disconnects, qbss_stations, util_pct, bss_timestamp, ie_checksum, beacon_fp, bssid)\n"
            "SELECT to_timestamp(ts), :sid, device_key, to_timestamp(last_time), freq_khz, channel, "
            "rssi, rssi_min, rssi_max, pk_total, pk_tx, pk_rx, pk_data, bytes, n_clients, "
            "disconnects, qbss_stations, util_pct, bss_timestamp, ie_checksum, beacon_fp, "
            "bssid::macaddr\n"
            "FROM stage_observations\n"
            "ON CONFLICT DO NOTHING;"),
    },
    {
        "name": "device_freq_hist",
        "stage": [("device_key", "text", "key"), ("freq_khz", "integer"), ("packets", "bigint"),
                  ("updated_ts", "bigint")],
        "macs": (),
        "insert": (
            "INSERT INTO device_freq_hist (sensor_id, device_key, freq_khz, packets, updated_ts)\n"
            "SELECT :sid, device_key, freq_khz, packets, to_timestamp(updated_ts)\n"
            "FROM stage_device_freq_hist\n"
            "ON CONFLICT (sensor_id, device_key, freq_khz) DO UPDATE SET\n"
            "  packets = EXCLUDED.packets, updated_ts = EXCLUDED.updated_ts\n"
            "  WHERE EXCLUDED.updated_ts > device_freq_hist.updated_ts;"),
    },
    {
        "name": "associations",
        "stage": [("ap_key", "text"), ("client_mac", "text"), ("first_seen", "bigint"),
                  ("last_seen", "bigint")],
        "macs": ("client_mac",),
        "insert": (
            "INSERT INTO associations (sensor_id, ap_key, client_mac, first_seen, last_seen)\n"
            "SELECT :sid, ap_key, client_mac::macaddr, to_timestamp(first_seen), "
            "to_timestamp(last_seen)\n"
            "FROM stage_associations\n"
            "ON CONFLICT (sensor_id, ap_key, client_mac) DO UPDATE SET\n"
            "  first_seen = LEAST(associations.first_seen, EXCLUDED.first_seen),\n"
            "  last_seen = GREATEST(associations.last_seen, EXCLUDED.last_seen);"),
    },
    {
        "name": "probes",
        "stage": [("device_key", "text", "key"), ("ssid", "text"), ("first_time", "bigint"),
                  ("last_time", "bigint")],
        "macs": (),
        "insert": (
            "INSERT INTO probes (sensor_id, device_key, ssid, first_time, last_time)\n"
            "SELECT :sid, device_key, ssid, to_timestamp(first_time), to_timestamp(last_time)\n"
            "FROM stage_probes\n"
            "ON CONFLICT (sensor_id, device_key, ssid) DO UPDATE SET\n"
            "  first_time = LEAST(probes.first_time, EXCLUDED.first_time),\n"
            "  last_time = GREATEST(probes.last_time, EXCLUDED.last_time);"),
    },
    {
        "name": "alerts",
        "order": " ORDER BY id",
        "stage": [("hash", "text"), ("ts", "double precision"), ("header", "text"),
                  ("class", "text"), ("severity", "smallint"), ("source_mac", "text"),
                  ("dest_mac", "text"), ("transmitter_mac", "text"), ("other_mac", "text"),
                  ("channel", "text"), ("freq_khz", "integer"), ("device_key", "text"),
                  ("text", "text"), ("raw", "text")],
        "macs": ("source_mac", "dest_mac", "transmitter_mac", "other_mac"),
        "insert": (
            "INSERT INTO alerts (sensor_id, hash, ts, header, class, severity, source_mac, "
            "dest_mac, transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw)\n"
            "SELECT :sid, hash, to_timestamp(ts), header, class, severity, source_mac::macaddr, "
            "dest_mac::macaddr, transmitter_mac::macaddr, other_mac::macaddr, channel, freq_khz, "
            "device_key, text, raw::jsonb\n"
            "FROM stage_alerts\n"
            "ON CONFLICT (sensor_id, hash) DO NOTHING;"),
    },
]


# --- helpers -----------------------------------------------------------------------

class SnapshotError(Exception):
    pass


def open_snapshot(path):
    if not os.path.isfile(path):
        raise SnapshotError("no such file: %s" % path)
    db = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)
    db.execute("PRAGMA query_only = 1")
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as e:
        raise SnapshotError("%s does not look like a collector buffer (%s)" % (path, e))
    version = row[0] if row else None
    if version != EXPECTED_SCHEMA_VERSION:
        raise SnapshotError("buffer schema version %s, this importer expects %s"
                            % (version, EXPECTED_SCHEMA_VERSION))
    return db


def sql_literal(s):
    """Single-quoted SQL string literal (standard_conforming_strings assumed)."""
    return "'" + s.replace("\x00", "").replace("'", "''") + "'"


def copy_value(v):
    if v is None:
        return "\\N"
    if isinstance(v, str):
        return v.translate(_COPY_ESCAPE)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        raise SnapshotError("unexpected BLOB value")
    return str(v)


def fmt_ts(ts):
    if ts is None:
        return "-"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def snapshot_summary(db):
    out = {}
    for t in ("polls", "devices", "device_config_history", "observations",
              "device_freq_hist", "associations", "probes", "alerts"):
        out[t] = db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    lo, hi = db.execute("SELECT MIN(ts), MAX(ts) FROM polls").fetchone()
    return out, lo, hi


# --- stream generation ---------------------------------------------------------------

def emit_copy(out, db, spec, log):
    """CREATE TEMP TABLE + COPY the rows of one buffer table. Returns the row count."""
    name = spec["name"]
    stage = "stage_" + name
    mac_idx = [i for i, c in enumerate(spec["stage"]) if c[0] in spec["macs"]]
    out.write("\\echo -- %s\n" % name)
    out.write("CREATE TEMP TABLE %s (%s) ON COMMIT DROP;\n" % (stage, _stage_ddl(spec)))
    out.write("COPY %s FROM STDIN;\n" % stage)
    n = 0
    try:
        for row in db.execute(_stage_select(spec)):
            for i in mac_idx:
                v = row[i]
                if v is not None and not _MAC_RE.match(v):
                    raise SnapshotError("%s row %d: column %s is not a MAC address: %r"
                                        % (name, n + 1, spec["stage"][i][0], v))
            if name == "alerts":
                row = list(row)
                # jsonb rejects \u0000; the collector never produced one, but be safe.
                row[-1] = row[-1].replace("\\u0000", "\\ufffd") if row[-1] else row[-1]
            out.write("\t".join(copy_value(v) for v in row))
            out.write("\n")
            n += 1
    finally:
        out.write("\\.\n")
    log("  %-22s %9d rows staged" % (name, n))
    return n


def emit_stream(out, db, args, log):
    counts, lo, hi = snapshot_summary(db)
    out.write("-- generated by import_snapshot.py from %s\n" % os.path.basename(args.snapshot))
    out.write("-- snapshot span %s .. %s, %d polls, %d observations\n"
              % (fmt_ts(lo), fmt_ts(hi), counts["polls"], counts["observations"]))
    out.write("\\set ON_ERROR_STOP on\n")
    out.write("SET client_encoding = 'UTF8';\n")
    out.write("BEGIN;\n")

    # 1. sensor row; fields not given on the command line keep their stored value.
    tz_new = sql_literal(args.tz) if args.tz else None
    out.write(
        "INSERT INTO sensors (name, description, location, tz)\n"
        "VALUES (%s, %s, %s, %s)\n"
        "ON CONFLICT (name) DO UPDATE SET\n"
        "  description = COALESCE(EXCLUDED.description, sensors.description),\n"
        "  location = COALESCE(EXCLUDED.location, sensors.location),\n"
        "  tz = %s;\n" % (
            sql_literal(args.sensor),
            sql_literal(args.description) if args.description else "NULL",
            sql_literal(args.location) if args.location else "NULL",
            tz_new or "'UTC'",
            tz_new or "sensors.tz"))
    out.write("SELECT id AS sid FROM sensors WHERE name = %s \\gset\n" % sql_literal(args.sensor))
    # Fail early on an unknown time zone name instead of inside the baseline refresh.
    out.write("SELECT now() AT TIME ZONE (SELECT tz FROM sensors WHERE id = :sid) AS tz_check;\n")

    # 2. staged copy + merge, devices first (foreign keys).
    for spec in TABLES:
        emit_copy(out, db, spec, log)
        out.write(spec["insert"])
        out.write("\n")

    # 3. baselines
    if not args.no_baselines:
        out.write("\\echo -- ap_baselines\n")
        out.write("SELECT refresh_ap_baselines(:sid, %d, %d) AS ap_baselines_upserted;\n"
                  % (args.min_obs, args.min_days))

    # 4. summary
    out.write("\\echo -- row counts for sensor %s\n" % args.sensor)
    parts = ["('%s', (SELECT count(*) FROM %s WHERE sensor_id = :sid))" % (t, t)
             for t in ("polls", "devices", "device_config_history", "observations",
                       "device_freq_hist", "associations", "probes", "alerts",
                       "ap_baselines", "detections")]
    out.write("SELECT * FROM (VALUES\n  %s) AS t (table_name, n_rows);\n" % ",\n  ".join(parts))
    out.write("COMMIT;\n")


def emit_abort(out, reason):
    """Roll back and make psql exit non-zero with the reason in its output."""
    out.write("ROLLBACK;\n")
    out.write("DO $$ BEGIN RAISE EXCEPTION 'import_snapshot.py aborted: %%', %s; END $$;\n"
              % sql_literal(reason.replace("$", "")))
    out.flush()


# --- main ----------------------------------------------------------------------------

def main(argv):
    ap = argparse.ArgumentParser(
        description="Emit the SQL that loads a collector buffer snapshot into PostgreSQL "
                    "(pipe into psql).")
    ap.add_argument("snapshot", help="path to the SQLite snapshot (export_snapshot.py)")
    ap.add_argument("--sensor", required=True, metavar="NAME",
                    help="sensors.name to file the data under (created if missing)")
    ap.add_argument("--description", metavar="TEXT")
    ap.add_argument("--location", metavar="TEXT", help="free text; no coordinates")
    ap.add_argument("--tz", metavar="ZONE",
                    help="sensor time zone for daily statistics, e.g. Europe/Bratislava "
                         "(default: keep the stored value, UTC for a new sensor)")
    ap.add_argument("--no-baselines", action="store_true",
                    help="do not call refresh_ap_baselines() after loading")
    ap.add_argument("--min-obs", type=int, default=200,
                    help="baseline: minimum RSSI readings per AP (default 200)")
    ap.add_argument("--min-days", type=int, default=2,
                    help="baseline: minimum distinct days per AP (default 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only print what the snapshot contains, emit no SQL")
    args = ap.parse_args(argv)
    if not args.sensor.strip() or "\x00" in args.sensor:
        ap.error("--sensor must be a non-empty name")

    def log(msg):
        print(msg, file=sys.stderr)

    try:
        db = open_snapshot(args.snapshot)
        counts, lo, hi = snapshot_summary(db)
        log("snapshot %s: %s .. %s" % (args.snapshot, fmt_ts(lo), fmt_ts(hi)))
        for t, n in counts.items():
            log("  %-22s %9d" % (t, n))
        if args.dry_run:
            return 0

        # The SQL stream must be UTF-8 whatever the locale says; anything the
        # buffer holds that is not encodable is replaced rather than fatal.
        out = sys.stdout
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
        try:
            emit_stream(out, db, args, log)
        except SnapshotError as e:
            # The COPY block is already closed by emit_copy; abort the transaction
            # so psql never commits a partial load.
            emit_abort(out, str(e))
            raise
        out.flush()
        log("done: SQL stream complete")
        return 0
    except SnapshotError as e:
        log("error: %s" % e)
        return 1
    except BrokenPipeError:
        # psql went away (it stops at the first error); silence the interpreter's
        # own complaint about the closed pipe at exit.
        log("error: output pipe closed - see psql's message above")
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
