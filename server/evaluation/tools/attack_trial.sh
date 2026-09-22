#!/usr/bin/env bash
# attack_trial.sh - run ONE staged deauth trial against our OWN isolated test AP
# and append a ground-truth row. Runs on the attacker device, never on the Pi.
#
# This is a measurement wrapper, not an attack tool: its job is the GUARDRAIL and
# the PROVENANCE. It refuses to transmit unless the target is on the TEST_BSSID
# allowlist and the radio is on the configured isolated channel, and it records
# the trial's identity and wall-clock start/stop so the evaluation can join it
# against the sensor's detections. It only ever points at the AP we own.
#
# Authorisation is the operator's responsibility: only run against an access
# point you own, on a channel you have cleared, having told whoever needs to know.
#
# Usage:
#   TEST_BSSID=AA:BB:CC:DD:EE:FF TEST_CHANNEL=36 IFACE=wlan1 GT=docs/groundtruth.csv \
#     ./attack_trial.sh <trial_id> <variant: fast|moderate|slow> <locked|hopping> <duration_s>
#
# Requires: aireplay-ng (aircrack-ng) and an injection-capable adapter in monitor
# mode. Verify injection first with `aireplay-ng --test`.

set -Eeuo pipefail

TRIAL_ID=${1:?trial_id}; VARIANT=${2:?variant}; CHAN_CONFIG=${3:?locked|hopping}; DURATION=${4:?duration_s}
: "${TEST_BSSID:?set TEST_BSSID to your own test AP}"
: "${TEST_CHANNEL:?set TEST_CHANNEL to the cleared isolated channel}"
: "${IFACE:?set IFACE to the monitor-mode injection interface}"
: "${GT:=docs/groundtruth.csv}"
TARGET=${TARGET:-$TEST_BSSID}
CLIENT=${CLIENT:-}          # optional own test client for unicast

# --- guardrails --------------------------------------------------------------
# 1. the target must be OUR test AP (exact match against the allowlist).
if [[ "${TARGET^^}" != "${TEST_BSSID^^}" ]]; then
  echo "refusing: target $TARGET is not the allowlisted TEST_BSSID $TEST_BSSID" >&2
  exit 2
fi
# 2. the radio must be parked on the cleared channel (no stray transmissions).
CUR_CHAN=$(iw dev "$IFACE" info 2>/dev/null | awk '/channel/{print $2; exit}')
if [[ -n "$CUR_CHAN" && "$CUR_CHAN" != "$TEST_CHANNEL" ]]; then
  echo "refusing: $IFACE is on channel $CUR_CHAN, not the cleared $TEST_CHANNEL" >&2
  exit 2
fi

case "$VARIANT" in
  fast)     RATE=0  ;;   # aireplay-ng default burst (>10/s)
  moderate) RATE=1  ;;   # ~1 frame/s
  slow)     RATE=0.4;;   # 1 frame every ~2.5 s
  *) echo "unknown variant $VARIANT (fast|moderate|slow)" >&2; exit 2;;
esac

# --- run ---------------------------------------------------------------------
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[$TRIAL_ID] $VARIANT/$CHAN_CONFIG on $TARGET ch$TEST_CHANNEL for ${DURATION}s from $START" >&2

# One deauth every 1/RATE seconds for DURATION; -a is the BSSID (our AP), -c the
# optional own client. `timeout` bounds the run; frame count comes from the pcap.
DEAUTH_ARGS=(--deauth 1 -a "$TARGET")
[[ -n "$CLIENT" ]] && DEAUTH_ARGS+=(-c "$CLIENT")
if [[ "$VARIANT" == "fast" ]]; then
  timeout "${DURATION}s" aireplay-ng --deauth 0 -a "$TARGET" ${CLIENT:+-c "$CLIENT"} "$IFACE" || true
else
  END=$(( $(date +%s) + DURATION ))
  while (( $(date +%s) < END )); do
    aireplay-ng "${DEAUTH_ARGS[@]}" "$IFACE" >/dev/null 2>&1 || true
    sleep "$(awk "BEGIN{print 1/$RATE}")"
  done
fi

STOP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- append the ground-truth row (frames_claimed left blank; pcap is truth) --
if [[ ! -f "$GT" ]]; then
  echo "trial_id,type,variant,channel_config,target_bssid,start_utc,stop_utc,frames_claimed,pcap_file,notes,first_frame_utc,last_frame_utc,pcap_frames,frame_count_ok" > "$GT"
fi
printf '%s,deauth_flood,%s,%s,%s,%s,%s,,cap/%s.pcap,,,,,\n' \
  "$TRIAL_ID" "$VARIANT" "$CHAN_CONFIG" "$TARGET" "$START" "$STOP" "$TRIAL_ID" >> "$GT"
echo "[$TRIAL_ID] done $STOP -> $GT" >&2
