-- GET /api/detections (api/app/routers/detections.py), no filters, limit 200 -
-- the Detections page's first request.
\echo == /api/detections: summary
EXPLAIN (ANALYZE, BUFFERS)
SELECT severity, type, count(*) AS n, count(*) FILTER (WHERE NOT acked) AS open
FROM detections WHERE sensor_id = :sid GROUP BY severity, type;
\echo == /api/detections: list
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.id, d.ts, d.type, d.severity, d.device_key, upper(d.mac::text) AS mac, d.ssid,
       d.summary, d.evidence, d.acked, d.acked_at, d.ack_note, d.created_at,
       ap.device_key AS ap_device_key, ap.ssid AS ap_ssid,
       upper(ap.bssid::text) AS ap_bssid, ap.hidden AS ap_hidden, ap.trusted AS ap_trusted
FROM detections d
LEFT JOIN LATERAL (
  SELECT device_key, ssid, bssid, hidden, coalesce(trusted, false) AS trusted, last_seen
  FROM ap_inventory i
  WHERE i.sensor_id = d.sensor_id
    AND (i.device_key = d.device_key OR (d.device_key IS NULL AND i.bssid = d.mac))
  ORDER BY last_seen DESC LIMIT 1) ap ON true
WHERE d.sensor_id = :sid
  AND (NULL::text    IS NULL OR d.severity = NULL::text)
  AND (NULL::text    IS NULL OR d.type = NULL::text)
  AND (NULL::boolean IS NULL OR d.acked = NULL::boolean)
ORDER BY d.ts DESC, d.id DESC
LIMIT 200;
\echo == detections: evidence size (what the list ships)
SELECT count(*) AS rows, pg_size_pretty(sum(pg_column_size(evidence))::bigint) AS evidence_stored,
       pg_size_pretty(sum(octet_length(evidence::text))::bigint) AS evidence_as_json_text,
       max(octet_length(evidence::text)) AS max_row_json_bytes
FROM detections WHERE sensor_id = :sid;
