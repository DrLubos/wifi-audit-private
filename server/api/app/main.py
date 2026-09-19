"""wifi-audit api (FastAPI).

Every route lives under /api: Caddy proxies /api/* to this container with the
path unchanged, everything else is the React app. Milestone 3, step 1 exposes
only the health endpoint; the dashboard read endpoints and, later, the sensor
ingest endpoints are added here.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from . import db

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app):
    db.pool.open(wait=False)    # the DB may still be starting; connections are lazy
    try:
        yield
    finally:
        db.pool.close()


app = FastAPI(
    title="wifi-audit api",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)
router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    """Liveness of the api and reachability of the database.

    200 when a connection can be obtained and schema_meta is readable (the
    schema version is returned so a missed schema.sql is visible here);
    503 otherwise, so the compose healthcheck and the frontend both see it.
    """
    try:
        with db.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except Exception as e:  # PoolTimeout, OperationalError, missing table, ...
        log.warning("health: database unavailable: %s", e)
        return JSONResponse({"status": "degraded", "database": "unavailable"},
                            status_code=503)
    return {"status": "ok", "database": "ok",
            "schema_version": row[0] if row else None}


app.include_router(router)
