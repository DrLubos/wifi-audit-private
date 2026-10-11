"""The SQL of the v2 endpoints: named queries in app/v2/sql/<group>.sql.

Each query starts with a line `-- name: <name>` and uses psycopg's named
placeholders `%(param)s` with an explicit cast where the type matters
(`%(sensor_ids)s::int[]`); a literal % is written %%. A name starting with _
is a fragment, not a query: `{{_name}}` in a query of the same file is
replaced by it (one filter shared by a list and its counts). Keeping the SQL
in files lets tests/perf/ep_v2.py run exactly the api's statements under
EXPLAIN (to_psql()), so the perf scripts cannot drift from the code.

Standard library only (tests/test_queries.py).
"""

import os
import re

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")
_NAME = re.compile(r"^-- name: (_?[a-z][a-z0-9_]*)\s*$", re.M)
PARAM = re.compile(r"%\((\w+)\)s")
_INCLUDE = re.compile(r"\{\{(_[a-z0-9_]+)\}\}")


def parse(text):
    """{name: sql} of one file, fragments substituted and left out; the text
    before the first name line is the file's comment."""
    parts = _NAME.split(text)
    raw = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        if name in raw:
            raise ValueError("duplicate query name %s" % name)
        raw[name] = body.strip().rstrip(";")
    out = {}
    for name, sql in raw.items():
        if name.startswith("_"):
            continue

        def include(m):
            if m.group(1) not in raw:
                raise ValueError("%s: unknown fragment %s" % (name, m.group(1)))
            return raw[m.group(1)]
        out[name] = _INCLUDE.sub(include, sql)
    return out


def load(group):
    with open(os.path.join(SQL_DIR, group + ".sql"), encoding="utf-8") as f:
        return parse(f.read())


def params(sql):
    return sorted(set(PARAM.findall(sql)))


def to_psql(sql):
    """psycopg placeholders -> psql variables (:'param'), %% -> %."""
    return PARAM.sub(lambda m: ":'%s'" % m.group(1), sql).replace("%%", "%")
