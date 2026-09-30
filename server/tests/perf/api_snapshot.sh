#!/bin/sh
# Save the dashboard API responses for a few APs as JSON files, so the answers
# of two schema versions can be compared (compare_api.py). Runs on the Mac,
# GET requests only.
#
#   cd server
#   BASE_URL=http://<box> OUT=tests/perf/out/schema5 sh tests/perf/api_snapshot.sh KEY...
#
# Per AP: the page (/api/aps/KEY), the hourly RSSI series and the 15-min
# series over RAW_FROM..RAW_TO (default: the 48 h before 2026-09-27 14:00 UTC,
# the end of the seeded data). Plus /api/overview, /api/aps, /api/detections.
# OUT is gitignored (tests/perf/out/): the files hold SSIDs and BSSIDs.
set -eu
: "${BASE_URL:?set BASE_URL=http://<box address>}"
: "${OUT:?set OUT=<directory>}"
RAW_FROM="${RAW_FROM:-2026-09-25T14:00:00Z}"
RAW_TO="${RAW_TO:-2026-09-27T14:00:00Z}"
mkdir -p "$OUT"

get() {  # get NAME PATH
  code=$(curl -s --compressed -o "$OUT/$1.json" -w '%{http_code}' "$BASE_URL$2")
  printf '%s %s -> %s\n' "$code" "$2" "$OUT/$1.json"
}

get overview /api/overview
get aps /api/aps
get detections /api/detections
for k in "$@"; do
  get "ap_$k" "/api/aps/$k"
  get "rssi_hourly_$k" "/api/aps/$k/rssi?bucket=3600"
  get "rssi_raw_$k" "/api/aps/$k/rssi?bucket=900&from=$RAW_FROM&to=$RAW_TO"
done
