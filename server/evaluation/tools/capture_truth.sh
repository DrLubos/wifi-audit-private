#!/usr/bin/env bash
# capture_truth.sh - the independent ground-truth capture for one trial.
#
# Runs a monitor-mode capture beside the attack (on the attacker device or a
# third monitor interface) and writes a per-trial pcap of the air. This is the
# sensor-independent record that `truth-check` reads for the true frame count and
# the first-frame timestamp that anchors latency - never trust only the attack
# script's own clock.
#
# Start it just before attack_trial.sh and stop it just after (Ctrl-C, or pass a
# duration). Capturing on the attacker sees exactly the frames it injects, which
# is what we want to corroborate.
#
# Usage:
#   IFACE=wlan1mon ./capture_truth.sh <trial_id> [duration_s]
#   # writes cap/<trial_id>.pcap (the path attack_trial.sh records)

set -Eeuo pipefail

TRIAL_ID=${1:?trial_id}; DURATION=${2:-}
: "${IFACE:?set IFACE to a monitor-mode capture interface}"
OUT_DIR=${OUT_DIR:-cap}
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$TRIAL_ID.pcap"

# Capture only management deauth/disassoc frames (subtype 10/12) to keep the pcap
# small; frame.time_epoch in the file is the capture clock (chrony-synced).
FILTER='type mgt and (subtype deauth or subtype disassoc)'

echo "capturing $IFACE -> $OUT ${DURATION:+for ${DURATION}s}" >&2
if command -v tshark >/dev/null 2>&1; then
  tshark -i "$IFACE" -w "$OUT" ${DURATION:+-a duration:$DURATION} -f "$FILTER"
else
  tcpdump -i "$IFACE" -w "$OUT" ${DURATION:+-G "$DURATION" -W 1} "$FILTER"
fi
echo "wrote $OUT" >&2
