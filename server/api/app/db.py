"""PostgreSQL connection pool for the api.

DATABASE_URL comes from the environment (compose derives it from .env). The
pool is created closed and opened in the FastAPI lifespan, so importing this
module never touches the network. Plain SQL over psycopg 3; no ORM.
"""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# A connection broken by a DB restart (or a crash recovery) is dropped when
# check_connection finds it at checkout - but psycopg_pool then sleeps before
# trying the next one, 1 s, 2 s, 4 s (+-10 %) per dead connection
# (_getconn_with_check_loop): measured 2.5-3.9 s for the first request after
# a restart (2026-09-29, tests/perf MODE=coldstart), and 3 dead connections
# exceed `timeout` (a 503). So idle connections above min_size are closed after
# max_idle, and /api/health (the compose healthcheck, the header badge)
# runs pool.check(), which discards dead idle connections without sleeping.
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=4,
    timeout=5,                         # seconds to wait for a connection
    max_idle=120,                      # seconds before an extra idle connection is closed
    open=False,
    check=ConnectionPool.check_connection,   # drop connections broken by a DB restart
    kwargs={"connect_timeout": 5, "application_name": "wifi-audit-api",
            "row_factory": dict_row},   # rows come back as dicts
)
