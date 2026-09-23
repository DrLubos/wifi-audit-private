#!/usr/bin/env bash
#
# kismet-prestart.sh - bring the capture adapter into a known state before
# Kismet starts. Runs as root from kismet.service (ExecStartPre=+...), installed
# by install_sensor.sh. Touches ONLY the configured capture adapter.
#
# Why: after a cold boot the USB adapter appears late (it first enumerates as a
# USB storage device and mode-switches to a NIC ~15 s later), chrony steps the
# clock by days on a Pi without an RTC, and a capture helper that crashed during
# that turbulence leaves a half-configured monitor VIF behind that the next
# Kismet start adopts as "ready" - and then never receives a single frame.
#
# Environment (set by the unit drop-in):
#   CAPTURE_IFACE            the capture adapter, e.g. wlan1            (required)
#   PRESTART_IFACE_TIMEOUT   seconds to wait for the adapter to appear  (default 90)
#   PRESTART_TIME_TIMEOUT    seconds to wait for chrony to be in sync   (default 60)
#   KISMET_LOG_*             passed through to kismet-log-retention.sh (step 0)
#
# Never blocks Kismet forever: every wait is bounded and a timeout only logs a
# warning - the sensor must still capture when there is no uplink for NTP.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

IFACE=${CAPTURE_IFACE:-}
IFACE_TIMEOUT=${PRESTART_IFACE_TIMEOUT:-90}
TIME_TIMEOUT=${PRESTART_TIME_TIMEOUT:-60}
MON="${IFACE}mon"

log()  { echo "kismet-prestart: $*"; }
warn() { echo "kismet-prestart: WARNING: $*" >&2; }

# 0. Log retention before Kismet opens a new log. Runs on every start so a
#    crash loop cannot pile up empty kismetdb files (69 of them on 2026-09-22).
RETENTION="$(dirname "$0")/kismet-log-retention.sh"
if [[ -x $RETENTION ]]; then
  "$RETENTION" || warn "log retention failed (exit $?) - starting Kismet anyway"
fi

if [[ -z $IFACE ]]; then
  warn "CAPTURE_IFACE is not set - nothing to prepare"
  exit 0
fi

# Safety: never touch the interface that carries the uplink / SSH session.
if ip -o route show default 2>/dev/null | grep -qw "dev $IFACE"; then
  warn "$IFACE carries the default route - refusing to touch it"
  exit 0
fi

# 1. Wait for the adapter (its cfg80211 phy) to exist.
waited=0
until [[ -e /sys/class/net/$IFACE/phy80211 ]]; do
  if (( waited >= IFACE_TIMEOUT )); then
    warn "$IFACE did not appear within ${IFACE_TIMEOUT}s - starting Kismet anyway (it retries the source)"
    break
  fi
  sleep 1; (( waited += 1 ))
done
if [[ -e /sys/class/net/$IFACE/phy80211 ]]; then
  phy=$(<"/sys/class/net/$IFACE/phy80211/name")
  drv=$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver" 2>/dev/null)" 2>/dev/null)
  log "$IFACE present after ${waited}s (phy=$phy driver=${drv:-?})"
else
  phy=""
fi

# 2. Bounded wait for the clock. chronyc waitsync <tries> <max-offset> <max-skew> <interval>
if command -v chronyc >/dev/null 2>&1; then
  tries=$(( TIME_TIMEOUT / 2 )); (( tries < 1 )) && tries=1
  if chronyc waitsync "$tries" 1.0 0 2 >/dev/null 2>&1; then
    log "clock synchronised ($(date -u '+%Y-%m-%d %H:%M:%S UTC'))"
  else
    warn "clock not synchronised after ${TIME_TIMEOUT}s - continuing (timestamps may be corrected later)"
  fi
fi

[[ -n $phy ]] || exit 0

# 3. Radio unblocked. Only this phy - never 'rfkill unblock all'.
if command -v rfkill >/dev/null 2>&1; then
  id=$(rfkill -rn -o ID,DEVICE 2>/dev/null | awk -v p="$phy" '$2 == p {print $1; exit}')
  if [[ -n $id ]]; then rfkill unblock "$id" && log "rfkill: $phy unblocked"; fi
fi

# 4. Remove a stale monitor VIF from a previous run so Kismet configures a
#    fresh one (mode, channel control) instead of adopting a broken one.
if [[ -e /sys/class/net/$MON ]]; then
  if iw dev "$MON" del 2>/dev/null; then
    log "removed stale monitor interface $MON"
  else
    warn "could not remove $MON - Kismet will reuse it"
  fi
fi

# 5. Adapter power saving off (may be unsupported by the driver - not fatal).
if iw dev "$IFACE" set power_save off 2>/dev/null; then
  log "power save: off"
fi
exit 0
