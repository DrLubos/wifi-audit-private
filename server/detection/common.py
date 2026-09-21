"""Shared pieces of the detector CLIs: database connection, sensor resolution,
time-window parsing and a plain fixed-width table printer.

psycopg is imported lazily so the pure logic (and the unit tests) run without
it; the api image has it installed.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
POLL_INTERVAL_S = 30            # the collector's poll interval (wifi-sensor/collector)


# --- database -------------------------------------------------------------------

def connect(application_name="wifi-audit-detect"):
    """Connection from DATABASE_URL (same variable as the api), rows as dicts."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    import psycopg                                   # lazy: tests need no driver
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10,
                           application_name=application_name)


def get_sensor(conn, name=None):
    """sensors row by name; default: the first sensor (api/app/deps.py does the same)."""
    if name is None:
        row = conn.execute(
            "SELECT id, name, location, tz FROM sensors ORDER BY id LIMIT 1").fetchone()
    else:
        row = conn.execute(
            "SELECT id, name, location, tz FROM sensors WHERE name = %s", (name,)).fetchone()
    if row is None:
        sys.exit("unknown sensor %r" % name if name else "no sensor seeded yet")
    return row


# --- time -----------------------------------------------------------------------

def parse_when(text):
    """ISO 8601 date or date-time -> aware datetime. Naive input is UTC; a date
    means midnight UTC. Accepts 'Z' and a space instead of 'T'."""
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("not an ISO 8601 date/time: %r" % text) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'7d', '36h', '90m', '45s', '600' (seconds) -> seconds (float)."""
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ValueError("not a duration: %r (use e.g. 7d, 36h, 90m, 45s)" % text)
    return float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]


def window_from_args(from_text, to_text, default_span_s=86400):
    """(--from, --to) -> (from, to) aware datetimes. --to defaults to now, --from
    to --to minus default_span_s."""
    to = parse_when(to_text) if to_text else datetime.now(UTC)
    frm = parse_when(from_text) if from_text else to - timedelta(seconds=default_span_s)
    if frm >= to:
        raise ValueError("--from must be before --to")
    return frm, to


def iso(dt):
    """Aware datetime -> ISO 8601 UTC with millisecond precision and a Z."""
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def fmt_dur(seconds):
    """Compact duration: 0.01s, 45s, 3m10s, 2h05m, 1d03h."""
    if seconds is None:
        return "-"
    if seconds < 10:
        return "%.2fs" % seconds
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    if h < 24:
        return "%dh%02dm" % (h, m)
    d, h = divmod(h, 24)
    return "%dd%02dh" % (d, h)


# --- output ---------------------------------------------------------------------

def table(headers, rows, align_right=(), out=sys.stdout):
    """Plain fixed-width table; align_right = set of column indexes."""
    rows = [["-" if v is None else str(v) for v in r] for r in rows]
    if not rows:
        print("  (none)", file=out)
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))

    def line(r):
        cells = [(v.rjust(widths[i]) if i in align_right else v.ljust(widths[i]))
                 for i, v in enumerate(r)]
        return "  " + "  ".join(cells).rstrip()
    print(line(headers), file=out)
    print("  " + "  ".join("-" * w for w in widths), file=out)
    for r in rows:
        print(line(r), file=out)
