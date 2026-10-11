#!/usr/bin/env python3
"""Turn the schema-6 observations of a pg_dump into ap_load_hourly rows.

Reads, on stdin, the SQL that `pg_restore --data-only --table=observations -f -`
writes for a dump taken before the schema-7 migration (2026-10-03): one
`COPY public.observations (<columns>) FROM stdin;` block. Writes, on stdout,
a psql script that stages the per-poll AP load columns of one sensor's rows
(ts, device_key, n_clients, qbss_stations, util_pct; rows with n_clients
set) as the session temp table stage_load_samples and calls
load_ap_load_samples() (schema 8) in one transaction. Nothing is restored
into a table; memory stays bounded (line by line).

  docker compose exec -T db pg_restore --data-only --table=observations -f - < DUMP \\
    | python3 ops/dump_load_samples.py --sensor-id 1 \\
    | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1

Run through ops/backfill_ap_load_from_dump.sh. Standard library only.
"""

import argparse
import io
import re
import sys

COPY_RE = re.compile(r"^COPY (?:public\.)?observations \((.*)\) FROM stdin;$")
WANTED = ("ts", "device_key", "n_clients", "qbss_stations", "util_pct")
NULL = "\\N"

PREAMBLE = """\\set ON_ERROR_STOP on
\\timing on
SET client_encoding = 'UTF8';
BEGIN;
CREATE TEMP TABLE stage_load_samples (
  ts timestamptz, device_key text, n_clients integer, qbss_stations integer, util_pct real)
  ON COMMIT DROP;
COPY stage_load_samples FROM STDIN;
"""

EPILOGUE = """\\.
SELECT count(*) AS samples, min(ts) AS first_ts, max(ts) AS last_ts FROM stage_load_samples;
SELECT * FROM load_ap_load_samples(%d);
COMMIT;
"""

ABORT = ("DO $$ BEGIN RAISE EXCEPTION 'dump_load_samples.py: %s'; END $$;\n")


def convert(lines, out, sensor_id, log):
    """Copy the wanted columns of sensor_id's rows with load values. Returns
    (rows read, samples written); raises ValueError when there is no
    observations COPY block or a column is missing."""
    idx = None
    read = written = 0
    sid = str(sensor_id)
    for line in lines:
        line = line.rstrip("\n")
        if idx is None:
            m = COPY_RE.match(line)
            if m:
                cols = [c.strip().strip('"') for c in m.group(1).split(",")]
                missing = [c for c in WANTED + ("sensor_id",) if c not in cols]
                if missing:
                    raise ValueError("observations in the dump lack %s" % ", ".join(missing))
                idx = [cols.index(c) for c in WANTED]
                sid_i = cols.index("sensor_id")
                load_i = cols.index("n_clients")
                out.write(PREAMBLE)
            continue
        if line == "\\.":
            out.write(EPILOGUE % sensor_id)
            return read, written
        read += 1
        f = line.split("\t")
        if f[sid_i] != sid or f[load_i] == NULL:
            continue
        out.write("\t".join(f[i] for i in idx))
        out.write("\n")
        written += 1
        if written % 500000 == 0:
            log("  %d samples" % written)
    if idx is None:
        raise ValueError("no COPY block for observations in the input")
    raise ValueError("the observations COPY block has no end marker (truncated input)")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sensor-id", type=int, required=True, help="sensors.id of the dump's rows")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    lines = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    try:
        read, written = convert(lines, sys.stdout, args.sensor_id, log)
    except ValueError as e:
        # psql must fail (ON_ERROR_STOP) and never commit a partial load
        sys.stdout.write("\\.\nROLLBACK;\n" if "end marker" in str(e) else "")
        sys.stdout.write(ABORT % str(e).replace("'", "").replace("$", ""))
        sys.stdout.flush()
        log("error: %s" % e)
        return 1
    log("dump_load_samples: %d observation rows read, %d load samples of sensor %d"
        % (read, written, args.sensor_id))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
