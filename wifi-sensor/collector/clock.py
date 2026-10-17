"""The clock data of a poll (buffer v8; server/docs/live-upload.md 8.3).

The Pi has no RTC and no fake-hwclock: a boot without NTP runs on a stale
date (findings.md section 13). The collector does not stop for that - it
records with each poll what the uploader needs to correct the time later:
CLOCK_MONOTONIC next to the wall clock, the boot id, and whether chrony is
synchronised (leap status Normal and |system time offset| < 0.5 s - a large
offset being slewed is not synchronised).

Standard library only (tests/test_clock.py).
"""

import subprocess
import time

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
SYNC_MAX_OFFSET_S = 0.5


def boot_id(path=BOOT_ID_PATH):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def now():
    """(wall, mono): time.time() and CLOCK_MONOTONIC read back to back."""
    wall = time.time()
    mono = time.monotonic()
    return wall, mono


def chrony_state(run=subprocess.run):
    """(synced, offset_s) from `chronyc -c tracking`; (False, None) when chronyc
    is missing, fails or prints something unexpected."""
    try:
        out = run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if out.returncode != 0:
        return False, None
    fields = out.stdout.strip().split(",")
    if len(fields) < 14:
        return False, None
    try:
        offset = float(fields[4])
    except ValueError:
        return False, None
    synced = fields[-1].strip() == "Normal" and abs(offset) < SYNC_MAX_OFFSET_S
    return synced, offset
