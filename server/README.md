# wifi-audit server

Backend of the passive Wi-Fi monitoring system: it will receive
store-and-forward uploads from one or more `wifi-sensor` deployments, keep the
long-term history that the sensors' 14-day buffers discard, and host the
analysis and the dashboard. Everything runs on one small box as a
docker-compose stack.

## Status: Milestone 3, step 1 - storage, seed, dashboard on real data

| Part | State |
|---|---|
| `schema.sql` | done - idempotent PostgreSQL schema: the collector's tables mirrored with a `sensor_id` on every row, plus `sensors`, `ap_baselines`, `detections`, `refresh_ap_baselines()` and the `ap_inventory` view. Schema 6: the dashboard read tables `ap_rssi_hourly`, `sensor_hourly`, `ap_summary`, `sensor_summary`, built by `refresh_rollups(sensor_id, from)` (the importer calls it), and `ap_config_changes` materialized. Schema 7 (`ops/migrate_schema7.sh`): `observations` slim and AP-only, keyed `(sensor_id, device_key, ts)` INCLUDE (rssi, rssi_floor) + BRIN on ts, `devices.cur_*`, `client_bssids`, `ap_baselines.band`. Schema 8 (`ops/migrate_schema8.sh`): `ap_load_hourly` (AP load per hour and advertised channel, from collector buffer v7 and the backfilled per-poll columns of buffers <= v5). Schema 9 (`ops/migrate_schema9.sh`): the dashboard v2 model - account, settings, networks, account-level APs, `detections` as the event table with operator statuses, security levels, sensor periods (`docs/dashboard-v2/`). Schema 10 (`ops/migrate_schema10.sh`): `devices.ever_ap` - an AP stays an AP when Kismet re-types it; Kismet events attributed to the account AP, old duplicates merged (`ops/merge_duplicate_events.sql`). Schemas 11-13 (`ops/migrate_schema13.sh`): a fixed reason with every event label (three outcomes), the adapter record per sensor, and the hand-entered `hardware_change` history entry that is a data boundary (RSSI baselines stay inside one hardware period; no antenna catalog). TimescaleDB-ready (ts in the key), not enabled. |
| `seed/` | done - `export_snapshot.py` (consistent copy of the Pi buffer) and `import_snapshot.py` (SQL + COPY stream piped into psql, idempotent merge). See `seed/README.md`. |
| `docker-compose.yml`, `api/`, `frontend/` | done - the stack below |
| dashboard | done - `/api/v2` (`api/app/v2/`, `docs/dashboard-v2/PLAN.md` section 7): the reads of Overview, Events, Networks, Access points (and the AP page with its RSSI, load and utilization), Spectrum, Sensors and Settings, all from the read tables (never raw observations, ETag from `sensor_summary.refreshed_at` and the account's write version, no client MAC in any response) and the writes behind the write token (event labels with a reason, the label export for RQ2, AP classification and note, networks, classification and detector settings, sensors, hardware changes; each audited); React pages for all of them. The first dashboard (`/api/overview`, `/aps`, `/alerts`, `/findings`, `/detections` and its pages) was removed in phase 5c. Measurements: `docs/performance.md` |
| dashboard v2 frontend | done - `docs/dashboard-v2/PLAN.md` section 10: the shell (sidebar, top bar, filters in the URL) and the pages Overview `/`, Events `/events`, Networks `/networks`, Spectrum `/spectrum`, Access points `/aps` and `/aps/<bssid>`, Sensors `/sensors`, Settings `/settings`; writes behind the write token (unlocked once per tab). The first dashboard's pages were removed in phase 5c; `/detections` redirects to `/events`, `/legacy` to `/`, and `/aps/<Kismet device key>` links to the BSSID page. Logic tests (`frontend/src/**/*.test.js`) run with `node --test` in the image's build stage |
| dashboard v2 API (read) | done, phase 3 of `docs/dashboard-v2/PLAN.md` - `/api/v2/...` (sensors, overview status and period, events feed with keyset cursors and event detail, networks + timeseries + clients heatmap, access points list / page / RSSI / load, spectrum + utilization, sensor coverage and outages, settings reads); period buckets in the account's time zone, weak ETag with the bucket epoch, no client MAC in any response (redacted, checked by `tests/perf/v2_smoke.py`). Code in `api/app/v2/`, its SQL in `api/app/v2/sql/*.sql` (run as is by `tests/perf/ep_v2.py`). The `/api/...` endpoints above stay until the frontend switch |
| `detection/` | prototype - batch detectors run on demand over a time window, writing only `detections`; one detector so far, `deauth-flood` (Kismet DEAUTHFLOOD alerts + burst events of the polled `client_disconnects` counter, per-AP baseline, idempotent re-runs). See `detection/README.md` |
| live ingest | being built (`docs/live-upload.md`, steps 1-6): schema 14 and `ingest_merge()` - the one merge of the snapshot import and the live ingest - (step 1); `/api/ingest/v1/batch` and `/heartbeat` with per-sensor tokens (`ops/sensor_token.sh`), stall events, the `refresher` service (step 2); the Pi's collector v8 and uploader (steps 3-4) |

## Stack

```
                 80/443
 internet ──────────────► frontend (Caddy 2, serves the built React app)
                              │  /api/*                     network: frontend
                              ▼
                            api (FastAPI, uvicorn :8000)    networks: frontend + backend
                              │  DATABASE_URL
                              ▼
                            db (postgres:17, volume db-data) network: backend only
```

| Service | Image | Notes |
|---|---|---|
| `db` | `postgres:17` | `schema.sql` mounted read-only into `/docker-entrypoint-initdb.d/`, runs on the first init of the `db-data` volume. No published port. `shared_buffers=128MB` for the 1 GB box. |
| `api` | `api/Dockerfile` (`python:3.13-slim`, non-root) | `DATABASE_URL` from the environment (compose derives it from `.env`). psycopg 3 pool, plain SQL. Routes carry the `/api` prefix themselves; docs at `/api/docs`. |
| `refresher` | the api image, `python -m app.refresher` | The dashboard rollups after live ingest (`refresh_rollups()` of the sensors the ingest marked dirty, contact periods, nightly baselines); waits max(`REFRESH_INTERVAL_S`, 4 x its last round). `docs/live-upload.md` 6.4 |
| `frontend` | `frontend/Dockerfile` (`node:22-alpine` build stage -> `caddy:2-alpine`) | Caddy serves `dist` and proxies `/api/*` to `api:8000` (`/api/ingest/*` with a 2 MB body limit); the only container publishing ports. `frontend/Caddyfile` is bind-mounted. |

Credentials live only in the gitignored `.env` (template: `.env.example`).

### First bring-up (on the box)

```
cd server
cp .env.example .env && $EDITOR .env          # POSTGRES_*, SITE_ADDRESS, WRITE_TOKEN (dashboard writes)

# once: the frontend build needs package-lock.json (no Node on the box or the Mac)
docker run --rm -v "$PWD/frontend:/app" -w /app node:22-alpine npm install --package-lock-only
git add frontend/package-lock.json            # commit it

docker compose build frontend                 # the node build is the memory peak - build one at a time
docker compose build api
docker compose up -d
docker compose ps                             # db healthy -> api healthy -> frontend running
curl -s http://localhost/api/health           # {"status":"ok","database":"ok","schema_version":"9"}
```

`SITE_ADDRESS=:80` serves plain HTTP; a domain name switches Caddy to automatic
HTTPS (DNS must point at the box, 80/443 reachable). The first `up` initialises
the volume and runs `10-schema.sql` (`docker compose logs db`). If that very
first init fails, `docker compose down -v` discards the empty volume - never
after a seed.

### Everyday commands

```
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"     # psql (socket auth inside the container)
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 < schema.sql                                         # re-apply the idempotent schema (stdin, see note)
docker compose exec frontend caddy reload --config /etc/caddy/Caddyfile     # after editing the Caddyfile
docker compose build api && docker compose up -d api                        # after an api change
docker compose logs -f api
docker compose run --rm detect deauth-flood --from 2026-09-15 --to 2026-09-21 --dry-run   # batch detector, see detection/README.md
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c 'SELECT * FROM refresh_rollups(1)'                                    # rebuild the dashboard tables of sensor 1 (whole history)
for t in tests/sql/*_test.sql; do
  docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$t"
done                                                                        # SQL fixture tests (scratch schema, rolled back)
```

SQL fixture tests in `tests/sql/` run the deployed definitions (the
`ap_config_changes` view, `refresh_rollups()`) against fixtures in empty copies
of the tables in a scratch schema, inside a transaction that is rolled back (no
real row is read or written); each prints `PASS` or raises. Run them after
applying `schema.sql`, as `$POSTGRES_USER` (they create the scratch schema).
Unit tests without a database: `python3 -m unittest discover -s api/tests`
(in the api image, which does not contain them: `docker compose run --rm
--no-deps -v "$PWD/api/tests:/app/tests:ro" api python -m unittest discover -s
tests`),
`-s seed/tests`, `-s detection/tests`, `-s evaluation/tests`, and
`-s tests -p 'test_*.py'` (schema/migration files) (from `server/`). Read-only
check of the schema-7 migration on the real data: `tests/sql/check_schema7.sql`
(also as `claude_ro`).

The dashboard reads the schema-6 tables, which only `refresh_rollups()` writes:
the importer calls it after every load; after changing `ap_baselines` by hand
(e.g. `trusted`) or anything else they summarise, run it again (idempotent; with
a `from` timestamp only the hours from there on are recomputed).

`$POSTGRES_USER`/`$POSTGRES_DB` above are the values from `.env`
(`set -a; . ./.env; set +a` loads them into the shell). Seeding from a Pi
snapshot: `seed/README.md`.

Re-apply the schema from the host file (stdin), not from
`/docker-entrypoint-initdb.d/10-schema.sql` inside the container: a single-file
bind mount pins the file's inode, and `git pull` writes a new file, so the
container keeps seeing the old contents until `db` is restarted. The mount is
only there for the first init.

### Backup and restore

The seeded dataset lives in the `db-data` volume only, so back it up:

```
./backup.sh                                   # -> backups/<db>-<UTC stamp>.dump (custom format, validated, gitignored)
scp google:~/wifi-audit/server/backups/<file> ~/somewhere-safe/    # copy it OFF the box
```

`backup.sh` reads `.env`, runs `pg_dump -Fc` inside the `db` container (read-only
on the database), checks the archive with `pg_restore --list`, prints size and
sha256, and keeps the newest `KEEP` dumps (default 7). A cron line on the box, if
wanted: `17 3 * * * cd ~/wifi-audit/server && ./backup.sh >> backups/backup.log 2>&1`.

Restore into the running stack (replaces the current contents; `--clean` drops
and recreates every object in the dump, including the function and the view):

```
set -a; . ./.env; set +a
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --clean --if-exists --no-owner --exit-on-error < backups/<file>.dump
docker compose restart api                    # drop pooled connections that saw the old objects
```

Restore on a fresh box: `docker compose up -d db` (the first init creates the
empty schema), then the same `pg_restore` line, then `up -d` the rest. A dump
taken with an older `schema.sql` restores the objects *as dumped*; re-apply the
current `schema.sql` afterwards if the schema moved on (it is idempotent).

### Local development without containers

`api/`: `DATABASE_URL=postgresql://... uvicorn app.main:app --reload` (needs a
reachable PostgreSQL). `frontend/`: `npm install && npm run dev` - Vite proxies
`/api` to `localhost:8000` (`vite.config.js`).

Record shape and semantics come from the sensor: `wifi-sensor/collector/store.py`
and `shape.py`; the mapping to PostgreSQL is documented at the top of
`schema.sql`. Privacy rule as on the sensor: no client IP addresses, no WPS
identity fields.
