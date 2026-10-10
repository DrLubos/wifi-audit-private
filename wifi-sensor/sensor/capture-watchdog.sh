#!/usr/bin/env bash
#
# capture-watchdog.sh - make sure Kismet is actually receiving frames.
#
# Run every few minutes by wifi-sensor-capture-watchdog.timer (as root,
# installed by install_sensor.sh). Kismet reports a datasource as "running"
# even when the radio delivers nothing (seen after a cold boot: helper crashed
# during bring-up, monitor VIF adopted half-configured, 0 packets for hours),
# so the check is Kismet's own packet counter, read from its REST API.
#
# Escalation, one step per consecutive silent check (with a 2-minute timer,
# STRIKES_RESTART=2 means "no frames for ~4 minutes"):
#   STRIKES_RESTART  -> systemctl restart kismet         (fresh helper + monitor VIF)
#   STRIKES_DRIVER   -> adapter on the bus: reload its kernel module, restart Kismet
#                       adapter GONE from the bus: power-cycle its hub port
#   STRIKES_USB      -> adapter on the bus, first time: software re-plug
#                       (authorized 0/1), restart Kismet
#                       adapter gone, or a re-plug already failed in an earlier
#                       round: power-cycle its hub port
#                       ... and start the cycle again
# A power-cycle happens at most once per PORT_CYCLE_MIN_INTERVAL; inside that
# interval the step falls back to restarting Kismet.
#
# The hub port: on 2026-10-06 the adapter stopped enumerating altogether (hub
# port state "not attached") and nothing short of cutting the port's power
# brought it back - the re-plug needs a device on the bus. The port is learned
# on every run while the interface is visible (wlanX -> USB device -> its
# 'port' link) and kept across reboots, so moving the adapter to another port
# while everything runs is picked up within one timer period. Before any
# power-cycle the port is checked (port_safe): never a root-hub port, never a
# port that carries any other network interface (the Ethernet uplink sits on
# the same hub on a Pi 3B+), never a port whose current occupant is a hub or a
# device other than the remembered adapter.
#
# Only the configured capture adapter is ever touched.
#
# Environment (set by the unit):
#   CAPTURE_IFACE          capture adapter, e.g. wlan1                       (required)
#   KISMET_URL             REST base URL           (default http://127.0.0.1:2501)
#   KISMET_AUTH_FILE       kismet_httpd.conf with httpd_username/httpd_password
#   WATCHDOG_STATE_DIR     last counter, strikes, re-plug flag (default /run/wifi-sensor)
#   WATCHDOG_PERSIST_DIR   adapter's hub port and last power-cycle time, kept
#                          across reboots (default /var/lib/wifi-sensor-watchdog)
#   STRIKES_RESTART / STRIKES_DRIVER / STRIKES_USB   (defaults 2 / 4 / 6)
#   PORT_CYCLE_MIN_INTERVAL  seconds between two port power-cycles (default 600)
#   PORT_CYCLE_OFF_SECONDS   how long the port stays off (default 5)
#   WATCHDOG_SYSFS         sysfs root (default /sys; tests use a fake tree)
#
# The script can be sourced (tests): only the functions are defined then.
set -u

IFACE=${CAPTURE_IFACE:-}
URL=${KISMET_URL:-http://127.0.0.1:2501}
AUTH_FILE=${KISMET_AUTH_FILE:-}
STATE_DIR=${WATCHDOG_STATE_DIR:-/run/wifi-sensor}
PERSIST_DIR=${WATCHDOG_PERSIST_DIR:-/var/lib/wifi-sensor-watchdog}
S_RESTART=${STRIKES_RESTART:-2}
S_DRIVER=${STRIKES_DRIVER:-4}
S_USB=${STRIKES_USB:-6}
PC_MIN_INTERVAL=${PORT_CYCLE_MIN_INTERVAL:-600}
PC_OFF=${PORT_CYCLE_OFF_SECONDS:-5}
SYSFS=${WATCHDOG_SYSFS:-/sys}

log()  { echo "capture-watchdog: $*"; }
warn() { echo "capture-watchdog: WARNING: $*" >&2; }

read_state() { [[ -s $1 ]] && cat "$1" || echo "$2"; }

# --------------------------------------------------------------- USB port --

# usb_port_of_iface IFACE - the hub port the interface's USB device is plugged
# into (resolved sysfs path of its 'port' link); fails when the interface, its
# USB device or the link is missing.
usb_port_of_iface() {
  local intf usbdev port
  [[ -e $SYSFS/class/net/$1/device ]] || return 1
  intf=$(readlink -f "$SYSFS/class/net/$1/device") || return 1   # .../1-1.1.2:1.0
  usbdev=$(dirname "$intf")                                      # .../1-1.1.2
  [[ -e $usbdev/port ]] || return 1
  port=$(readlink -f "$usbdev/port") || return 1                 # .../1-1.1:1.0/1-1.1-port2
  [[ -n $port && -d $port ]] || return 1
  echo "$port"
}

# port_child PORT - sysfs path of the device that is (or would be) plugged into
# PORT: <hub device>/<hub name>.<port number>. It may not exist.
port_child() {
  local port hubdev hub n
  port=$(readlink -f "$1") || return 1
  hubdev=$(dirname "$(dirname "$port")")       # .../1-1.1  (port lives in 1-1.1:1.0/)
  hub=$(basename "$hubdev")
  n=${port##*-port}
  echo "$hubdev/$hub.$n"
}

# port_safe PORT IFACE [EXPECTED_DEVICE] - 0 when PORT may be power-cycled for
# IFACE's adapter. Prints the reason when not.
port_safe() {
  local port iface=$2 expected=${3:-} child name n other dev cls
  # Resolve first: the parent hub is derived from the real device tree, not
  # from wherever a /sys/bus/usb/devices/... link happens to live.
  port=$(readlink -f "$1" 2>/dev/null) || port=$1
  name=$(basename "$port")
  if [[ ! -d $port || ! -e $port/disable ]]; then
    echo "port $name has no 'disable' control"; return 1
  fi
  if [[ $name == usb*-port* ]]; then
    echo "port $name is a root-hub port (would cut every USB device)"; return 1
  fi
  n=${name##*-port}
  if ! [[ $n =~ ^[0-9]+$ ]]; then
    echo "cannot parse the port number of $name"; return 1
  fi
  child=$(port_child "$port")
  # No other network interface (uplink!) may live behind this port.
  for other in "$SYSFS"/class/net/*; do
    [[ -e $other/device ]] || continue
    case $(basename "$other") in
      "$iface"|"$iface"mon) continue ;;
    esac
    dev=$(readlink -f "$other/device") || continue
    if [[ $dev == "$child" || $dev == "$child"/* ]]; then
      echo "port $name carries network interface $(basename "$other")"; return 1
    fi
  done
  if [[ -d $child ]]; then
    cls=$(read_state "$child/bDeviceClass" "")
    if [[ $cls == 09 ]]; then
      echo "port $name holds a USB hub"; return 1
    fi
    if [[ -n $expected && $child != "$expected" ]]; then
      echo "port $name holds $(basename "$child"), not the adapter $(basename "$expected")"; return 1
    fi
  fi
  return 0
}

# -------------------------------------------------------------- decision --

# next_action STRIKES PRESENT REPLUGGED CYCLE_AGE - the step for this silent check.
#   PRESENT    1 = the adapter's interface exists (it is on the bus)
#   REPLUGGED  1 = a software re-plug already ran in this outage without recovery
#   CYCLE_AGE  seconds since the last port power-cycle (large = never)
# Prints one of: none restart driver replug port_cycle restart_only
# (restart_only = a power-cycle was due but the rate limit forbids it).
next_action() {
  local strikes=$1 present=$2 replugged=$3 age=$4 want=""
  if (( strikes == S_RESTART )); then
    echo restart; return
  elif (( strikes == S_DRIVER )); then
    if (( present )); then echo driver; return; fi
    want=port_cycle
  elif (( strikes >= S_USB )); then
    if (( present && ! replugged )); then echo replug; return; fi
    want=port_cycle
  else
    echo none; return
  fi
  if (( age < PC_MIN_INTERVAL )); then echo restart_only; else echo "$want"; fi
}

# ---------------------------------------------------------------- actions --

# power_cycle_port PORT - stop Kismet, switch the port off and on, wait for the
# interface, start Kismet. Logs whether the adapter came back.
power_cycle_port() {
  local port=$1 i
  log "power-cycling hub port $(basename "$port") of $IFACE (state: $(read_state "$port/state" "?")) and restarting kismet"
  date +%s >"$PC_FILE"
  systemctl stop kismet
  if ! echo 1 >"$port/disable"; then
    warn "could not switch off $(basename "$port")"
  fi
  sleep "$PC_OFF"
  echo 0 >"$port/disable" || warn "could not switch $(basename "$port") back on"
  # The RTL8821CU first enumerates as USB storage and mode-switches ~15 s later.
  for ((i = 0; i < 45; i++)); do
    [[ -e $SYSFS/class/net/$IFACE ]] && break
    sleep 1
  done
  if [[ -e $SYSFS/class/net/$IFACE ]]; then
    log "$IFACE is back after the power-cycle (${i}s)"
  else
    warn "$IFACE still missing after the power-cycle (port state: $(read_state "$port/state" "?"))"
  fi
  systemctl start kismet
}

# ------------------------------------------------------------------- main --
main() {
  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  [[ -n $IFACE ]] || { warn "CAPTURE_IFACE is not set"; exit 0; }
  mkdir -p "$STATE_DIR" "$PERSIST_DIR"
  LAST_FILE=$STATE_DIR/capture-packets
  STRIKE_FILE=$STATE_DIR/capture-strikes
  DEV_FILE=$STATE_DIR/capture-usb-device
  DRV_FILE=$STATE_DIR/capture-driver
  REPLUG_FILE=$STATE_DIR/capture-replugged
  PORT_FILE=$PERSIST_DIR/capture-usb-port
  PORT_DEV_FILE=$PERSIST_DIR/capture-usb-port-device
  PC_FILE=$PERSIST_DIR/capture-port-cycled-at

  reset_state() { echo 0 >"$STRIKE_FILE"; echo "${1:-0}" >"$LAST_FILE"; rm -f "$REPLUG_FILE"; }

  # Remember where the adapter lives while it is visible: its USB device and
  # driver (re-plug, module reload) and its hub port (power-cycle, persistent).
  # Refreshed on every run, so moving the adapter to another port is followed.
  local present=0 devpath usbdev drv port old_port
  if [[ -e $SYSFS/class/net/$IFACE/device ]]; then
    present=1
    devpath=$(readlink -f "$SYSFS/class/net/$IFACE/device")   # .../1-1.1.2:1.0
    usbdev=$(dirname "$devpath")                               # .../1-1.1.2
    [[ -e $usbdev/authorized ]] && echo "$usbdev" >"$DEV_FILE"
    drv=$(basename "$(readlink -f "$devpath/driver" 2>/dev/null)" 2>/dev/null)
    [[ -n $drv ]] && echo "$drv" >"$DRV_FILE"
    if port=$(usb_port_of_iface "$IFACE"); then
      old_port=$(read_state "$PORT_FILE" "")
      if [[ $port != "$old_port" ]]; then
        echo "$port" >"$PORT_FILE"
        echo "$usbdev" >"$PORT_DEV_FILE"
        log "$IFACE is on hub port $(basename "$port")${old_port:+ (was $(basename "$old_port"))}"
      fi
    fi
  fi

  # Kismet not running: systemd owns that problem (Restart=always). Start fresh.
  if ! systemctl is-active --quiet kismet; then
    reset_state 0
    exit 0
  fi

  # Sum of num_packets over all datasources, or "" when the API does not answer.
  local packets="" auth json
  if [[ -s $AUTH_FILE ]]; then
    auth=$(awk -F= '$1 == "httpd_username" {u = $2} $1 == "httpd_password" {sub(/^[^=]*=/, ""); p = $0} END {print u ":" p}' "$AUTH_FILE")
    json=$(curl -fsS -m 10 -K - "$URL/datasource/all_sources.json" <<<"user = \"$auth\"" 2>/dev/null || true)
    if [[ -n $json ]]; then
      packets=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    print(sum(int(s.get("kismet.datasource.num_packets") or 0) for s in json.load(sys.stdin)))
except Exception:
    pass' 2>/dev/null || true)
    fi
  else
    warn "auth file $AUTH_FILE missing - cannot query Kismet"
    exit 0
  fi

  local last strikes reason action
  last=$(read_state "$LAST_FILE" 0)
  strikes=$(read_state "$STRIKE_FILE" 0)

  # Alive = the counter moved. A counter that dropped means Kismet restarted and
  # is receiving again; an identical non-zero counter means the source is stuck.
  if [[ -n $packets && $packets -gt 0 && $packets -ne $last ]]; then
    if (( strikes > 0 )); then log "capture recovered ($packets packets)"; fi
    reset_state "$packets"
    exit 0
  fi

  (( strikes += 1 ))
  echo "$strikes" >"$STRIKE_FILE"
  [[ -n $packets ]] && echo "$packets" >"$LAST_FILE"
  reason=${packets:+"packet counter stuck at $packets"}
  reason=${reason:-"REST API not answering"}
  (( present )) || reason="$reason; $IFACE is not on the bus"
  log "no frames from $IFACE: $reason (strike $strikes)"

  local replugged=0 cycled_at age
  [[ -e $REPLUG_FILE ]] && replugged=1
  cycled_at=$(read_state "$PC_FILE" 0)
  age=$(( $(date +%s) - cycled_at ))
  # No RTC: a stamp from a later clock than the current one (boot before chrony
  # stepped the clock) must not block power-cycles for days.
  (( age < 0 )) && age=$(( PC_MIN_INTERVAL + 1 ))
  action=$(next_action "$strikes" "$present" "$replugged" "$age")

  case $action in
    restart)
      log "restarting kismet"
      systemctl restart kismet
      ;;
    driver)
      drv=$(read_state "$DRV_FILE" "")
      if [[ -n $drv ]]; then
        log "reloading driver $drv and restarting kismet"
        systemctl stop kismet
        modprobe -r "$drv" 2>&1 | sed 's/^/capture-watchdog: /' || true
        sleep 2
        modprobe "$drv" 2>&1 | sed 's/^/capture-watchdog: /' || true
        sleep 5
        systemctl start kismet
      else
        warn "driver of $IFACE unknown - skipping module reload"
        systemctl restart kismet
      fi
      ;;
    replug)
      usbdev=$(read_state "$DEV_FILE" "")
      if [[ -n $usbdev && -e $usbdev/authorized ]]; then
        log "re-plugging USB device $(basename "$usbdev") and restarting kismet"
        systemctl stop kismet
        echo 0 >"$usbdev/authorized"; sleep 3
        echo 1 >"$usbdev/authorized"; sleep 10
        systemctl start kismet
        touch "$REPLUG_FILE"
      else
        warn "USB device of $IFACE unknown - restarting kismet only"
        systemctl restart kismet
      fi
      ;;
    port_cycle)
      port=$(read_state "$PORT_FILE" "")
      local why
      if [[ -z $port ]]; then
        warn "hub port of $IFACE never seen (adapter not visible since this was installed) - restarting kismet only"
        systemctl restart kismet
      elif ! why=$(port_safe "$port" "$IFACE" "$(read_state "$PORT_DEV_FILE" "")"); then
        warn "not power-cycling: $why - restarting kismet only"
        systemctl restart kismet
      else
        power_cycle_port "$port"
        rm -f "$REPLUG_FILE"
      fi
      ;;
    restart_only)
      log "port power-cycle due, but the last one was ${age}s ago (< ${PC_MIN_INTERVAL}s) - restarting kismet only"
      systemctl restart kismet
      ;;
  esac

  # Start the escalation over rather than hammering the bus every 2 minutes.
  if (( strikes >= S_USB )); then echo 0 >"$STRIKE_FILE"; fi
  exit 0
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
