"""PostgreSQL connection pool for the api.

DATABASE_URL comes from the environment (compose derives it from .env). The
pool is created closed and opened in the FastAPI lifespan, so importing this
module never touches the network. Plain SQL over psycopg 3; no ORM.
"""

import os

from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=4,
    timeout=5,                         # seconds to wait for a connection
    open=False,
    check=ConnectionPool.check_connection,   # drop connections broken by a DB restart
    kwargs={"connect_timeout": 5, "application_name": "wifi-audit-api"},
)
