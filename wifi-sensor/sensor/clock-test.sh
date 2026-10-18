#!/bin/sh
# clock-test.sh - make the next boot start with a wrong clock and no NTP for a
# while, to validate the live upload's timestamp correction
# (docs/clock-validation.md; server/docs/live-upload.md 8.3). The developer
# runs it (it is followed by a reboot); never part of a normal boot.
#
#   sudo clock-test.sh status
#   sudo clock-test.sh arm --hours-back 6 --offline-min 30
#   sudo clock-test.sh arm --epoch 2026-09-15T13:52:32Z --offline-min 30 --lan-only
#   sudo clock-test.sh end        # NTP back now (stops the hold)
#   sudo clock-test.sh disarm     # remove every trace
#
# How (nothing touches the network, sshd, Tailscale or chrony's config files):
# - arm: sets the mtime of /var/lib/systemd/timesync/clock - systemd starts the
#   boot clock there, the Pi has no RTC (findings.md section 13) - writes the
#   arm flag (clock-save.sh then leaves the file alone, also at shutdown) and a
#   chrony drop-in whose ExecStartPost runs `clock-test.sh boot-hook`. It
#   installs its own copy to $INSTALL_PATH, so the root hook never runs a file
#   the sensor user can edit.
# - boot-hook (first boot after arm): consumes the flag (a second reboot is a
#   normal boot), takes chrony's sources offline and starts the transient unit
#   wifi-sensor-clocktest-hold, which re-asserts `chronyc offline` every 30 s
#   (a source added later, e.g. by DHCP, would come up online) and runs
#   `chronyc online` after N minutes; `makestep 1 3` then steps the clock at
#   the first update. Every step is `ExecStartPost=-`: it can never keep chrony
#   from starting. If chrony synchronised before the hook (lost race), the
#   boot is a normal one - the log says so; disarm and arm again.
set -eu

STATE_DIR=${CLOCKTEST_STATE_DIR:-/var/lib/wifi-sensor-clocktest}
ARMED=$STATE_DIR/armed
LOG=$STATE_DIR/log
CLOCK_FILE=${CLOCKTEST_CLOCK_FILE:-/var/lib/systemd/timesync/clock}
DROPIN_DIR=${CLOCKTEST_DROPIN_DIR:-/etc/systemd/system/chrony.service.d}
DROPIN=$DROPIN_DIR/50-wifi-sensor-clocktest.conf
INSTALL_PATH=${CLOCKTEST_INSTALL_PATH:-/opt/wifi-sensor/sensor/clock-test.sh}
CLOCK_SAVE=${CLOCKTEST_CLOCK_SAVE:-/opt/wifi-sensor/sensor/clock-save.sh}
BOOT_ID_FILE=${CLOCKTEST_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}
UPTIME_FILE=${CLOCKTEST_UPTIME_FILE:-/proc/uptime}
HOLD_UNIT=wifi-sensor-clocktest-hold
HOLD_STEP_S=${CLOCKTEST_HOLD_STEP_S:-30}

say() { echo "clock-test: $*"; }
log() {
  say "$*"
  mkdir -p "$STATE_DIR" 2>/dev/null && echo "$(date -u +%FT%TZ) $*" >>"$LOG" || true
}
die() { echo "clock-test: ERROR $*" >&2; exit 1; }
need_root() { [ "${CLOCKTEST_NO_ROOT:-}" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo"; }

leap() { chronyc -c tracking 2>/dev/null | awk -F, '{print $NF}'; }
boot_id() { cat "$BOOT_ID_FILE" 2>/dev/null || echo unknown; }
uptime_s() { cut -d. -f1 "$UPTIME_FILE"; }     # monotonic: a clock step cannot move it
iso() { date -u -d "@$1" +%FT%TZ 2>/dev/null || date -u -r "$1" +%FT%TZ; }
num() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

cmd_status() {
  if [ -e "$ARMED" ]; then
    . "$ARMED"
    say "armed: next boot starts at $(iso "$EPOCH") (epoch $EPOCH), NTP held off $OFFLINE_MIN min (armed $ARMED_AT)"
  else
    say "not armed"
  fi
  [ -e "$DROPIN" ] && say "chrony drop-in present: $DROPIN" || say "chrony drop-in: none"
  if systemctl is-active --quiet "$HOLD_UNIT" 2>/dev/null; then
    say "NTP hold running ($HOLD_UNIT) - 'clock-test.sh end' ends it now"
  fi
  say "boot clock file: $(stat -c '%y' "$CLOCK_FILE" 2>/dev/null || echo missing)"
  say "chrony: $(leap || echo unknown)   boot: $(boot_id)"
  [ -e "$LOG" ] && { say "last log lines:"; tail -n 5 "$LOG"; } || true
}

cmd_arm() {
  need_root
  hours="" epoch="" offline="" lan=0
  while [ $# -gt 0 ]; do
    case $1 in
      --hours-back) hours=${2:-}; shift 2 ;;
      --epoch) epoch=${2:-}; shift 2 ;;
      --offline-min) offline=${2:-}; shift 2 ;;
      --lan-only) lan=1; shift ;;
      *) usage ;;
    esac
  done
  num "$offline" && [ "$offline" -ge 1 ] && [ "$offline" -le 1440 ] || die "--offline-min 1..1440 is required"
  now=$(date +%s)
  if [ -n "$hours" ] && [ -z "$epoch" ]; then
    num "$hours" && [ "$hours" -ge 1 ] && [ "$hours" -le 72 ] || die "--hours-back 1..72"
    target=$((now - hours * 3600))
  elif [ -n "$epoch" ] && [ -z "$hours" ]; then
    target=$(date -u -d "$epoch" +%s 2>/dev/null) || die "--epoch: not a date: $epoch"
    [ "$target" -lt "$now" ] || die "--epoch must be in the past"
    # weeks behind: TLS fails (Tailscale's control plane, the ingest server)
    if [ $((now - target)) -gt $((72 * 3600)) ] && [ "$lan" != 1 ]; then
      die "more than 72 h behind: ssh over Tailscale is gone until chrony steps - only from the LAN, add --lan-only"
    fi
  else
    die "give --hours-back H or --epoch ISO (one of them)"
  fi
  [ "$(leap)" = Normal ] || die "chrony is not synchronised now - arm from a synchronised clock"
  [ -e "$ARMED" ] && die "already armed (clock-test.sh status / disarm)"

  install -D -m 0755 "$0" "$INSTALL_PATH" 2>/dev/null || cp "$0" "$INSTALL_PATH"
  mkdir -p "$STATE_DIR" "$DROPIN_DIR"
  printf 'EPOCH=%s\nOFFLINE_MIN=%s\nARMED_AT=%s\n' "$target" "$offline" "$(date -u +%FT%TZ)" >"$ARMED"
  touch -d "@$target" "$CLOCK_FILE"
  cat >"$DROPIN" <<EOF
# clock-test.sh (wifi-sensor, docs/clock-validation.md) - removed by 'clock-test.sh disarm'.
# Does nothing unless $ARMED exists; '-' = a failure never stops chrony.
[Service]
ExecStartPost=-$INSTALL_PATH boot-hook
EOF
  systemctl daemon-reload
  log "armed: next boot starts at $(iso "$target") ($(( (now - target) / 60 )) min behind now), NTP held off $offline min"
  say "nothing changes until 'sudo reboot'. Undo: sudo $INSTALL_PATH disarm"
}

cmd_boot_hook() {
  [ -e "$ARMED" ] || exit 0
  . "$ARMED"
  boot=$(boot_id)
  mv "$ARMED" "$STATE_DIR/consumed-$boot"
  i=0
  until chronyc offline >/dev/null 2>&1; do
    i=$((i + 1))
    [ $i -ge 15 ] && { log "boot $boot: chronyc offline failed - normal boot"; exit 0; }
    sleep 1
  done
  state=$(leap || echo unknown)
  if [ "$state" = Normal ]; then
    log "boot $boot: chrony was already synchronised (lost race) - normal boot; disarm and arm again"
    chronyc online >/dev/null 2>&1 || true
    exit 0
  fi
  systemd-run --unit="$HOLD_UNIT" --description="clock test: NTP held off $OFFLINE_MIN min" --collect \
    "$INSTALL_PATH" hold "$OFFLINE_MIN" >/dev/null 2>&1 \
    || { log "boot $boot: could not start the hold - chrony back online"; chronyc online >/dev/null 2>&1 || true; exit 0; }
  log "boot $boot: started at $(date -u +%FT%TZ) (armed $(iso "$EPOCH")), chrony offline ($state), online in $OFFLINE_MIN min"
}

cmd_hold() {
  minutes=${1:-}
  num "$minutes" || die "hold: minutes"
  end=$(( $(uptime_s) + minutes * 60 ))
  trap 'chronyc online >/dev/null 2>&1; log "hold stopped early - chrony online"; exit 0' TERM INT
  while [ "$(uptime_s)" -lt "$end" ]; do
    chronyc offline >/dev/null 2>&1 || true
    sleep "$HOLD_STEP_S" &
    wait $! || true
  done
  chronyc online >/dev/null 2>&1 || true
  log "boot $(boot_id): hold over after $minutes min - chrony online"
}

cmd_end() {
  need_root
  systemctl stop "$HOLD_UNIT" 2>/dev/null || true
  chronyc online >/dev/null 2>&1 || true
  log "ended by hand - chrony online"
}

cmd_disarm() {
  need_root
  systemctl stop "$HOLD_UNIT" 2>/dev/null || true
  chronyc online >/dev/null 2>&1 || true
  rm -f "$ARMED" "$DROPIN"
  rmdir "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload
  # the boot clock file back to now (clock-save.sh saves only while synchronised)
  [ -x "$CLOCK_SAVE" ] && "$CLOCK_SAVE" || true
  log "disarmed (flag and drop-in removed; the log and consumed-* records stay in $STATE_DIR)"
}

case ${1:-} in
  status) cmd_status ;;
  arm) shift; cmd_arm "$@" ;;
  boot-hook) cmd_boot_hook ;;
  hold) shift; cmd_hold "$@" ;;
  end) cmd_end ;;
  disarm) cmd_disarm ;;
  *) usage ;;
esac
