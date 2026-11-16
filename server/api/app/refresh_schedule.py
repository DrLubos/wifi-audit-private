"""When the refresher works (docs/live-upload.md 6.4). Standard library only
(tests/test_ingest_logic.py).

- next_delay(): a run of d seconds is followed by the next no earlier than
  max(interval, 4 d) after its start - at most a fifth of a core on the
  e2-micro, however slow the refresh gets.
- baseline_due(): refresh_ap_baselines() once a night at 03:30 in the
  account's time zone.
- detectors_due(): the batch detectors once a night between 04:00 and 06:00
  local time (after the baselines; a refresher started later that day does
  not repeat them - the next night's 48-hour window covers the day).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

DUTY_FACTOR = 4
BASELINE_AT = (3, 30)
DETECT_FROM, DETECT_UNTIL = (4, 0), (6, 0)


def next_delay(interval_s, last_duration_s, factor=DUTY_FACTOR):
    return max(interval_s, factor * last_duration_s) - last_duration_s


def baseline_due(now_utc, tz, last_date, at=BASELINE_AT):
    """(due, local date): due once per local day, from `at` local time on."""
    local = now_utc.astimezone(ZoneInfo(tz))
    day = local.date().isoformat()
    past = (local.hour, local.minute) >= at
    return (past and last_date != day), day


def detectors_due(now_utc, tz, last_date, start=DETECT_FROM, until=DETECT_UNTIL):
    """(due, local date): once per local day, only inside [start, until) local time."""
    local = now_utc.astimezone(ZoneInfo(tz))
    day = local.date().isoformat()
    inside = start <= (local.hour, local.minute) < until
    return (inside and last_date != day), day


def local_day(ts, tz):
    """The local date (ISO) of a timestamp, None for None."""
    return None if ts is None else ts.astimezone(ZoneInfo(tz)).date().isoformat()


def utc_now():
    from datetime import timezone
    return datetime.now(timezone.utc)
