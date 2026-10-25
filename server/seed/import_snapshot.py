#!/usr/bin/env python3
"""Load a snapshot of the sensor's SQLite buffer into the server's PostgreSQL.

Reads the snapshot (see export_snapshot.py) and writes ONE SQL stream to stdout
that psql executes in a single transaction:

  python3 import_snapshot.py buffer-snapshot.db --sensor pi-fri \\
      | psql "$DATABASE_URL" -v ON_ERROR_STOP=1

Only the standard library is needed; psql does the talking to PostgreSQL, so
the script runs wherever psql runs (the server box, or a laptop with a tunnel).

Since live upload (server schema 14, docs/live-upload.md) this is the
recovery path: the sensor's uploader is the normal one, and both go through
the same server-side merge, so they cannot drift apart.

What the stream does
  1. upserts the sensors row (--sensor NAME, optional --location/--description/--tz),
  2. SELECT ingest_stage_create() - the session staging tables stage_<x>
     (schema.sql), shared with the live ingest - and per buffer table a
     COPY stage_<x> (columns) FROM STDIN with the rows streamed straight out
     of SQLite (bounded memory), in the buffer's representation (unix
     seconds, text MACs, 0/1 flags); polls.id goes in as seq (the buffer's
     row id: the server finds polls that never arrived by its gaps),
  3. SELECT ingest_merge(sensor, 'snapshot'): the type conversions and the
     merge rules that make a re-import - or a later, overlapping snapshot,
     or rows the live ingest already delivered - idempotent and accumulating
     (polls, observations, history, alerts: nothing twice; devices: first /
     last seen widened, configuration from the newest row; client tables:
     first / last widened; device_freq_hist: newer wins; ap_load_hourly and
     the per-poll AP load of buffers <= v5 (load_ap_load_samples()): the row
     with more polls wins). It returns the staged / inserted rows per table
     and the earliest inserted ts (the psql variable :merged),
  4. records the import in sensor_uploads (schema 9: span, buffer version,
     inserted rows, the buffer size export_snapshot.py stored in the copy),
  5. refreshes ap_baselines for the sensor (unless --no-baselines),
  6. prints per-table row counts for the sensor.

The client tables (associations, client_bssids, probes) and the client
devices themselves (their rows and frequency histograms) stay on the Pi
unless --with-client-tables (live upload decision D4: nothing on the server
reads them; the live ingest accepts AP devices only). Rows already on the
server are not touched (ops/delete_client_rows.sh, a later decision).

Everything runs between BEGIN and COMMIT, so a failure (bad MAC, FK violation,
lost connection) leaves the database untouched. After COMMIT the stream
  7. vacuums observations (outside the transaction), so the new heap pages are
     all-visible and the AP queries stay index-only scans (schema.sql, covering
     primary key observations_pkey, INCLUDE (rssi, rssi_floor)),
  8. calls refresh_rollups(sensor, earliest inserted ts) (schema 6; unless
     --no-rollups): the dashboard tables from that hour on, the whole history
     on the sensor's first refresh. It runs in its own transaction: if it
     fails, the import stays and `SELECT * FROM refresh_rollups(<id>)` can be
     re-run by hand (idempotent).

Column and semantics reference: wifi-sensor/collector/store.py (buffer schema v2
to v7; v3 added observations.disconnects_last, v4 polls.ds_hop_*, v5
observations.rssi_floor; older snapshots import them as NULL - their floor
readings stay raw in rssi and are read through rssi_valid(); v6 devices.cur_*
and client_bssids; v7 ap_load_hourly) and server/schema.sql (schema 14:
ingest_stage_create(), ingest_merge()).
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

EXPECTED_SCHEMA_VERSIONS = ("2", "3", "4", "5", "6", "7")

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# COPY text format: backslash, tab, newline and carriage return are escaped;
# NUL cannot be stored in PostgreSQL text at all and is dropped.
_COPY_ESCAPE = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r", "\x00": ""})

# The advertised-configuration columns shared by devices and device_config_history.
# --- table specs -----------------------------------------------------------------
#
# name:   the buffer table; the staging table is stage_<name> (created by the
#         server's ingest_stage_create(), schema 14).
# stage:  (stage_column, pg_type[, sqlite_expression]) - the columns this
#         importer fills, in COPY order; the type is documentation (the
#         server's staging table defines it; tests/test_schema_files.py keeps
#         the names equal).
# optional: staging columns that older buffer versions lack; they are selected
#         as NULL when the snapshot's table does not have them.
# where / order: SQLite filter and order on the snapshot rows.
# select: function(db) -> the SQLite SELECT, instead of the generated one.
# macs:   staging columns that must be a MAC address (validated before COPY).
# optional_table: the buffer table exists only from some version on; a
#         snapshot without it skips the spec.
# client: client data, staged only with --with-client-tables (D4).
# ap_only: without --with-client-tables only the rows of AP devices (the
#         snapshot's AP evidence) - what the live ingest accepts.
# The merge itself is ingest_merge() in schema.sql.

def _stage_select(spec, db, with_clients=False):
    if "select" in spec:
        return spec["select"](db)
    where = spec.get("where", "")
    if spec.get("ap_only") and not with_clients:
        where = " WHERE %s IN (SELECT key FROM devices WHERE %s)" % (spec["ap_only"], _AP_EVIDENCE)
    present = {r[1] for r in db.execute("PRAGMA table_info(%s)" % spec["name"])}
    cols = []
    for c in spec["stage"]:
        expr = c[2] if len(c) > 2 else c[0]
        if c[0] in spec.get("optional", ()) and expr not in present:
            expr = "NULL AS " + c[0]
        cols.append(expr)
    return "SELECT %s FROM %s%s%s" % (", ".join(cols), spec["name"], where, spec.get("order", ""))


# A device of the snapshot that has been an AP (server schema 10,
# devices.ever_ap): typed ap, or carrying an advertised configuration - the
# collector writes one only for a device that beaconed an SSID or was typed
# ap, and keeps it when Kismet re-types the device later. The server side
# reads the merged devices.ever_ap (set by its trigger), which also remembers
# APs of earlier snapshots.
_AP_EVIDENCE = "type = 'ap' OR ssid IS NOT NULL OR crypt IS NOT NULL OR adv_channel IS NOT NULL"


# Per-poll AP load samples of observation rows written by buffers <= v5 (v6
# and later leave the columns NULL), for load_ap_load_samples(). The ts goes
# in as timestamptz text, the shape that function reads (shared with the dump
# backfill, ops/backfill_ap_load_from_dump.sh); non-AP rows never carry the
# columns, the AP filter is a guard.
def _load_samples_select(db):
    return ("SELECT strftime('%Y-%m-%d %H:%M:%S+00', ts, 'unixepoch'), key, n_clients, "
            "qbss_stations, util_pct FROM observations WHERE n_clients IS NOT NULL "
            "AND key IN (SELECT key FROM devices WHERE " + _AP_EVIDENCE + ")")


def _client_bssids_select(db):
    """client_bssids of a v6 buffer plus the per-poll observations.bssid of
    older rows (v5 and earlier write it; v6 leaves it NULL), deduplicated."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    parts = ["SELECT key, bssid, ts AS f, ts AS l FROM observations WHERE bssid IS NOT NULL"]
    if "client_bssids" in tables:
        parts.append("SELECT client_key, bssid, first_seen, last_seen FROM client_bssids")
    return ("SELECT key, bssid, MIN(f), MAX(l) FROM (%s) GROUP BY key, bssid ORDER BY key, bssid"
            % " UNION ALL ".join(parts))


_CONFIG_STAGE = [("ssid", "text"), ("cloaked", "integer"), ("crypt", "text"),
                 ("crypt_bits", "bigint"), ("mfp_sup", "integer"), ("mfp_req", "integer"),
                 ("adv_channel", "text"), ("ht_mode", "text"), ("beacon_rate", "integer"),
                 ("country", "text")]
_CUR_COLS = ("cur_n_clients", "cur_qbss_stations", "cur_util_pct", "cur_at")

TABLES = [
    {
        "name": "devices",
        "stage": [("device_key", "text", "key"), ("mac", "text"), ("type", "text"),
                  ("manuf", "text"), ("first_seen", "bigint"), ("last_seen", "bigint")]
                 + _CONFIG_STAGE + [("config_changed_at", "bigint"),
                                    ("cur_n_clients", "integer"), ("cur_qbss_stations", "integer"),
                                    ("cur_util_pct", "real"), ("cur_at", "bigint")],
        "optional": _CUR_COLS,                                      # buffer v6
        "ap_only": "key",
        "macs": ("mac",),
    },
    {
        "name": "polls",
        "order": " ORDER BY ts",
        "stage": [("seq", "bigint", "id"), ("ts", "bigint"), ("kismet_ts", "bigint"),
                  ("devices_total", "integer"), ("devices_active", "integer"), ("new_obs", "integer"),
                  ("ds_running", "integer"), ("ds_error", "text"), ("ds_packets", "bigint"),
                  ("ds_hop_n", "integer"), ("ds_hop_visited", "integer"), ("ds_hop_ok", "integer"),
                  ("duration_ms", "integer"), ("ok", "integer"), ("error", "text"),
                  ("config_version", "integer")],
        "optional": ("ds_hop_n", "ds_hop_visited", "ds_hop_ok",    # buffer v4
                     "config_version"),                             # buffer v9
        "macs": (),
    },
    {
        "name": "device_config_history",
        "order": " ORDER BY key, ts",
        "stage": [("device_key", "text", "key"), ("ts", "bigint")] + _CONFIG_STAGE,
        "macs": (),
    },
    {
        # Server schema 7: AP rows only, the columns the detectors and the
        # dashboard read. The buffer's other columns (last_time, freq_khz,
        # channel, rssi_min/max, pk_tx/rx, bytes, n_clients, qbss_stations,
        # util_pct, ie_checksum, beacon_fp, bssid) are not read; a client's
        # BSSID comes in through client_bssids (with --with-client-tables),
        # the AP load through devices.cur_*. Rows of devices that have never
        # been an AP are left out on the Pi side already (the snapshot's AP
        # evidence) and again by the merge (the merged devices.ever_ap,
        # schema 10): an AP that Kismet re-typed as a client keeps its
        # observations.
        "name": "observations",
        "where": " WHERE key IN (SELECT key FROM devices WHERE " + _AP_EVIDENCE + ")",
        "order": " ORDER BY ts, key",
        "stage": [("ts", "bigint"), ("device_key", "text", "key"), ("rssi", "smallint"),
                  ("rssi_floor", "integer"), ("pk_total", "bigint"), ("pk_data", "bigint"),
                  ("disconnects", "integer"), ("disconnects_last", "bigint"),
                  ("bss_timestamp", "bigint")],
        "optional": ("disconnects_last", "rssi_floor"),   # buffer v3, v5
        "macs": (),
    },
    {
        # Buffer v7: the collector's hourly AP load rows; schema 8.
        "name": "ap_load_hourly",
        "optional_table": True,
        "stage": [("device_key", "text", "key"), ("hour", "bigint"), ("channel", "text"),
                  ("polls", "integer"), ("clients_polls", "integer"), ("clients_max", "integer"),
                  ("clients_sum", "integer"), ("qbss_polls", "integer"), ("qbss_max", "integer"),
                  ("qbss_sum", "integer"), ("util_max", "real"), ("util_sum", "real")],
        "macs": (),
    },
    {
        # The per-poll AP load of rows from buffers <= v5 -> ap_load_hourly
        # (schema 8, load_ap_load_samples(), source 'v5_obs').
        "name": "load_samples",
        "select": _load_samples_select,
        "stage": [("ts", "timestamptz"), ("device_key", "text"), ("n_clients", "integer"),
                  ("qbss_stations", "integer"), ("util_pct", "real")],
        "macs": (),
    },
    {
        "name": "device_freq_hist",
        "ap_only": "key",
        "stage": [("device_key", "text", "key"), ("freq_khz", "integer"), ("packets", "bigint"),
                  ("updated_ts", "bigint")],
        "macs": (),
    },
    {
        "name": "associations",
        "client": True,
        "stage": [("ap_key", "text"), ("client_mac", "text"), ("first_seen", "bigint"),
                  ("last_seen", "bigint")],
        "macs": ("client_mac",),
    },
    {
        "name": "client_bssids",
        "client": True,
        "select": _client_bssids_select,
        "stage": [("device_key", "text"), ("bssid", "text"), ("first_seen", "bigint"),
                  ("last_seen", "bigint")],
        "macs": ("bssid",),
    },
    {
        "name": "probes",
        "client": True,
        "stage": [("device_key", "text", "key"), ("ssid", "text"), ("first_time", "bigint"),
                  ("last_time", "bigint")],
        "macs": (),
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
    if version not in EXPECTED_SCHEMA_VERSIONS:
        raise SnapshotError("buffer schema version %s, this importer expects one of %s"
                            % (version, ", ".join(EXPECTED_SCHEMA_VERSIONS)))
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
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("polls", "devices", "device_config_history", "observations",
              "device_freq_hist", "associations", "client_bssids", "probes", "alerts",
              "ap_load_hourly"):
        if t in tables:                              # client_bssids: v6, ap_load_hourly: v7
            out[t] = db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    lo, hi = db.execute("SELECT MIN(ts), MAX(ts) FROM polls").fetchone()
    return out, lo, hi


# --- stream generation ---------------------------------------------------------------

def emit_copy(out, db, spec, log, with_clients=False):
    """COPY the rows of one buffer table into its staging table (created by
    ingest_stage_create()). Returns the row count."""
    name = spec["name"]
    mac_idx = [i for i, c in enumerate(spec["stage"]) if c[0] in spec["macs"]]
    out.write("\\echo -- %s\n" % name)
    out.write("COPY stage_%s (%s) FROM STDIN;\n" % (name, ", ".join(c[0] for c in spec["stage"])))
    n = 0
    try:
        for row in db.execute(_stage_select(spec, db, with_clients)):
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

    # 2. the staging tables of the server (schema 14), one COPY per buffer table
    out.write("SELECT ingest_stage_create();\n")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for spec in TABLES:
        if spec.get("optional_table") and spec["name"] not in tables:
            log("  %-22s   (not in this buffer version)" % spec["name"])
            continue
        if spec.get("client") and not args.with_client_tables:
            log("  %-22s   (client table: left on the sensor, --with-client-tables)" % spec["name"])
            continue
        emit_copy(out, db, spec, log, args.with_client_tables)

    # 3. the merge shared with the live ingest; :merged = {staged, inserted, conflicts, min_ts}
    out.write("\\echo -- ingest_merge\n")
    out.write("SELECT ingest_merge(:sid, 'snapshot') AS merged \\gset\n")

    # 4. the upload record (schema 9): what came in and from which buffer
    meta = dict(db.execute("SELECT key, value FROM meta"))
    src_bytes = meta.get("snapshot_source_bytes")
    out.write("\\echo -- sensor_uploads\n")
    out.write(
        "INSERT INTO sensor_uploads (sensor_id, via, buffer_version, polls_from, polls_to, "
        "buffer_bytes, rows)\n"
        "SELECT :sid, 'snapshot', %s, %s, %s, %s,\n"
        "       coalesce(:'merged'::jsonb -> 'inserted', '{}'::jsonb);\n"
        % (sql_literal(str(meta.get("schema_version"))),
           "to_timestamp(%d)" % lo if lo is not None else "NULL",
           "to_timestamp(%d)" % hi if hi is not None else "NULL",
           int(src_bytes) if src_bytes and src_bytes.isdigit() else "NULL"))

    # 5. baselines
    if not args.no_baselines:
        out.write("\\echo -- ap_baselines\n")
        out.write("SELECT refresh_ap_baselines(:sid, %d, %d) AS ap_baselines_upserted;\n"
                  % (args.min_obs, args.min_days))

    # 6. summary
    out.write("\\echo -- row counts for sensor %s\n" % args.sensor)
    parts = ["('%s', (SELECT count(*) FROM %s WHERE sensor_id = :sid))" % (t, t)
             for t in ("polls", "devices", "device_config_history", "observations",
                       "ap_load_hourly", "device_freq_hist", "associations", "client_bssids",
                       "probes", "alerts", "ap_baselines", "detections")]
    out.write("SELECT * FROM (VALUES\n  %s) AS t (table_name, n_rows);\n" % ",\n  ".join(parts))
    out.write("COMMIT;\n")

    # 7. visibility map: the AP queries are index-only scans on the covering index
    #    and fetch the heap row for every page not marked all-visible; autovacuum's
    #    insert threshold is ~20 % of the table, i.e. days of imports. VACUUM cannot
    #    run in a transaction block, and ON_ERROR_STOP keeps a failed load from
    #    getting here.
    out.write("\\echo -- vacuum observations (visibility map for index-only scans)\n")
    out.write("VACUUM (ANALYZE) observations;\n")
    out.write("\\echo -- staged / inserted rows per table, earliest inserted ts\n")
    out.write("SELECT jsonb_pretty(:'merged'::jsonb) AS merged;\n")

    # 8. dashboard tables from the earliest inserted ts (nothing new: the
    #    current hour and the per-AP/per-sensor rows; the sensor's first
    #    refresh covers everything). Own transaction, after the vacuum.
    if not args.no_rollups:
        out.write("\\echo -- refresh_rollups (dashboard tables)\n")
        out.write("SELECT * FROM refresh_rollups(:sid, "
                  "coalesce((:'merged'::jsonb ->> 'min_ts')::timestamptz, now()));\n")
        out.write("ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, "
                  "ap_config_changes;\n")


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
    ap.add_argument("--no-rollups", action="store_true",
                    help="do not call refresh_rollups() after loading (run it by hand later)")
    ap.add_argument("--min-obs", type=int, default=200,
                    help="baseline: minimum RSSI readings per AP (default 200)")
    ap.add_argument("--min-days", type=int, default=2,
                    help="baseline: minimum distinct days per AP (default 2)")
    ap.add_argument("--with-client-tables", action="store_true",
                    help="also load the client devices, associations, client_bssids and "
                         "probes (client data; off by default since live upload, decision D4)")
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
