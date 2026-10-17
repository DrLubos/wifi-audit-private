"""When the refresher works (docs/live-upload.md 6.4). Standard library only
(tests/test_ingest_logic.py).

- next_delay(): a run of d seconds is followed by the next no earlier than
  max(interval, 4 d) after its start - at most a fifth of a core on the
  e2-micro, however slow the refresh gets.
- baseline_due(): refresh_ap_baselines() once a night at 03:30 in the
  account's time zone.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

DUTY_FACTOR = 4
BASELINE_AT = (3, 30)


def next_delay(interval_s, last_duration_s, factor=DUTY_FACTOR):
    return max(interval_s, factor * last_duration_s) - last_duration_s


def baseline_due(now_utc, tz, last_date, at=BASELINE_AT):
    """(due, local date): due once per local day, from `at` local time on."""
    local = now_utc.astimezone(ZoneInfo(tz))
    day = local.date().isoformat()
    past = (local.hour, local.minute) >= at
    return (past and last_date != day), day


def utc_now():
    from datetime import timezone
    return datetime.now(timezone.utc)
