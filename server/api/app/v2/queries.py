"""The SQL of the v2 endpoints: named queries in app/v2/sql/<group>.sql.

Each query starts with a line `-- name: <name>` and uses psycopg's named
placeholders `%(param)s` with an explicit cast where the type matters
(`%(sensor_ids)s::int[]`); a literal % is written %%. Keeping the SQL in files
lets tests/perf/ep_v2.py run exactly the api's statements under EXPLAIN
(to_psql()), so the perf scripts cannot drift from the code.

Standard library only (tests/test_queries.py).
"""

import os
import re

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")
_NAME = re.compile(r"^-- name: ([a-z][a-z0-9_]*)\s*$", re.M)
PARAM = re.compile(r"%\((\w+)\)s")


def parse(text):
    """{name: sql} of one file; the text before the first name line is the file's comment."""
    parts = _NAME.split(text)
    out = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        if name in out:
            raise ValueError("duplicate query name %s" % name)
        out[name] = body.strip().rstrip(";")
    return out


def load(group):
    with open(os.path.join(SQL_DIR, group + ".sql"), encoding="utf-8") as f:
        return parse(f.read())


def params(sql):
    return sorted(set(PARAM.findall(sql)))


def to_psql(sql):
    """psycopg placeholders -> psql variables (:'param'), %% -> %."""
    return PARAM.sub(lambda m: ":'%s'" % m.group(1), sql).replace("%%", "%")
