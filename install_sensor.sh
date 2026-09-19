#!/usr/bin/env bash
#
# install_sensor.sh - provision the passive Wi-Fi monitoring sensor (Milestone 1)
#
# Installs Kismet from its official apt repository on a Raspberry Pi running
# Raspberry Pi OS 64-bit Lite (Debian trixie), configures it to capture
# passively on a dedicated USB Wi-Fi adapter, and runs it as an unprivileged
# systemd service that starts on boot. Also installs chrony for time sync.
#
# Cold-boot robustness (sensor/ directory, installed to INSTALL_DIR/sensor):
#   kismet-prestart.sh   ExecStartPre of kismet.service: waits for the USB
#                        adapter and the clock, unblocks the radio, removes a
#                        stale monitor VIF, disables power saving.
#   capture-watchdog.sh  systemd timer: checks Kismet's packet counter every
#                        2 min; restarts Kismet, reloads the driver, re-plugs
#                        the USB device when no frames arrive.
#
# Usage - on the Pi, from your normal user account (never as root directly):
#
#   sudo ./install_sensor.sh
#   sudo CAPTURE_IFACE=wlan1 ./install_sensor.sh
#
# Configuration - environment variables, or KEY=value lines in sensor.conf next
# to this script (see sensor.conf.example). Environment variables take
# precedence over the file. Nothing sensor-specific is hardcoded below.
#
#   CAPTURE_IFACE      Wireless interface of the USB capture adapter (e.g. wlan1).
#                      Prompted for if unset.
#   SENSOR_USER        Unprivileged user that runs the Kismet server.
#                      Default: the user who invoked sudo.
#   KISMET_LOG_DIR     Directory for kismetdb logs.   Default: /var/lib/kismet
#   KISMET_LOG_TITLE   Log file name prefix.          Default: wifi-sensor
#   KISMET_HTTPD_PORT  Web UI port.                   Default: 2501
#   KISMET_HTTPD_USER  Web UI username  - prompted for if unset. Environment
#   KISMET_HTTPD_PASS  Web UI password    only; keep credentials out of sensor.conf.
#   INSTALL_DIR        Where the helper scripts go.  Default: /opt/wifi-sensor
#   SENSOR_CONF        Config file path. Default: <script dir>/sensor.conf
#
# Idempotent: every step inspects the current state and only changes what
# differs, so the script can be re-run at any time (e.g. after swapping the
# adapter). Kismet is only restarted when something actually changed.
#
# Safety: interfaces carrying the default route, a global IP address or the
# current SSH session are detected and never touched. The capture adapter must
# be USB-attached and support monitor mode, otherwise the script refuses to run.
#
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_VARS=(CAPTURE_IFACE SENSOR_USER KISMET_LOG_DIR KISMET_LOG_TITLE
           KISMET_HTTPD_PORT KISMET_HTTPD_USER KISMET_HTTPD_PASS INSTALL_DIR)

# Helper scripts (sensor/) and unit templates (systemd/) shipped in this repository.
SENSOR_SRC_DIR="$SCRIPT_DIR/sensor"
WATCHDOG_UNIT="wifi-sensor-capture-watchdog"
WATCHDOG_SERVICE_TEMPLATE="$SCRIPT_DIR/systemd/$WATCHDOG_UNIT.service"
WATCHDOG_TIMER_TEMPLATE="$SCRIPT_DIR/systemd/$WATCHDOG_UNIT.timer"

# Locations defined by the Kismet / Debian packaging (not sensor-specific).
KISMET_KEY_URL="https://www.kismetwireless.net/repos/kismet-release.gpg.key"
KISMET_REPO_URL="https://www.kismetwireless.net/repos/apt/release"
KISMET_KEYRING="/usr/share/keyrings/kismet-archive-keyring.gpg"
KISMET_LIST="/etc/apt/sources.list.d/kismet.list"
KISMET_HELPER="/usr/bin/kismet_cap_linux_wifi"
KISMET_SITE_CONF="/etc/kismet/kismet_site.conf"
KISMET_ETC_HTTPD_CONF="/etc/kismet/kismet_httpd.conf"
KISMET_UNIT_OVERRIDE="/etc/systemd/system/kismet.service.d/override.conf"
NM_CONF="/etc/NetworkManager/conf.d/99-wifi-sensor-capture.conf"

# ------------------------------------------------------------------ helpers --
ts()   { date '+%H:%M:%S'; }
log()  { printf '%s [info] %s\n' "$(ts)" "$*"; }
warn() { printf '%s [warn] %s\n' "$(ts)" "$*" >&2; }
die()  { printf '%s [fail] %s\n' "$(ts)" "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
trap 'die "aborted at line $LINENO: $BASH_COMMAND"' ERR

usage() {
  sed -n '3,/^set -Eeuo/{ /^set -Eeuo/d; s/^# \{0,1\}//p }' "${BASH_SOURCE[0]}"
}

# Set to 1 whenever a Kismet-relevant file or setting was modified, so the
# service is restarted only when needed.
CHANGED=0
LAST_WRITE_CHANGED=0
mark_changed() { CHANGED=1; }

# write_if_changed DEST MODE OWNER:GROUP   (content on stdin)
# Installs DEST only when its content differs; always enforces mode and owner.
# Feed it with a heredoc or a redirection, never through a pipe - the last
# element of a pipeline runs in a subshell and the CHANGED flags would be lost.
write_if_changed() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  LAST_WRITE_CHANGED=0
  if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then
    log "unchanged: $dest"
  else
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    log "wrote: $dest"
    LAST_WRITE_CHANGED=1
    mark_changed
  fi
  chmod "$mode" "$dest"
  chown "$owner" "$dest"
  rm -f "$tmp"
}

pkg_installed() {
  local status
  status=$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)
  [[ $status == *"install ok installed"* ]]
}

load_config() {
  local v
  local -A from_env=()
  if [[ ! -f $SENSOR_CONF ]]; then
    log "no config file at $SENSOR_CONF - using environment and prompts"
    return
  fi
  log "loading $SENSOR_CONF (environment variables take precedence)"
  for v in "${CONF_VARS[@]}"; do
    if [[ -n ${!v:-} ]]; then from_env[$v]=${!v}; fi
  done
  # shellcheck disable=SC1090
  source "$SENSOR_CONF"
  for v in "${!from_env[@]}"; do
    printf -v "$v" '%s' "${from_env[$v]}"
  done
}

# --------------------------------------------------------- interface safety --
# Interfaces that must never be touched: anything carrying a default route, a
# global-scope address, or the SSH session this script runs in.
declare -A PROTECTED=()
detect_protected_ifaces() {
  local ifc ssh_ip
  while read -r ifc; do
    if [[ -n $ifc ]]; then PROTECTED[$ifc]=1; fi
  done < <({ ip -o -4 route show default; ip -o -6 route show default; } 2>/dev/null \
           | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}')
  while read -r ifc; do
    if [[ -n $ifc ]]; then PROTECTED[$ifc]=1; fi
  done < <(ip -o addr show scope global 2>/dev/null | awk '{print $2}')
  if [[ -n ${SSH_CONNECTION:-} ]]; then
    ssh_ip=$(awk '{print $3}' <<<"$SSH_CONNECTION")
    ifc=$(ip -o addr show 2>/dev/null \
          | awk -v ip="$ssh_ip" 'index($4, ip "/") == 1 {print $2; exit}' || true)
    if [[ -n $ifc ]]; then PROTECTED[$ifc]=1; fi
  fi
  PROTECTED[lo]=1
}

wireless_ifaces() {
  local p
  for p in /sys/class/net/*/phy80211; do
    [[ -e $p ]] || continue
    basename "$(dirname "$p")"
  done
}

select_capture_iface() {
  local ifc usb_devs
  local candidates=()
  for ifc in $(wireless_ifaces); do
    # Skip protected interfaces and monitor VIFs that a running Kismet created.
    if [[ -z ${PROTECTED[$ifc]:-} && $ifc != *mon ]]; then candidates+=("$ifc"); fi
  done
  if [[ -n ${CAPTURE_IFACE:-} ]]; then
    log "capture interface from config: $CAPTURE_IFACE"
    return
  fi
  if [[ ${#candidates[@]} -eq 0 ]]; then
    warn "no unprotected wireless interface found (present: $(wireless_ifaces | tr '\n' ' '))"
    usb_devs=$(lsusb 2>/dev/null || true)
    if [[ $usb_devs == *"a69c:"* ]]; then
      warn "an AICSemi (AIC8800-family) dongle is attached in USB mass-storage mode;"
      warn "it has no usable Linux monitor-mode driver and cannot serve as the capture adapter"
    fi
    die "plug in the USB capture adapter and re-run (check with: iw dev), or set CAPTURE_IFACE"
  fi
  [[ -t 0 ]] || die "CAPTURE_IFACE is not set and there is no terminal to prompt on"
  read -r -p "Capture interface [${candidates[0]}] (candidates: ${candidates[*]}): " CAPTURE_IFACE
  CAPTURE_IFACE=${CAPTURE_IFACE:-${candidates[0]}}
}

# Checks that need only /sys - run before the long apt steps so a wrong
# interface fails fast.
check_capture_iface() {
  local ifc=$CAPTURE_IFACE devpath
  [[ -e /sys/class/net/$ifc ]] || die "interface '$ifc' does not exist (see: iw dev)"
  if [[ -n ${PROTECTED[$ifc]:-} ]]; then
    die "'$ifc' carries the uplink / SSH session (protected: ${!PROTECTED[*]}) - refusing to touch it"
  fi
  [[ -e /sys/class/net/$ifc/phy80211 ]] || die "'$ifc' is not a wireless (cfg80211) interface"
  devpath=$(readlink -f "/sys/class/net/$ifc/device")
  if [[ $devpath != */usb[0-9]* ]]; then
    die "'$ifc' is not USB-attached ($devpath); the built-in radio must never be used for capture"
  fi
  CAPTURE_PHY=$(<"/sys/class/net/$ifc/phy80211/name")
  CAPTURE_DRIVER=$(basename "$(readlink -f "/sys/class/net/$ifc/device/driver")")
  CAPTURE_MAC=$(<"/sys/class/net/$ifc/address")
  log "capture interface: $ifc  phy=$CAPTURE_PHY  driver=$CAPTURE_DRIVER  mac=$CAPTURE_MAC"
}

# Needs iw, so it runs after the base packages are installed.
check_monitor_mode() {
  local modes
  modes=$(iw phy "$CAPTURE_PHY" info \
          | awk '/Supported interface modes:/ {f = 1; next} f && /^\t[^\t]/ {f = 0} f')
  if [[ $modes != *"* monitor"* ]]; then
    die "'$CAPTURE_IFACE' ($CAPTURE_PHY, driver $CAPTURE_DRIVER) has no monitor mode - supported modes:$modes"
  fi
  log "monitor mode: supported (frame injection is not needed by a passive sensor and is not tested)"
}

# ------------------------------------------------------------- credentials --
KEEP_CREDS=0
collect_credentials() {
  local existing keep p1 p2
  if [[ -n ${KISMET_HTTPD_USER:-} && -n ${KISMET_HTTPD_PASS:-} ]]; then
    log "web UI credentials taken from the environment"
    return
  fi
  if [[ -s $AUTH_FILE ]]; then
    existing=$(awk -F= '$1 == "httpd_username" {print $2; exit}' "$AUTH_FILE")
    if [[ -n $existing ]]; then
      keep=Y
      if [[ -t 0 ]]; then
        read -r -p "Web UI login already exists (user '$existing'). Keep it? [Y/n] " keep
      fi
      if [[ ${keep:-Y} =~ ^[Yy]?$ ]]; then
        KEEP_CREDS=1
        KISMET_HTTPD_USER=$existing
        KISMET_HTTPD_PASS=$(awk -F= '$1 == "httpd_password" {sub(/^[^=]*=/, ""); print; exit}' "$AUTH_FILE")
        return
      fi
    fi
  fi
  [[ -t 0 ]] || die "web UI credentials not set (KISMET_HTTPD_USER/KISMET_HTTPD_PASS) and no terminal to prompt on"
  echo "Choose the login for the Kismet web UI (stored only in $AUTH_FILE, mode 0600)."
  while :; do
    read -r -p "Kismet web UI username: " KISMET_HTTPD_USER
    if [[ -n $KISMET_HTTPD_USER ]]; then break; fi
    warn "username must not be empty"
  done
  while :; do
    read -r -s -p "Kismet web UI password: " p1; echo
    read -r -s -p "Repeat password: " p2; echo
    if [[ -n $p1 && $p1 == "$p2" ]]; then
      KISMET_HTTPD_PASS=$p1
      break
    fi
    warn "passwords are empty or do not match - try again"
  done
}

# ---------------------------------------------------------------- packages --
install_base_packages() {
  step "APT: update and base tools"
  apt-get update -q
  apt-get install -y -q iw rfkill curl gnupg ca-certificates libcap2-bin usbutils
}

setup_kismet_repo() {
  step "Kismet apt repository ($CODENAME)"
  local refresh=0 tmp
  if [[ -s $KISMET_KEYRING ]] && gpg --quiet --show-keys "$KISMET_KEYRING" >/dev/null 2>&1; then
    log "signing key present: $KISMET_KEYRING"
  else
    log "fetching signing key from $KISMET_KEY_URL"
    tmp=$(mktemp)
    curl -fsSL "$KISMET_KEY_URL" | gpg --dearmor >"$tmp"
    write_if_changed "$KISMET_KEYRING" 0644 root:root <"$tmp"
    rm -f "$tmp"
    refresh=1
  fi
  write_if_changed "$KISMET_LIST" 0644 root:root \
    <<<"deb [signed-by=$KISMET_KEYRING] $KISMET_REPO_URL/$CODENAME $CODENAME main"
  if [[ $LAST_WRITE_CHANGED -eq 1 ]]; then refresh=1; fi
  if [[ $refresh -eq 1 ]]; then
    apt-get update -q
  fi
}

# True when the capture helper can get raw-socket privileges without the
# server being root: either file capabilities (current packaging) or setuid.
helper_privileged() {
  local caps
  caps=$(getcap "$KISMET_HELPER" 2>/dev/null || true)
  [[ $caps == *cap_net_raw* || -u $KISMET_HELPER ]]
}

install_kismet() {
  step "Kismet packages"
  # Pre-answer the packaging question: install the capture helpers privileged
  # (group kismet + capabilities / setuid root) so the server itself can run
  # as an unprivileged member of the kismet group.
  debconf-set-selections <<<"kismet-common kismet-common/install-setuid boolean true"
  if pkg_installed kismet; then
    log "kismet already installed: $(dpkg-query -W -f='${Version}' kismet)"
  fi
  apt-get install -y -q kismet
  if ! helper_privileged; then
    log "capture helper is not privileged - reconfiguring"
    dpkg-reconfigure -f noninteractive kismet-capture-linux-wifi
    helper_privileged || die "$KISMET_HELPER is neither setuid nor capability-enabled; refusing to run Kismet as root"
  fi
  log "capture helper: $(stat -c '%U:%G %a' "$KISMET_HELPER") $(getcap "$KISMET_HELPER" 2>/dev/null || true)"
}

ensure_kismet_group() {
  local groups
  groups=$(id -nG "$SENSOR_USER")
  if [[ " $groups " == *" kismet "* ]]; then
    log "$SENSOR_USER is already in group kismet"
  else
    usermod -aG kismet "$SENSOR_USER"
    log "added $SENSOR_USER to group kismet (takes effect for new logins)"
    mark_changed
  fi
}

install_chrony() {
  step "Time synchronisation (chrony)"
  if pkg_installed chrony; then
    log "chrony already installed"
  else
    # Replaces systemd-timesyncd (apt removes it: both provide time-daemon).
    apt-get install -y -q chrony
  fi
  systemctl enable --now chrony
  log "chrony: $(systemctl is-active chrony)  $(chronyc -n tracking 2>/dev/null \
        | awk -F': *' '/^(Leap status|System time)/ {printf "%s: %s; ", $1, $2}')"
}

# ---------------------------------------------------------- capture adapter --
prepare_capture_adapter() {
  step "Capture adapter: $CAPTURE_IFACE"
  local id
  # Unblock only the capture phy, never a blanket 'rfkill unblock all'.
  id=$(rfkill -rn -o ID,DEVICE 2>/dev/null \
       | awk -v p="$CAPTURE_PHY" '$2 == p {print $1; exit}' || true)
  if [[ -n $id ]]; then
    rfkill unblock "$id"
    log "rfkill: $CAPTURE_PHY unblocked (id $id)"
  else
    warn "rfkill: no entry for $CAPTURE_PHY - skipping"
  fi
  if iw dev "$CAPTURE_IFACE" set power_save off 2>/dev/null; then
    log "power save: off"
  else
    warn "power save: could not change (driver may not support it, or Kismet already owns the interface)"
  fi
}

configure_network_manager() {
  step "NetworkManager: hand $CAPTURE_IFACE to Kismet"
  if ! systemctl is-active --quiet NetworkManager; then
    log "NetworkManager is not running - nothing to do"
    return
  fi
  write_if_changed "$NM_CONF" 0644 root:root <<EOF
# Managed by install_sensor.sh (wifi-sensor).
# The capture adapter is driven by Kismet's datasource; NetworkManager must not
# touch it, nor the monitor VIF Kismet creates on it. Every other interface -
# including the SSH/uplink interface - stays managed exactly as before.
[keyfile]
unmanaged-devices=interface-name:${CAPTURE_IFACE};interface-name:${CAPTURE_IFACE}mon;mac:${CAPTURE_MAC}
EOF
  # Apply without restarting NetworkManager, so the uplink is never interrupted.
  nmcli general reload conf || warn "nmcli reload failed - the drop-in applies on the next NetworkManager start"
  nmcli device set "$CAPTURE_IFACE" managed no >/dev/null 2>&1 || true
  local state
  state=$(nmcli -t -f DEVICE,STATE device 2>/dev/null \
          | awk -F: -v d="$CAPTURE_IFACE" '$1 == d {print $2; exit}' || true)
  log "NetworkManager state of $CAPTURE_IFACE: ${state:-not listed} (expected: unmanaged)"
}

# ------------------------------------------------------------------ kismet --
configure_kismet() {
  step "Kismet configuration"
  install -d -m 0750 -o "$SENSOR_USER" -g kismet "$KISMET_LOG_DIR"
  install -d -m 0700 -o "$SENSOR_USER" -g "$SENSOR_USER" "$SENSOR_HOME/.kismet"

  write_if_changed "$KISMET_SITE_CONF" 0644 root:root <<EOF
# ${KISMET_SITE_CONF} - managed by install_sensor.sh (wifi-sensor).
# Overrides the packaged defaults in /etc/kismet/kismet*.conf. Re-run the
# installer to change these values instead of editing this file by hand.

# --- capture ---------------------------------------------------------------
# Dedicated USB adapter. Kismet puts it into monitor mode and hops channels
# itself; nothing else on the system touches this interface.
source=${CAPTURE_IFACE}:name=capture,type=linuxwifi
# To pin the sensor to fixed channels later, e.g.:
#   source=${CAPTURE_IFACE}:name=capture,type=linuxwifi,channels="1,6,11"

# --- logging ---------------------------------------------------------------
# kismetdb (SQLite) only; the collector (Milestone 2) reads from it / the REST API.
log_types=kismet
log_prefix=${KISMET_LOG_DIR}
log_title=${KISMET_LOG_TITLE}

# --- web UI ----------------------------------------------------------------
httpd_port=${KISMET_HTTPD_PORT}
# The login lives in ${AUTH_FILE} (mode 0600), never in a world-readable file.
EOF

  # The packaged /etc/kismet/kismet_httpd.conf is world-readable and any
  # httpd_username/httpd_password set there override the per-user auth file.
  # Credentials belong only in the 0600 file in the sensor user's home.
  if grep -Eq '^httpd_(username|password)=' "$KISMET_ETC_HTTPD_CONF" 2>/dev/null; then
    sed -i -E '/^httpd_(username|password)=/d' "$KISMET_ETC_HTTPD_CONF"
    log "removed plaintext credentials from $KISMET_ETC_HTTPD_CONF"
    mark_changed
  fi

  if [[ $KEEP_CREDS -eq 1 ]]; then
    log "keeping existing web UI credentials in $AUTH_FILE"
  else
    write_if_changed "$AUTH_FILE" 0600 "$SENSOR_USER:$SENSOR_USER" <<EOF
httpd_username=${KISMET_HTTPD_USER}
httpd_password=${KISMET_HTTPD_PASS}
EOF
  fi
}

install_sensor_scripts() {
  step "Helper scripts -> $INSTALL_DIR/sensor"
  local f
  [[ -f $SENSOR_SRC_DIR/kismet-prestart.sh && -f $SENSOR_SRC_DIR/capture-watchdog.sh ]] \
    || die "helper scripts not found in $SENSOR_SRC_DIR"
  install -d -m 0755 -o root -g root "$INSTALL_DIR" "$INSTALL_DIR/sensor"
  for f in "$SENSOR_SRC_DIR"/*.sh; do
    write_if_changed "$INSTALL_DIR/sensor/$(basename "$f")" 0755 root:root <"$f"
  done
}

configure_service() {
  step "systemd: kismet.service"
  write_if_changed "$KISMET_UNIT_OVERRIDE" 0644 root:root <<EOF
# Managed by install_sensor.sh (wifi-sensor).
# The packaged unit runs Kismet as root. Run the server as the unprivileged
# sensor user instead - only the capture helper (kismet_cap_linux_wifi, group
# kismet, file capabilities) is privileged.
[Unit]
After=network-online.target chrony.service
Wants=network-online.target chrony.service

[Service]
User=${SENSOR_USER}
Group=kismet
WorkingDirectory=${KISMET_LOG_DIR}
# Before every (re)start: wait for the USB adapter and for chrony (both
# bounded), unblock the radio, remove a stale monitor VIF, power saving off.
# '+' runs it as root, '-' never lets it block Kismet from starting.
Environment=CAPTURE_IFACE=${CAPTURE_IFACE}
ExecStartPre=-+${INSTALL_DIR}/sensor/kismet-prestart.sh
RestartSec=5
EOF
  systemctl daemon-reload
  if [[ $(systemctl is-enabled kismet 2>/dev/null || true) == enabled ]]; then
    log "kismet.service already enabled"
  else
    systemctl enable kismet
    log "kismet.service enabled on boot"
  fi
  if [[ $CHANGED -eq 1 ]] || ! systemctl is-active --quiet kismet; then
    log "(re)starting kismet.service"
    systemctl restart kismet
  else
    log "kismet.service already running with the current configuration"
  fi
}

install_watchdog() {
  step "systemd: $WATCHDOG_UNIT.timer"
  local rendered
  [[ -f $WATCHDOG_SERVICE_TEMPLATE && -f $WATCHDOG_TIMER_TEMPLATE ]] \
    || die "watchdog unit templates not found next to $0"
  rendered=$(sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__CAPTURE_IFACE__|$CAPTURE_IFACE|g" \
                 -e "s|__KISMET_URL__|http://127.0.0.1:$KISMET_HTTPD_PORT|g" \
                 -e "s|__AUTH_FILE__|$AUTH_FILE|g" "$WATCHDOG_SERVICE_TEMPLATE")
  write_if_changed "/etc/systemd/system/$WATCHDOG_UNIT.service" 0644 root:root <<<"$rendered"
  write_if_changed "/etc/systemd/system/$WATCHDOG_UNIT.timer" 0644 root:root <"$WATCHDOG_TIMER_TEMPLATE"
  systemctl daemon-reload
  systemctl enable --now "$WATCHDOG_UNIT.timer" >/dev/null 2>&1
  log "$WATCHDOG_UNIT.timer: $(systemctl is-active "$WATCHDOG_UNIT.timer"), next run $(systemctl show -p NextElapseUSecRealtime --value "$WATCHDOG_UNIT.timer" 2>/dev/null || echo '?')"
}

verify_kismet() {
  step "Verifying Kismet"
  local url="http://127.0.0.1:${KISMET_HTTPD_PORT}" curl_auth i up=0 sources
  # Credentials go through curl's config on stdin, not the command line.
  curl_auth="user = \"${KISMET_HTTPD_USER}:${KISMET_HTTPD_PASS}\""
  # Kismet needs 10-30 s on a Pi 3 to load its databases before the UI answers.
  for ((i = 0; i < 60; i++)); do
    if curl -fsS -o /dev/null -K - "$url/system/status.json" <<<"$curl_auth" 2>/dev/null; then
      up=1
      break
    fi
    sleep 2
  done
  if ! systemctl is-active --quiet kismet; then
    journalctl -u kismet -n 30 --no-pager || true
    die "kismet.service is not running - see the journal above"
  fi
  if [[ $up -eq 0 ]]; then
    journalctl -u kismet -n 20 --no-pager || true
    warn "web UI did not answer on $url within 2 minutes (wrong credentials, or still starting)"
    warn "check with: journalctl -u kismet -f"
    return
  fi
  log "web UI is up on port $KISMET_HTTPD_PORT"
  # "running" is not enough - a source can be up with a dead radio. Give the
  # source up to 30 s to deliver its first frames and report the count.
  for ((i = 0; i < 15; i++)); do
    sources=$(curl -fsS -K - "$url/datasource/all_sources.json" <<<"$curl_auth" 2>/dev/null || true)
    if [[ $(datasource_packets "$sources") -gt 0 ]]; then break; fi
    sleep 2
  done
  if [[ -n $sources ]] && command -v python3 >/dev/null; then
    printf '%s' "$sources" | python3 -c '
import json, sys
try:
    srcs = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for s in srcs:
    name = s.get("kismet.datasource.name")
    iface = s.get("kismet.datasource.capture_interface") or s.get("kismet.datasource.interface")
    pk = s.get("kismet.datasource.num_packets") or 0
    if s.get("kismet.datasource.error"):
        print("  datasource %s (%s): ERROR - %s" % (name, iface, s.get("kismet.datasource.error_reason")))
    elif s.get("kismet.datasource.running") and pk > 0:
        print("  datasource %s (%s): running, hopping=%s, %d packets" % (name, iface, s.get("kismet.datasource.hopping"), pk))
    elif s.get("kismet.datasource.running"):
        print("  datasource %s (%s): running but NO packets yet - the watchdog will restart Kismet if this persists" % (name, iface))
    else:
        print("  datasource %s (%s): not running (yet)" % (name, iface))
' || warn "could not read datasource status; check the Datasources panel in the web UI"
  fi
}

# Sum of kismet.datasource.num_packets in an all_sources.json document (0 on error).
datasource_packets() {
  printf '%s' "$1" | python3 -c '
import json, sys
try:
    print(sum(int(s.get("kismet.datasource.num_packets") or 0) for s in json.load(sys.stdin)))
except Exception:
    print(0)' 2>/dev/null || echo 0
}

print_next_steps() {
  local ip
  ip=$(ip -o -4 addr show scope global 2>/dev/null \
       | awk '{split($4, a, "/"); print a[1]; exit}' || true)
  cat <<EOF

==> Done. Next steps
  Web UI     http://${ip:-<pi-ip>}:${KISMET_HTTPD_PORT}/   (or http://$(hostname).local:${KISMET_HTTPD_PORT}/ with mDNS)
             login: '${KISMET_HTTPD_USER}' with the password you entered
             (stored in ${AUTH_FILE}, mode 0600 - delete it and re-run to reset)
  Service    systemctl status kismet
             journalctl -u kismet -f
  Capture    iw dev                      # expect '${CAPTURE_IFACE}mon' with type monitor
  Watchdog   systemctl list-timers ${WATCHDOG_UNIT}.timer
             journalctl -b -u kismet -u ${WATCHDOG_UNIT}     # pre-start + watchdog lines included
  Logs       ${KISMET_LOG_DIR}/${KISMET_LOG_TITLE}-*.kismet
             kismetdb_statistics --in <file.kismet>
  Time       chronyc tracking
  Rules      never run 'sudo kismet' - the service runs unprivileged as '${SENSOR_USER}'.
             '${SENSOR_USER}' is in group 'kismet'; log out and in again before running kismet by hand.
  Re-run     sudo CAPTURE_IFACE=${CAPTURE_IFACE} $0     (idempotent; offers to keep the login)
EOF
}

# -------------------------------------------------------------------- main --
main() {
  if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
  fi

  step "Preflight"
  [[ $EUID -eq 0 ]] || die "run with sudo from your normal user account: sudo $0"
  SENSOR_CONF="${SENSOR_CONF:-$SCRIPT_DIR/sensor.conf}"
  load_config

  SENSOR_USER="${SENSOR_USER:-${SUDO_USER:-}}"
  if [[ -z $SENSOR_USER || $SENSOR_USER == root ]]; then
    die "cannot determine the unprivileged user; run via sudo from your normal account or set SENSOR_USER"
  fi
  SENSOR_HOME=$(getent passwd "$SENSOR_USER" | cut -d: -f6 || true)
  [[ -n $SENSOR_HOME ]] || die "user '$SENSOR_USER' does not exist"
  AUTH_FILE="$SENSOR_HOME/.kismet/kismet_httpd.conf"
  KISMET_LOG_DIR="${KISMET_LOG_DIR:-/var/lib/kismet}"
  KISMET_LOG_TITLE="${KISMET_LOG_TITLE:-wifi-sensor}"
  KISMET_HTTPD_PORT="${KISMET_HTTPD_PORT:-2501}"
  INSTALL_DIR="${INSTALL_DIR:-/opt/wifi-sensor}"

  CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
  [[ -n $CODENAME ]] || die "cannot determine the Debian codename from /etc/os-release"
  log "host: $(hostname)  os: $(. /etc/os-release && echo "${PRETTY_NAME:-?}") ($CODENAME/$(dpkg --print-architecture))"
  log "sensor user: $SENSOR_USER ($SENSOR_HOME)  log dir: $KISMET_LOG_DIR"

  detect_protected_ifaces
  log "protected (uplink/SSH) interfaces: ${!PROTECTED[*]}"
  select_capture_iface
  check_capture_iface
  # All interactive input happens here, before the long-running apt steps.
  collect_credentials

  install_base_packages
  check_monitor_mode
  setup_kismet_repo
  install_kismet
  ensure_kismet_group
  install_chrony
  prepare_capture_adapter
  configure_network_manager
  configure_kismet
  install_sensor_scripts
  configure_service
  install_watchdog
  verify_kismet
  print_next_steps
}

main "$@"
