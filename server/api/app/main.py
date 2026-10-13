"""wifi-audit api (FastAPI).

Every route lives under /api: Caddy proxies /api/* to this container with the
path unchanged, everything else is the React app. The health endpoint and the
dashboard endpoints (/api/v2, v2/: the reads of docs/dashboard-v2/PLAN.md
section 7.3 and the token-gated writes of 7.5); the sensor ingest endpoints
are added here later. The first dashboard's endpoints (/api/overview, /aps,
/alerts, /findings, /detections) were removed in phase 5c.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from . import cache, db
from .v2 import routes as v2

log = logging.getLogger("api")

# Read responses that may be cached briefly: the seeded data is static and the
# box has one core. Health is always fresh. The endpoints on the schema-6
# dashboard tables set Cache-Control and an ETag themselves (cache.py).
UNCACHED = {"/api/health", "/api/docs", "/api/openapi.json"}


@asynccontextmanager
async def lifespan(_app):
    db.pool.open(wait=False)    # the DB may still be starting; connections are lazy
    try:
        yield
    finally:
        db.pool.close()


app = FastAPI(
    title="wifi-audit api",
    version="0.4.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)


@app.exception_handler(OperationalError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(_request, exc):
    log.warning("database unavailable: %s", exc)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


@app.middleware("http")
async def cache_control(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (request.method == "GET" and path.startswith("/api/") and path not in UNCACHED
            and response.status_code == 200):
        response.headers.setdefault("Cache-Control", cache.CACHE_CONTROL)
    return response


router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    """Liveness of the api and reachability of the database.

    200 when a connection can be obtained and schema_meta is readable (the
    schema version is returned so a missed schema.sql is visible here);
    503 otherwise, so the compose healthcheck and the frontend both see it.
    First drops the pool's dead idle connections (after a DB restart), so the
    next dashboard request does not wait for the pool's check backoff (db.py).
    """
    try:
        db.pool.check()
        with db.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except Exception as e:  # PoolTimeout, OperationalError, missing table, ...
        log.warning("health: database unavailable: %s", e)
        return JSONResponse({"status": "degraded", "database": "unavailable"},
                            status_code=503)
    return {"status": "ok", "database": "ok",
            "schema_version": row["value"] if row else None}


app.include_router(router)
app.include_router(v2.router)
