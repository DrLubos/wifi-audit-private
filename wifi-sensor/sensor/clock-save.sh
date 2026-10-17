#!/bin/sh
# clock-save.sh - keep the next boot's starting clock current (live upload N11).
#
# The Pi has no RTC and no fake-hwclock: at boot systemd sets the clock to the
# modification time of /var/lib/systemd/timesync/clock, which nothing updates
# here (chrony, not timesyncd), so every boot started on 2026-09-15 - weeks
# behind, too far for TLS (findings.md section 13). This script sets that
# time to now, but only while chrony is synchronised (leap status Normal,
# |offset| < 0.5 s) - never a wrong time - and never while the clock
# validation test has armed a boot epoch on purpose (docs/clock-validation.md).
# A boot without NTP then starts at most an hour (+ the downtime) behind.
#
# Run as root by wifi-sensor-clock-save.timer (hourly) and by
# wifi-sensor-clock-save-shutdown.service (at shutdown). Changes nothing else:
# no clock is set, chrony's configuration is not touched.
set -eu
FILE=/var/lib/systemd/timesync/clock
ARMED=/var/lib/wifi-sensor-clocktest/armed

if [ -e "$ARMED" ]; then
  echo "clock-save: clock test armed - not saving"
  exit 0
fi
if ! out=$(chronyc -c tracking 2>/dev/null); then
  echo "clock-save: chronyc failed - not saving"
  exit 0
fi
leap=${out##*,}
offset=$(printf '%s\n' "$out" | cut -d, -f5)
if [ "$leap" != Normal ]; then
  echo "clock-save: clock not synchronised ($leap) - not saving"
  exit 0
fi
if ! awk -v o="$offset" 'BEGIN { exit !(o + 0 < 0.5 && o + 0 > -0.5) }'; then
  echo "clock-save: chrony offset $offset s - not saving"
  exit 0
fi
install -d -m 0755 "$(dirname "$FILE")"
touch "$FILE"
echo "clock-save: next boot starts at $(date -u +%FT%TZ) or later"
