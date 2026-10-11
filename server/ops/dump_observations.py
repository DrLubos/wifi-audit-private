#!/usr/bin/env python3
"""Restore the observation rows the schema-7 migration skipped, from a dump.

The schema-7 migration (2026-10-03) copied the observation rows of devices
whose type was 'ap' at that moment. Devices that Kismet classified as APs
only later (a later import takes the type from the newer row) lost their
rows from before the first snapshot that still carried them. A pg_dump taken
before the migration still has them.

Reads, on stdin, the SQL that `pg_restore --data-only -f -` writes for the
tables devices and observations of such a dump - devices first. Collects the
device_keys of one sensor whose type in the dump is not 'ap', and writes, on
stdout, a psql script that stages those devices' observation rows (the
columns the server keeps since schema 7) and, in one transaction, inserts
the rows of devices that are APs on the server NOW (ON CONFLICT DO NOTHING -
rows already there are skipped), reports what it inserted and refreshes
the baselines; after COMMIT it vacuums observations and refreshes the
rollups from the earliest restored ts. Re-running inserts nothing.

Run through ops/restore_observations_from_dump.sh. Standard library only.
"""

import argparse
import io
import re
import sys

COPY_RE = re.compile(r"^COPY (?:public\.)?(devices|observations) \((.*)\) FROM stdin;$")
# schema-7 observation columns (server/schema.sql), all present in schema 6
OBS_COLS = ("ts", "pk_total", "pk_data", "bss_timestamp", "disconnects_last", "disconnects",
            "rssi", "rssi_floor", "device_key")

PREAMBLE = """\\set ON_ERROR_STOP on
\\timing on
SET client_encoding = 'UTF8';
BEGIN;
-- session lifetime (survives COMMIT): what was inserted, for the refresh
CREATE TEMP TABLE restore_from (device_key text, rows bigint, min_ts timestamptz, max_ts timestamptz);
CREATE TEMP TABLE stage_restore (
  ts timestamptz, pk_total bigint, pk_data bigint, bss_timestamp bigint,
  disconnects_last timestamptz, disconnects integer, rssi smallint, rssi_floor boolean,
  device_key text) ON COMMIT DROP;
COPY stage_restore FROM STDIN;
"""

EPILOGUE = """\\.
ANALYZE stage_restore;
\\echo -- staged: observation rows of devices that were not APs in the dump
SELECT count(*) AS rows, count(DISTINCT device_key) AS devices, min(ts) AS first_ts,
       max(ts) AS last_ts FROM stage_restore;
\\echo -- inserted: rows of devices that are APs now (rows already on the server skipped)
WITH ins AS (
  INSERT INTO observations (ts, pk_total, pk_data, bss_timestamp, disconnects_last, sensor_id,
                            disconnects, rssi, rssi_floor, device_key)
  SELECT s.ts, s.pk_total, s.pk_data, s.bss_timestamp, s.disconnects_last, %(sid)d,
         s.disconnects, s.rssi, s.rssi_floor, s.device_key
  FROM stage_restore s
  JOIN devices d ON d.sensor_id = %(sid)d AND d.device_key = s.device_key AND d.type = 'ap'
  ORDER BY s.ts, s.device_key
  ON CONFLICT DO NOTHING
  RETURNING device_key, ts)
INSERT INTO restore_from SELECT device_key, count(*), min(ts), max(ts) FROM ins GROUP BY 1;
SELECT r.device_key, upper(d.mac::text) AS bssid, coalesce(nullif(d.ssid, ''), '(hidden)') AS ssid,
       r.rows, r.min_ts, r.max_ts
FROM restore_from r JOIN devices d ON d.sensor_id = %(sid)d AND d.device_key = r.device_key
ORDER BY r.rows DESC;
SELECT count(*) AS devices, coalesce(sum(rows), 0) AS rows_inserted FROM restore_from;
\\echo -- ap_baselines
SELECT refresh_ap_baselines(%(sid)d, 200, 2) AS ap_baselines_upserted;
COMMIT;
\\echo -- vacuum observations (visibility map for index-only scans, BRIN summary)
VACUUM (ANALYZE) observations;
\\echo -- refresh_rollups from the earliest restored ts (nothing restored: the latest hour)
SELECT * FROM refresh_rollups(%(sid)d, coalesce((SELECT min(min_ts) FROM restore_from), now()));
ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, ap_config_changes;
"""

ABORT = "DO $$ BEGIN RAISE EXCEPTION 'dump_observations.py: %s'; END $$;\n"


class CopyOpen(ValueError):
    """Raised after the stage COPY was opened: the caller must close it and roll back."""


def convert(lines, out, sensor_id, log):
    """Returns (non-AP devices in the dump, observation rows read, rows staged);
    raises ValueError on a missing block, a missing column, the wrong order or
    truncated input."""
    sid = str(sensor_id)
    table = None
    keys = None              # device_keys of the sensor that were not APs in the dump
    idx = sid_i = None
    read = staged = 0
    for line in lines:
        line = line.rstrip("\n")
        if table is None:
            m = COPY_RE.match(line)
            if not m:
                continue
            table, cols = m.group(1), [c.strip().strip('"') for c in m.group(2).split(",")]
            if table == "devices":
                for c in ("sensor_id", "device_key", "type"):
                    if c not in cols:
                        raise ValueError("devices in the dump lack %s" % c)
                d_sid, d_key, d_type = (cols.index(c) for c in ("sensor_id", "device_key", "type"))
                keys = set()
            else:
                if keys is None:
                    raise ValueError("observations before devices in the input")
                missing = [c for c in OBS_COLS + ("sensor_id",) if c not in cols]
                if missing:
                    raise ValueError("observations in the dump lack %s" % ", ".join(missing))
                idx = [cols.index(c) for c in OBS_COLS]
                sid_i = cols.index("sensor_id")
                n_cols = len(cols)
                out.write(PREAMBLE)
            continue
        if line == "\\.":
            if table == "observations":
                out.write(EPILOGUE % {"sid": sensor_id})
                return len(keys), read, staged
            table = None
            continue
        f = line.split("\t")
        if table == "devices":
            if len(f) <= max(d_sid, d_key, d_type):
                raise ValueError("malformed devices row (truncated input?)")
            if f[d_sid] == sid and f[d_type] != "ap":
                keys.add(f[d_key])
            continue
        read += 1
        if len(f) != n_cols:
            raise CopyOpen("observations row %d has %d of %d columns (truncated input?)"
                             % (read, len(f), n_cols))
        if f[sid_i] != sid or f[idx[-1]] not in keys:
            continue
        out.write("\t".join(f[i] for i in idx))
        out.write("\n")
        staged += 1
    if keys is None:
        raise ValueError("no COPY block for devices in the input")
    if idx is None:
        raise ValueError("no COPY block for observations in the input")
    raise CopyOpen("the observations COPY block has no end marker (truncated input)")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sensor-id", type=int, required=True, help="sensors.id of the dump's rows")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    lines = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    try:
        n_keys, read, staged = convert(lines, sys.stdout, args.sensor_id, log)
    except ValueError as e:
        # psql must fail (ON_ERROR_STOP) and never commit a partial restore
        sys.stdout.write("\\.\nROLLBACK;\n" if isinstance(e, CopyOpen) else "")
        sys.stdout.write(ABORT % str(e).replace("'", "").replace("$", ""))
        sys.stdout.flush()
        log("error: %s" % e)
        return 1
    log("dump_observations: %d devices of sensor %d were not APs in the dump; %d observation "
        "rows read, %d of theirs staged" % (n_keys, args.sensor_id, read, staged))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
