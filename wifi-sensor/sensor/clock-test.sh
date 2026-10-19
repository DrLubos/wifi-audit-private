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
# How (nothing touches the network, sshd, Tailscale or /etc/chrony):
# - arm: sets the mtime of /var/lib/systemd/timesync/clock - systemd starts the
#   boot clock there, the Pi has no RTC (findings.md section 13) - writes the
#   arm flag (clock-save.sh then leaves the file alone, also at shutdown) and a
#   chrony drop-in (below). It installs its own copy to $INSTALL_PATH, so the
#   root hooks never run a file the sensor user can edit.
# - boot-prep (ExecStartPre=-+, the first boot after arm): consumes the flag
#   (a second reboot is a normal boot) and writes $RUN_DIR/chrony.conf - the
#   system's chrony.conf WITHOUT its pool/server/peer lines (saved to
#   $RUN_DIR/sources) and without sourcedir - into /run, which is empty again
#   at the next boot.
# - chronyd (the drop-in's ExecStart, with the unit's own '!' prefix): starts
#   chronyd with that copy (-f) if it exists, else exactly as the unit does.
#   So the armed boot's chronyd has no time source at all. (Marking sources
#   `offline` did not hold, 2026-10-18: the pool's servers resolved after
#   `chronyc offline` came up online, and Debian's NetworkManager hook
#   20-chrony-onoffline runs `chronyc onoffline` on every network event,
#   which puts any source with a route online. A source that does not exist
#   cannot be put online.)
# - boot-hook (ExecStartPost=-+): starts the transient unit
#   wifi-sensor-clocktest-hold, which after N minutes (timed on the uptime)
#   adds the saved sources (`chronyc add pool ...`) and removes the copy, so a
#   later chrony restart is a normal one - if adding fails it restarts chrony,
#   now on the normal config; `makestep 1 3` then steps the clock at the first
#   update.
#   '-' = a failure never keeps chrony from starting; '+' = root, outside
#   chrony's sandbox (User=_chrony, ProtectSystem=strict, ProcSubset=pid -
#   inside it the hook could not write its state: 2026-10-18 21:03).
set -eu

STATE_DIR=${CLOCKTEST_STATE_DIR:-/var/lib/wifi-sensor-clocktest}
ARMED=$STATE_DIR/armed
LOG=$STATE_DIR/log
RUN_DIR=${CLOCKTEST_RUN_DIR:-/run/wifi-sensor-clocktest}
TEST_CONF=$RUN_DIR/chrony.conf
HOLD_MIN=$RUN_DIR/offline-min
HOLD_STARTED=$RUN_DIR/hold-started
SOURCES=$RUN_DIR/sources
CHRONY_CONF=${CLOCKTEST_CHRONY_CONF:-/etc/chrony/chrony.conf}
CHRONYD=${CLOCKTEST_CHRONYD:-/usr/sbin/chronyd}
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

# the system's chrony.conf without its sources (they go to $1, one
# `chronyc add` argument line each) and without extra source files
sourceless_conf() {
  awk -v src="$1" '
    $1 == "pool" || $1 == "server" || $1 == "peer" {
      line = $0; sub(/[ \t]+#.*$/, "", line); print line > src
      print "# clock-test: no time source this boot - " $0; n++; next }
    $1 == "sourcedir" { print "# clock-test: no source files this boot - " $0; next }
    { print }
    END { if (!n) exit 3 }' "$CHRONY_CONF"
}

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
  [ -e "$TEST_CONF" ] && say "this boot's chrony runs on $TEST_CONF (no time source)" || true
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
  sourceless_conf /dev/null >/dev/null || die "$CHRONY_CONF has no pool/server line to hold back"
  # sources from conf.d would not be held: refuse rather than half-hold
  if grep -qsE '^[[:space:]]*(pool|server|peer)[[:space:]]' "$(dirname "$CHRONY_CONF")"/conf.d/*.conf; then
    die "sources in $(dirname "$CHRONY_CONF")/conf.d - not supported by the hold"
  fi

  install -D -m 0755 "$0" "$INSTALL_PATH" 2>/dev/null || cp "$0" "$INSTALL_PATH"
  mkdir -p "$STATE_DIR" "$DROPIN_DIR"
  printf 'EPOCH=%s\nOFFLINE_MIN=%s\nARMED_AT=%s\n' "$target" "$offline" "$(date -u +%FT%TZ)" >"$ARMED"
  touch -d "@$target" "$CLOCK_FILE"
  cat >"$DROPIN" <<EOF
# clock-test.sh (wifi-sensor, docs/clock-validation.md) - removed by 'clock-test.sh disarm'.
# boot-prep and boot-hook do nothing unless $ARMED exists ('-': a failure never
# stops chrony; '+': root, outside chrony's sandbox). The ExecStart wrapper starts
# chronyd exactly as the unit does unless boot-prep wrote $TEST_CONF this boot.
[Service]
ExecStartPre=-+$INSTALL_PATH boot-prep
ExecStart=
ExecStart=!$INSTALL_PATH chronyd \$DAEMON_OPTS
ExecStartPost=-+$INSTALL_PATH boot-hook
EOF
  systemctl daemon-reload
  log "armed: next boot starts at $(iso "$target") ($(( (now - target) / 60 )) min behind now), NTP held off $offline min"
  say "nothing changes until 'sudo reboot'. Undo: sudo $INSTALL_PATH disarm"
}

cmd_boot_prep() {
  [ -e "$ARMED" ] || exit 0
  . "$ARMED"
  boot=$(boot_id)
  if ! mv "$ARMED" "$STATE_DIR/consumed-$boot" 2>/dev/null; then
    say "boot $boot: cannot consume the arm flag (no write access to $STATE_DIR) - normal boot, still armed"
    exit 0
  fi
  mkdir -p "$RUN_DIR"
  if ! sourceless_conf "$SOURCES" >"$TEST_CONF.tmp" || [ ! -s "$SOURCES" ]; then
    rm -f "$TEST_CONF.tmp" "$SOURCES"
    log "boot $boot: could not write the source-less chrony.conf - normal boot"
    exit 0
  fi
  chmod 0644 "$TEST_CONF.tmp"
  echo "$OFFLINE_MIN" >"$HOLD_MIN"
  mv "$TEST_CONF.tmp" "$TEST_CONF"
  log "boot $boot: prepared at $(date -u +%FT%TZ) (armed $(iso "$EPOCH")) - chrony starts without time sources"
}

cmd_chronyd() {
  if [ -s "$TEST_CONF" ]; then
    exec "$CHRONYD" "$@" -f "$TEST_CONF"
  fi
  exec "$CHRONYD" "$@"
}

cmd_boot_hook() {
  [ -s "$TEST_CONF" ] && [ -s "$HOLD_MIN" ] || exit 0
  [ -e "$HOLD_STARTED" ] && exit 0               # a chrony restart later in the same boot
  minutes=$(cat "$HOLD_MIN")
  boot=$(boot_id)
  touch "$HOLD_STARTED"
  state=$(leap || echo unknown)
  if [ "$state" = Normal ]; then
    log "boot $boot: chrony synchronised although it has no time source - normal boot; disarm"
    hold_over
    exit 0
  fi
  systemd-run --unit="$HOLD_UNIT" --description="clock test: NTP held off $minutes min" --collect \
    "$INSTALL_PATH" hold "$minutes" >/dev/null 2>&1 \
    || { log "boot $boot: could not start the hold - time sources back now"; hold_over; exit 0; }
  log "boot $boot: chrony started without time sources ($state), sources back in $minutes min"
}

hold_over() {
  rm -f "$TEST_CONF"                               # a later chrony restart is a normal one
  if [ -s "$SOURCES" ]; then
    ok=1
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      # word splitting on purpose: "pool 2.debian.pool.ntp.org iburst" -> chronyc add pool ... iburst
      # shellcheck disable=SC2086
      chronyc add $line >/dev/null 2>&1 || ok=0
    done <"$SOURCES"
    rm -f "$SOURCES"
    if [ $ok = 0 ]; then
      log "adding the time sources failed - restarting chrony (normal config)"
      systemctl restart chrony 2>/dev/null || true
      return 0
    fi
  fi
  chronyc online >/dev/null 2>&1 || true
}

cmd_hold() {
  minutes=${1:-}
  num "$minutes" || die "hold: minutes"
  end=$(( $(uptime_s) + minutes * 60 ))
  trap 'hold_over; log "hold stopped early - chrony online"; exit 0' TERM INT
  while [ "$(uptime_s)" -lt "$end" ]; do
    chronyc offline >/dev/null 2>&1 || true      # belt and braces: there is no source to take offline
    sleep "$HOLD_STEP_S" &
    wait $! || true
  done
  hold_over
  log "boot $(boot_id): hold over after $minutes min - time sources added, chrony online"
}

cmd_end() {
  need_root
  systemctl stop "$HOLD_UNIT" 2>/dev/null || true
  hold_over
  log "ended by hand - chrony online"
}

cmd_disarm() {
  need_root
  systemctl stop "$HOLD_UNIT" 2>/dev/null || true
  hold_over
  rm -f "$ARMED" "$DROPIN" "$HOLD_MIN"
  rmdir "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload
  # the boot clock file back to now (clock-save.sh saves only while synchronised)
  [ -x "$CLOCK_SAVE" ] && "$CLOCK_SAVE" || true
  log "disarmed (flag and drop-in removed; the log and consumed-* records stay in $STATE_DIR)"
}

case ${1:-} in
  status) cmd_status ;;
  arm) shift; cmd_arm "$@" ;;
  boot-prep) cmd_boot_prep ;;
  chronyd) shift; cmd_chronyd "$@" ;;
  boot-hook) cmd_boot_hook ;;
  hold) shift; cmd_hold "$@" ;;
  end) cmd_end ;;
  disarm) cmd_disarm ;;
  *) usage ;;
esac
