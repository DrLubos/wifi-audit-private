# wifi-audit server

Backend of the passive Wi-Fi monitoring system: it will receive
store-and-forward uploads from one or more `wifi-sensor` deployments, keep the
long-term history that the sensors' 14-day buffers discard, and host the
analysis and the dashboard. Everything runs on one small box as a
docker-compose stack.

## Status: Milestone 3, step 1 - storage, seed, empty stack

| Part | State |
|---|---|
| `schema.sql` | done - idempotent PostgreSQL schema: the collector's tables mirrored with a `sensor_id` on every row, plus `sensors`, `ap_baselines`, `detections`, `refresh_ap_baselines()` and the `ap_inventory` view. TimescaleDB-ready (`observations` keyed `(ts, sensor_id, device_key)`), not enabled. |
| `seed/` | done - `export_snapshot.py` (consistent copy of the Pi buffer) and `import_snapshot.py` (SQL + COPY stream piped into psql, idempotent merge). See `seed/README.md`. |
| `docker-compose.yml`, `api/`, `frontend/` | done - the stack below; the api serves only `/api/health`, the React app only displays it |
| dashboard read endpoints + pages | next |
| live ingest, detection | later; see `CLAUDE.md` for the scope rules |

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
| `frontend` | `frontend/Dockerfile` (`node:22-alpine` build stage -> `caddy:2-alpine`) | Caddy serves `dist` and proxies `/api/*` to `api:8000`; the only container publishing ports. `frontend/Caddyfile` is bind-mounted. |

Credentials live only in the gitignored `.env` (template: `.env.example`).

### First bring-up (on the box)

```
cd server
cp .env.example .env && $EDITOR .env          # POSTGRES_*, SITE_ADDRESS

# once: the frontend build needs package-lock.json (no Node on the box or the Mac)
docker run --rm -v "$PWD/frontend:/app" -w /app node:22-alpine npm install --package-lock-only
git add frontend/package-lock.json            # commit it

docker compose build frontend                 # the node build is the memory peak - build one at a time
docker compose build api
docker compose up -d
docker compose ps                             # db healthy -> api healthy -> frontend running
curl -s http://localhost/api/health           # {"status":"ok","database":"ok","schema_version":"1"}
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
    -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/10-schema.sql        # re-apply the idempotent schema
docker compose exec frontend caddy reload --config /etc/caddy/Caddyfile     # after editing the Caddyfile
docker compose build api && docker compose up -d api                        # after an api change
docker compose logs -f api
```

`$POSTGRES_USER`/`$POSTGRES_DB` above are the values from `.env`
(`set -a; . ./.env; set +a` loads them into the shell). Seeding from a Pi
snapshot: `seed/README.md`.

### Local development without containers

`api/`: `DATABASE_URL=postgresql://... uvicorn app.main:app --reload` (needs a
reachable PostgreSQL). `frontend/`: `npm install && npm run dev` - Vite proxies
`/api` to `localhost:8000` (`vite.config.js`).

Record shape and semantics come from the sensor: `wifi-sensor/collector/store.py`
and `shape.py`; the mapping to PostgreSQL is documented at the top of
`schema.sql`. Privacy rule as on the sensor: no client IP addresses, no WPS
identity fields.
