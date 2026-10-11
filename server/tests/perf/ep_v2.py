"""The v2 endpoints' SQL as a psql script: EXPLAIN (ANALYZE, BUFFERS) or a run.

The statements are the api's own (app/v2/sql/*.sql, via app/v2/queries.py),
so this cannot drift from the code. Parameters: the period buckets come from
app/v2/periods.py (range, account time zone, now); the account's sensors, its
latest capture, the busiest AP and the newest event are looked up by the
script itself (\\gset). Read-only - pipe it into psql as claude_ro:

  cd server
  python3 tests/perf/ep_v2.py [--range 30d] [--mode explain|json] [--now 2026-10-06T19:30:00Z] \\
      overview sensors.sensors ... | ssh google 'cd ~/wifi-audit/server && docker compose exec -T db \\
      sh -c '\\''psql -X -q -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"'\\'''

A bare group runs every query of app/v2/sql/<group>.sql; group.query one.
measure.sh runs it (explain, cold and warm) for every group.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

from app.v2 import periods, queries  # noqa: E402

GROUPS = ("context", "sensors", "overview", "events", "networks", "aps", "spectrum", "settings")

# Looked up on the box. A psql variable per api parameter of the same name.
PRELUDE = r"""
\set ON_ERROR_STOP on
\pset pager off
SET track_io_timing = on;
SET application_name = 'perf-measure-v2';
SET jit = off;                       -- as the api's v2 connections (app/v2/deps.py)
SELECT 1 AS account_id \gset
SELECT coalesce(array_agg(s.id ORDER BY s.id), '{}')::text AS sensor_ids,
       coalesce(max(ss.last_capture_at), now()) AS latest,
       coalesce(max(ss.last_capture_at), now()) AS account_latest,
       coalesce(min(s.id), 0) AS sensor_id
FROM sensors s LEFT JOIN sensor_summary ss ON ss.sensor_id = s.id WHERE s.account_id = :account_id \gset
SELECT coalesce((SELECT upper(s.bssid::text) FROM ap_baselines b JOIN ap_summary s USING (sensor_id, device_key)
                 ORDER BY coalesce(b.n_obs, 0) + coalesce(b.n_floor, 0) DESC LIMIT 1), '') AS bssid \gset
SELECT coalesce((SELECT max(id) FROM detections), 0) AS event_id \gset
SELECT coalesce((SELECT id FROM aps WHERE upper(bssid::text) = :'bssid'), 0) AS ap_id,
       '{' || :'bssid' || '}' AS macs,
       :'latest'::timestamptz - interval '6 hours' AS ts, :'latest' AS ts_end \gset
"""

# Fixed values of the other parameters (the api's defaults or a typical request).
FIXED = {
    "limit": "51", "q": "", "status": "{}", "kinds": "{}", "managed": "false",
    "before_ts": "infinity", "before_id": str(2 ** 63 - 1), "after_ts": "-infinity", "after_id": "0",
    "oldest_first": "false",
}


def pg_array(values):
    return "{" + ",".join('"%s"' % v for v in values) + "}"


def psql_set(name, value):
    return "\\set %s '%s'" % (name, str(value).replace("'", "''"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="group or group.query")
    ap.add_argument("--range", default="30d", choices=periods.RANGES)
    ap.add_argument("--tz", default="Europe/Bratislava")
    ap.add_argument("--now", help="ISO time (default: now)")
    ap.add_argument("--mode", default="explain", choices=("explain", "json", "rows"))
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="override a parameter")
    args = ap.parse_args(argv)

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc))
    p = periods.period(args.range, args.tz, now)
    values = dict(FIXED)
    values.update({"from": periods.iso(p.start), "to": periods.iso(p.ends[-1]),
                   "starts": pg_array(periods.iso(t) for t in p.starts),
                   "ends": pg_array(periods.iso(t) for t in p.ends),
                   "now": periods.iso(now), "tz": args.tz})
    values.update(kv.partition("=")[::2] for kv in args.set)    # after the prelude: they win
    looked_up = {"account_id", "sensor_ids", "latest", "account_latest", "sensor_id", "bssid", "event_id",
                 "ap_id", "macs", "ts", "ts_end"}

    out = [PRELUDE] + [psql_set(k, v) for k, v in sorted(values.items())]
    if args.mode == "json":
        out += ["\\pset format unaligned", "\\pset tuples_only on"]
    for target in args.targets:
        group, _, name = target.partition(".")
        if group not in GROUPS:
            sys.exit("unknown group %s" % group)
        qs = queries.load(group)
        for qname, sql in qs.items():
            if name and qname != name:
                continue
            missing = [x for x in queries.params(sql) if x not in values and x not in looked_up]
            if missing:
                sys.exit("%s.%s: no value for %s" % (group, qname, ", ".join(missing)))
            body = queries.to_psql(sql)
            if args.mode == "json":
                out.append("\\echo == %s.%s\nSELECT coalesce(json_agg(q), '[]'::json) FROM (\n%s\n) q;"
                           % (group, qname, body))
            elif args.mode == "rows":
                out.append("\\echo == %s.%s\n%s;" % (group, qname, body))
            else:
                out.append("\\echo == %s.%s (range %s)\nEXPLAIN (ANALYZE, BUFFERS)\n%s;"
                           % (group, qname, args.range, body))
    print("\n".join(out))


if __name__ == "__main__":
    main()
