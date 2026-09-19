# wifi-audit server

Backend of the passive Wi-Fi monitoring system: it will receive
store-and-forward uploads from one or more `wifi-sensor` deployments, keep the
long-term history that the sensors' 14-day buffers discard, and host the
analysis and the dashboard. Stack (all on one small box): PostgreSQL, FastAPI,
React, Caddy.

## Status: Milestone 3, step 1 - storage + one-time seed

| Part | State |
|---|---|
| `schema.sql` | done - idempotent PostgreSQL schema: the collector's tables mirrored with a `sensor_id` on every row, plus `sensors`, `ap_baselines`, `detections`, `refresh_ap_baselines()` and the `ap_inventory` view. TimescaleDB-ready (`observations` keyed `(ts, sensor_id, device_key)`), not enabled. |
| `seed/` | done - `export_snapshot.py` (consistent copy of the Pi buffer) and `import_snapshot.py` (SQL + COPY stream piped into psql, idempotent merge). See `seed/README.md`. |
| API (FastAPI), dashboard (React) | next |
| live ingest, detection | later; see `CLAUDE.md` for the scope rules |

```
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f server/schema.sql
python3 server/seed/import_snapshot.py buffer-snapshot.db --sensor pi-fri | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
```

Record shape and semantics come from the sensor: `wifi-sensor/collector/store.py`
and `shape.py`; the mapping to PostgreSQL is documented at the top of
`schema.sql`. Privacy rule as on the sensor: no client IP addresses, no WPS
identity fields.
