#!/bin/sh
# End-to-end timing of the dashboard as the developer's browser sees it: every
# request of every page through Caddy (index.html, the JS/CSS bundle, the
# page's API calls), measured with curl from the Mac - network, Caddy, api and
# database together.
#
#   cd server
#   BASE_URL=http://<box> KEY=<device_key> sh tests/perf/e2e.sh first    # one pass
#   BASE_URL=http://<box> KEY=<device_key> N=5 sh tests/perf/e2e.sh warm # N passes
#
# first = one pass, meant for the first visit after the box sat idle (hours,
# no restart, no cache drop); warm = N passes (default 5) with GAP seconds
# between requests (default 2 - back-to-back requests run the e2-micro out of
# CPU burst credit, docs/performance.md), median and max per request.
# BASE_URL is the public address (never committed); KEY is the AP whose page
# is loaded (measure.sh prints the busiest one; taking it from /api/aps here
# would warm the database before a `first` pass). NEW_API=1 requests the
# schema-6 AP page calls (/rssi without bucket=).
#
# Read-only: GET requests only, no ssh. Each request is its own curl, i.e. its
# own TCP connection, so time_connect is one round trip on every line; a
# browser reuses up to 6 connections and pays it once per connection. The
# browser's parallel API calls are sequential here. Sizes: wire = body bytes
# as sent with `Accept-Encoding: deflate, gzip` (curl --compressed; Caddy also
# offers zstd to browsers), raw = decompressed body.
set -eu
: "${BASE_URL:?set BASE_URL=http://<box address>}"
: "${KEY:?set KEY=<device_key of the AP page to load>}"
MODE="${1:-warm}"
N="${N:-5}"
GAP="${GAP:-2}"
[ "$MODE" = first ] && N=1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# one request -> "page path connect appconnect ttfb total wire raw code enc"
req() {
  page=$1 path=$2
  curl -s --compressed -o "$tmp/body" \
    -w "%{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total} %{size_download} %{http_code} %header{content-encoding}\n" \
    "$BASE_URL$path" > "$tmp/w" || echo "0 0 0 0 0 000 -" > "$tmp/w"
  raw=$(wc -c < "$tmp/body" | tr -d ' ')
  read -r c a s t w code enc < "$tmp/w" || true
  printf '%s %s %s %s %s %s %s %s %s %s\n' "$page" "$path" "$c" "$a" "$s" "$t" "$w" "$raw" "$code" "${enc:--}"
  [ "$MODE" = first ] || sleep "$GAP"
}

pass() {
  # The shell every page loads: index.html, then its bundle.
  req shell /
  for a in $(grep -o -E '/assets/[^"]+\.(js|css)' "$tmp/body"); do req shell "$a"; done
  req shell /api/health
  req overview /api/overview
  req overview /api/findings
  req overview "/api/alerts?limit=50"
  req inventory /api/aps
  req ap "/api/aps/$KEY"
  if [ -n "${NEW_API:-}" ]; then req ap "/api/aps/$KEY/rssi"; else req ap "/api/aps/$KEY/rssi?bucket=3600"; fi
  req detections /api/detections
}

echo "# e2e $MODE $(date -u +%Y-%m-%dT%H:%M:%SZ), N=$N, gap ${GAP}s"
i=0
while [ "$i" -lt "$N" ]; do pass; i=$((i + 1)); done > "$tmp/all"

# Median and max per request (ms); sizes from the last pass.
awk '
{ k = $1 " " $2; if (!(k in seen)) { seen[k] = ++nk; key[nk] = k }
  n[k]++; c[k, n[k]] = $3 * 1000; a[k, n[k]] = $4 * 1000; s[k, n[k]] = $5 * 1000; t[k, n[k]] = $6 * 1000
  w[k] = $7; r[k] = $8; code[k] = $9; enc[k] = $10 }
function med(arr, k, m,   i, j, x, v) {
  for (i = 1; i <= m; i++) v[i] = arr[k, i]
  for (i = 2; i <= m; i++) { x = v[i]; for (j = i - 1; j >= 1 && v[j] > x; j--) v[j + 1] = v[j]; v[j + 1] = x }
  return v[int((m + 1) / 2)]
}
function mx(arr, k, m,   i, x) { x = arr[k, 1]; for (i = 2; i <= m; i++) if (arr[k, i] > x) x = arr[k, i]; return x }
END {
  printf "%-10s %-44s %8s %8s %8s %8s %8s %9s %9s %4s %s\n", "page", "request", "connect", "tls", "ttfb", "total", "max", "wire_B", "raw_B", "HTTP", "enc"
  for (i = 1; i <= nk; i++) { k = key[i]; m = n[k]; split(k, p, " ")
    printf "%-10s %-44s %8.0f %8.0f %8.0f %8.0f %8.0f %9d %9d %4s %s\n", p[1], substr(p[2], 1, 44),
      med(c, k, m), med(a, k, m), med(s, k, m), med(t, k, m), mx(t, k, m), w[k], r[k], code[k], enc[k] }
}' "$tmp/all"
