"""Periods and buckets of the v2 period endpoints (PLAN.md 7.2, SPEC section 3).

  24h  24 hourly buckets from the current hour - 23
  7d   28 x 6 h aligned to local 00/06/12/18, the last one containing now
  30d  30 local calendar days, today partial

Boundaries are computed in the account's local wall time and converted to
UTC, so the buckets around a DST change are 5/7 hours (7d) or 23/25 hours
(30d) long. accounts.tz must have whole-hour offsets: every edge then falls on
a UTC hour and SQL joins the hourly rows with hour >= start AND hour < end.
The last bucket ends in the future; `now` is the end of the period.

Standard library only (tests/test_periods.py).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

RANGES = ("24h", "7d", "30d")
BUCKET = {"24h": "1h", "7d": "6h", "30d": "1d"}
TITLE = {"24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days"}

UTC = timezone.utc


@dataclass(frozen=True)
class Period:
    range: str
    tz: str
    bucket: str                 # '1h' | '6h' | '1d'
    starts: tuple               # bucket starts, UTC datetimes
    ends: tuple                 # bucket ends (exclusive); the last one is after now
    now: datetime

    @property
    def start(self):
        return self.starts[0]

    @property
    def epoch(self):
        """Unix seconds of the current bucket's start: part of the ETag, so a
        304 never keeps the bucket boundaries of the previous hour/window/day."""
        return int(self.starts[-1].timestamp())

    def seconds(self, i):
        """Length of bucket i up to now (the current bucket is partial)."""
        return (min(self.ends[i], self.now) - self.starts[i]).total_seconds()

    def label(self):
        local = ZoneInfo(self.tz)
        return "%s: %s – %s" % (TITLE.get(self.range, "last " + self.range), fmt_local(self.start, local),
                                fmt_local(self.now, local))

    def envelope(self, sensor_filter):
        """The common head of every period response (without buckets)."""
        return {
            "filters": {"sensor": sensor_filter, "range": self.range},
            "period": {"from": iso(self.start), "to": iso(self.now), "bucket": self.bucket,
                       "tz": self.tz, "label": self.label()},
        }


def fmt_local(dt, local):
    d = dt.astimezone(local)
    return "%d %s" % (d.day, d.strftime("%b %H:%M"))


def iso(dt):
    """UTC ISO 8601 with a Z, seconds precision (the v2 timestamp format)."""
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_utc(naive_local, local):
    utc = naive_local.replace(tzinfo=local).astimezone(UTC)
    if utc.minute or utc.second or utc.microsecond:
        raise ValueError("time zone %s: bucket edge %s is not on a UTC hour" % (local.key, naive_local))
    return utc


def period(range_, tz, now):
    """The buckets of `range_` in time zone `tz` at the instant `now` (aware)."""
    if range_ not in RANGES:
        raise ValueError("range must be one of %s" % ", ".join(RANGES))
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = ZoneInfo(tz)
    now = now.astimezone(UTC)
    wall = now.astimezone(local).replace(tzinfo=None)     # naive local wall time
    if range_ == "24h":
        # whole-hour offsets: the local hour starts at the UTC hour
        last = now.replace(minute=0, second=0, microsecond=0)
        starts = [last - timedelta(hours=23 - i) for i in range(24)]
        ends = starts[1:] + [last + timedelta(hours=1)]
    else:
        if range_ == "7d":
            step, n = timedelta(hours=6), 28
            last = wall.replace(hour=wall.hour - wall.hour % 6, minute=0, second=0, microsecond=0)
        else:
            step, n = timedelta(days=1), 30
            last = wall.replace(hour=0, minute=0, second=0, microsecond=0)
        edges = [_to_utc(last - step * (n - 1 - i), local) for i in range(n + 1)]
        starts, ends = edges[:-1], edges[1:]
    return Period(range_, tz, BUCKET[range_], tuple(starts), tuple(ends), now)


def local_days(tz, now, days):
    """The last `days` local calendar days (today partial): [(date, start, end)] in UTC."""
    local = ZoneInfo(tz)
    today = now.astimezone(local).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        out.append((d.date(), _to_utc(d, local), _to_utc(d + timedelta(days=1), local)))
    return out


def days_window(tz, now, days):
    """A period of `days` local calendar days ending today (partial), one
    bucket per day - the AP page's 4-week clients heatmap."""
    local = ZoneInfo(tz)
    now = now.astimezone(UTC)
    today = now.astimezone(local).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    edges = [_to_utc(today - timedelta(days=days - 1 - i), local) for i in range(days + 1)]
    return Period("%dd" % days, tz, "1d", tuple(edges[:-1]), tuple(edges[1:]), now)
