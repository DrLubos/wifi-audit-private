#!/usr/bin/env python3
"""Observation rows of polls the server stored without all of their rows ->
a psql script that merges them through ingest_merge().

Reads the JSON that the sensor's analysis/extract_poll_rows.py writes (the AP
observation rows of given polls, straight from the buffer) and writes, on
stdout, a psql script: one transaction that

- finds, for every poll of the extract with rows, THE stored poll of the
  sensor: the same seq and the same time on the sensor's own clock (stored
  ts - ts_offset_s = the buffer's raw ts). No such poll, or more than one
  (a seq the buffer gave out twice), stops the script - nothing is guessed;
- stages each row with the stored poll's ts - the time its sibling rows of
  the same poll carry on the server, whatever anchor the uploader had then -
  and its Kismet time (disconnects_last) moved by the poll's stored offset;
- runs ingest_merge(sensor, 'snapshot'): the one merge of the live ingest
  and the snapshot import - AP rows only, rows already stored skipped
  (ON CONFLICT DO NOTHING), discard windows honoured, the refresher's dirty
  mark moved back to the earliest new row;
- reports the rows per poll before and after and the merge's own counts.

A dry run unless psql gets -v apply=1 (rolled back). Re-running inserts
nothing. Run through ops/merge_poll_rows.sh. Standard library only
(tests/test_poll_rows_sql.py).
"""

import argparse
import json
import sys

FORMAT = "wifi-sensor poll rows 1"
STAGE_COLS = ("seq", "raw_ts", "device_key", "rssi", "rssi_floor", "pk_total", "pk_data", "disconnects",
              "disconnects_last", "bss_timestamp")
INT_FIELDS = ("rssi", "rssi_floor", "pk_total", "pk_data", "disconnects", "disconnects_last", "bss_timestamp")


class Bad(Exception):
    pass


def _int(v, what, null=True):
    if v is None and null:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise Bad("%s must be an integer, not %r" % (what, v))
    return v


def rows(extract):
    """[(seq, raw_ts, device_key, rssi, ...)] of the extract, checked."""
    if not isinstance(extract, dict) or extract.get("format") != FORMAT:
        raise Bad("not an extract of analysis/extract_poll_rows.py (format %r expected)" % FORMAT)
    polls = {}
    for p in extract.get("polls") or []:
        pid = _int(p.get("id"), "polls.id", null=False)
        polls[pid] = _int(p.get("ts"), "polls.ts", null=False)
    out, seen = [], set()
    for o in extract.get("observations") or []:
        pid = _int(o.get("poll_id"), "observations.poll_id", null=False)
        if pid not in polls:
            raise Bad("observation %r belongs to poll %d, which is not in the extract" % (o.get("id"), pid))
        if _int(o.get("ts"), "observations.ts", null=False) != polls[pid]:
            raise Bad("observation %r does not carry the ts of its poll %d" % (o.get("id"), pid))
        key = o.get("key")
        if not isinstance(key, str) or not key or any(c in key for c in "\t\n\r\\"):
            raise Bad("observation %r: bad device key %r" % (o.get("id"), key))
        if (pid, key) in seen:
            raise Bad("poll %d has two rows of %s" % (pid, key))
        seen.add((pid, key))
        out.append((pid, polls[pid], key) + tuple(_int(o.get(f), "observations." + f) for f in INT_FIELDS))
    if not out:
        raise Bad("the extract holds no observation row")
    return out


def _copy(v):
    return "\\N" if v is None else str(v)


SCRIPT = """\\set ON_ERROR_STOP on
\\pset pager off
SET client_encoding = 'UTF8';
BEGIN;
SET LOCAL lock_timeout = '30s';
SELECT ingest_stage_create();
CREATE TEMP TABLE merge_poll_rows (
  seq bigint, raw_ts bigint, device_key text, rssi smallint, rssi_floor integer, pk_total bigint,
  pk_data bigint, disconnects integer, disconnects_last bigint, bss_timestamp bigint) ON COMMIT DROP;
COPY merge_poll_rows (%(cols)s) FROM STDIN;
%(data)s
\\.
-- THE stored poll of each extract poll: same seq, same time on the sensor's clock
CREATE TEMP TABLE merge_polls ON COMMIT DROP AS
SELECT m.seq, m.raw_ts, m.n AS extract_rows, count(p.seq) AS stored_polls, min(p.ts) AS ts,
       min(p.ts_offset_s) AS ts_offset_s
FROM (SELECT seq, raw_ts, count(*) AS n FROM merge_poll_rows GROUP BY seq, raw_ts) m
LEFT JOIN polls p ON p.sensor_id = %(sid)d AND p.seq = m.seq
  AND p.ts - make_interval(secs => coalesce(p.ts_offset_s, 0)) = to_timestamp(m.raw_ts)
GROUP BY m.seq, m.raw_ts, m.n;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM merge_polls WHERE stored_polls <> 1 ORDER BY seq LOOP
    RAISE EXCEPTION 'poll_rows_sql.py: poll %% (sensor clock %%) matches %% stored polls of sensor %(sid)d - nothing merged',
      r.seq, to_timestamp(r.raw_ts), r.stored_polls;
  END LOOP;
END $$;
\\echo == polls of the extract: stored ts, rows in the extract, AP rows stored before
SELECT m.seq, m.ts, m.ts_offset_s, m.extract_rows,
       (SELECT count(*) FROM observations o WHERE o.sensor_id = %(sid)d AND o.ts = m.ts) AS stored_before
FROM merge_polls m ORDER BY m.seq;
INSERT INTO stage_observations (ts, device_key, rssi, rssi_floor, pk_total, pk_data, disconnects,
                                disconnects_last, bss_timestamp)
SELECT extract(epoch FROM m.ts)::bigint, r.device_key, r.rssi, r.rssi_floor, r.pk_total, r.pk_data, r.disconnects,
       r.disconnects_last + round(coalesce(m.ts_offset_s, 0))::bigint, r.bss_timestamp
FROM merge_poll_rows r JOIN merge_polls m ON m.seq = r.seq AND m.raw_ts = r.raw_ts;
\\echo == ingest_merge
SELECT jsonb_pretty(ingest_merge(%(sid)d, 'snapshot') - 'excluded') AS merge;
\\echo == AP rows stored after
SELECT m.seq, m.ts, m.extract_rows,
       (SELECT count(*) FROM observations o WHERE o.sensor_id = %(sid)d AND o.ts = m.ts) AS stored_after
FROM merge_polls m ORDER BY m.seq;
\\if :{?apply}
COMMIT;
\\echo merge_poll_rows: committed
\\else
ROLLBACK;
\\echo merge_poll_rows: dry run, rolled back - run again with --apply
\\endif
"""


def script(extract, sensor_id):
    data = "\n".join("\t".join(_copy(v) for v in r) for r in rows(extract))
    return SCRIPT % {"cols": ", ".join(STAGE_COLS), "data": data, "sid": sensor_id}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("extract", help="JSON written by analysis/extract_poll_rows.py")
    ap.add_argument("--sensor-id", type=int, required=True)
    args = ap.parse_args(argv)
    try:
        with open(args.extract) as f:
            extract = json.load(f)
        sys.stdout.write(script(extract, args.sensor_id))
    except (Bad, ValueError, OSError) as e:
        print("poll_rows_sql.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
