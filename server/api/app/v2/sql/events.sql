-- GET /api/v2/events (the feed: keyset on the rows' (ts, id), never OFFSET)
-- and GET /api/v2/events/{id}. Event rows name AP BSSIDs only
-- (refresh_events(), the detectors' subject); the api still redacts every
-- other MAC in summaries and evidence (app/v2/privacy.py). No cursor = the
-- open bounds ('infinity', 2^63 - 1) / ('-infinity', 0).

-- name: _filter
-- Every filter but status and the cursor: sensors, kinds, the lowest
-- severity, managed networks only (the event's network: its AP's SSID or the
-- name seen for a hidden AP), BSSID / SSID search (q: a LIKE pattern,
-- escaped by the api).
FROM detections d
LEFT JOIN ap_summary su ON su.sensor_id = d.sensor_id AND su.device_key = d.device_key
WHERE d.sensor_id = ANY (%(sensor_ids)s::int[])
  AND (cardinality(%(kinds)s::text[]) = 0 OR d.kind = ANY (%(kinds)s::text[]))
  AND (nullif(%(severity)s::text, '') IS NULL
       OR array_position(ARRAY['info', 'low', 'medium', 'high', 'critical'], d.severity)
          >= array_position(ARRAY['info', 'low', 'medium', 'high', 'critical'], %(severity)s::text))
  AND (NOT %(managed)s::boolean
       OR coalesce(nullif(su.ssid, ''), su.name_seen, d.ssid)
          IN (SELECT n.ssid FROM networks n WHERE n.account_id = %(account_id)s AND n.kind = 'managed'))
  AND (nullif(%(q)s::text, '') IS NULL
       OR upper(d.mac::text) LIKE '%%' || upper(%(q)s::text) || '%%'
       OR coalesce(nullif(su.ssid, ''), su.name_seen, d.ssid) ILIKE '%%' || %(q)s::text || '%%')

-- name: _label
coalesce(r.label, CASE p.type WHEN 'deauth_flood' THEN 'Deauthentication flood'
                              WHEN 'evil_twin' THEN 'Possible evil twin (RSSI)'
                              WHEN 'unknown_bssid_managed_ssid' THEN 'Unknown BSSID with managed SSID'
                              WHEN 'security_change' THEN 'Security change'
                              WHEN 'sensor_outage' THEN 'Sensor not collecting'
                              WHEN 'sensor_limited' THEN 'Capture limited' END, p.type)

-- name: _row
-- The row of the feed an event is shown in (display only, decided
-- 2026-10-12): repeated info / low events of one sensor, kind, type,
-- network, status and severity on one local day share a row - per network
-- (decided after the first deploy: per AP, NOCLIENTMFP spread over 22
-- IK-WIFI BSSIDs left 224 rows of 271 events); a hidden AP without a known
-- name groups on its own BSSID. Every other event has its own row. The
-- status is part of it, so a row is one status (its rows count per status)
-- and a bulk label can take the row as it is.
CASE WHEN d.severity IN ('info', 'low') AND d.kind <> 'sensor' AND d.mac IS NOT NULL
     THEN concat_ws('|', d.sensor_id, d.kind, d.type,
                    coalesce(nullif(su.ssid, ''), su.name_seen, d.ssid, upper(d.mac::text)), d.status, d.severity,
                    (d.ts AT TIME ZONE %(tz)s::text)::date)
     ELSE 'e' || d.id END

-- name: counts
-- Per status: the events and the feed rows they make.
SELECT d.status, count(*)::int AS n, count(DISTINCT {{_row}})::int AS rows
{{_filter}}
GROUP BY d.status

-- name: feed
-- One page of rows in the direction of travel (oldest_first: paging to newer
-- events with an `after` cursor), limit = page size + 1 to see whether more
-- follow; returned newest first. A row is placed at its newest event (ts,
-- id), which also leads it; members: the row's events newest first when it
-- has more than one. Classification: account_aps, once per AP of the page.
WITH ev AS MATERIALIZED (
  SELECT d.id, d.ts, {{_row}} AS row_key
  {{_filter}}
    AND (cardinality(%(status)s::text[]) = 0 OR d.status = ANY (%(status)s::text[]))
), feed_rows AS MATERIALIZED (
  SELECT (array_agg(ev.id ORDER BY ev.ts DESC, ev.id DESC))[1] AS id, max(ev.ts) AS ts, min(ev.ts) AS ts_first,
         CASE WHEN count(*) > 1 THEN array_agg(ev.id ORDER BY ev.ts DESC, ev.id DESC) END AS members
  FROM ev
  GROUP BY ev.row_key
), top AS MATERIALIZED (
  SELECT r.id, r.ts_first, r.members FROM feed_rows r
  WHERE (r.ts, r.id) < (%(before_ts)s::timestamptz, %(before_id)s::bigint)
    AND (r.ts, r.id) > (%(after_ts)s::timestamptz, %(after_id)s::bigint)
  ORDER BY CASE WHEN %(oldest_first)s::boolean THEN r.ts END, CASE WHEN %(oldest_first)s::boolean THEN r.id END,
           r.ts DESC, r.id DESC
  LIMIT %(limit)s::int
), page AS MATERIALIZED (
  SELECT d.id, d.ts, d.ts_end, d.severity, d.kind, d.type, d.summary, d.mac, d.ssid, d.status, d.status_at,
         d.status_reason, d.sensor_id, coalesce(nullif(su.ssid, ''), su.name_seen, d.ssid) AS network,
         t.ts_first, t.members
  FROM top t
  JOIN detections d ON d.id = t.id
  LEFT JOIN ap_summary su ON su.sensor_id = d.sensor_id AND su.device_key = d.device_key
), cls AS MATERIALIZED (
  SELECT v.bssid, v.classification FROM account_aps v
  WHERE v.ap_id = ANY (ARRAY(SELECT a.id FROM aps a
                             WHERE a.account_id = %(account_id)s AND a.bssid IN (SELECT mac FROM page)))
)
SELECT p.id, p.ts, p.ts_end, p.severity, p.kind, p.type, {{_label}} AS type_label, p.summary,
       upper(p.mac::text) AS bssid, p.ssid, p.network, c.classification, s.name AS sensor, p.status, p.status_at,
       p.status_reason, p.ts_first, p.members, (p.ts AT TIME ZONE %(tz)s::text)::date AS day
FROM page p
JOIN sensors s ON s.id = p.sensor_id
LEFT JOIN kismet_alert_rules r ON p.kind = 'kismet' AND r.header = p.type
LEFT JOIN cls c ON c.bssid = p.mac
ORDER BY p.ts DESC, p.id DESC

-- name: members
-- The events of the page's grouped rows (feed.members) with their BSSIDs
-- (the APs of one network).
SELECT d.id, d.ts, d.ts_end, d.summary, d.status_at, d.status_reason, upper(d.mac::text) AS bssid
FROM detections d
WHERE d.id = ANY (%(ids)s::bigint[])
ORDER BY d.ts DESC, d.id DESC

-- name: event
SELECT p.id, p.ts, p.ts_end, p.severity, p.kind, p.type, {{_label}} AS type_label, p.summary,
       upper(p.mac::text) AS bssid, p.ssid, coalesce(nullif(su.ssid, ''), su.name_seen, p.ssid) AS network,
       s.name AS sensor, p.status, p.status_at, p.status_reason, p.status_note, p.evidence, p.created_at, p.updated_at,
       a.id AS ap_id, (SELECT v.classification FROM account_aps v WHERE v.ap_id = a.id) AS classification
FROM detections p
JOIN sensors s ON s.id = p.sensor_id AND s.account_id = %(account_id)s
LEFT JOIN ap_summary su ON su.sensor_id = p.sensor_id AND su.device_key = p.device_key
LEFT JOIN kismet_alert_rules r ON p.kind = 'kismet' AND r.header = p.type
LEFT JOIN aps a ON a.account_id = s.account_id AND a.bssid = p.mac
WHERE p.id = %(event_id)s::bigint

-- name: who_saw_it
-- Every sensor that has the event's AP: whether it observed the AP in the
-- hours of the event, the median of its hourly RSSI medians then, the AP's
-- clients then (AP-reported station count when its beacons carried a BSS
-- Load element, else Kismet's count; max), first / last seen.
SELECT se.name AS sensor, su.first_seen, su.last_seen,
       coalesce(sum(h.n_obs), 0)::int > 0 AS seen,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY h.median) AS rssi_median,
       (SELECT CASE WHEN bool_or(l.qbss_polls > 0) THEN max(l.qbss_max) FILTER (WHERE l.qbss_polls > 0)
                    ELSE max(l.clients_max) END
        FROM ap_load_hourly l
        WHERE l.sensor_id = x.sensor_id AND l.device_key = x.device_key
          AND l.hour >= date_trunc('hour', %(ts)s::timestamptz, 'UTC') AND l.hour <= %(ts_end)s::timestamptz) AS clients
FROM ap_sensors x
JOIN sensors se ON se.id = x.sensor_id
JOIN ap_summary su ON su.sensor_id = x.sensor_id AND su.device_key = x.device_key
LEFT JOIN ap_rssi_hourly h ON h.sensor_id = x.sensor_id AND h.device_key = x.device_key
      AND h.hour >= date_trunc('hour', %(ts)s::timestamptz, 'UTC') AND h.hour <= %(ts_end)s::timestamptz
WHERE x.ap_id = %(ap_id)s::bigint
GROUP BY se.id, se.name, su.first_seen, su.last_seen, x.sensor_id, x.device_key
ORDER BY se.id

-- name: labels
-- The current labels of the given events of the account (the old values
-- of a status write, audit_log).
SELECT d.id, d.status, d.status_reason, d.status_note, d.status_at
FROM detections d
JOIN sensors se ON se.id = d.sensor_id AND se.account_id = %(account_id)s
WHERE d.id = ANY (%(ids)s::bigint[])
ORDER BY d.id

-- name: label_history
-- The changes of one event's label, newest first (audit_log 'event.status':
-- every label write keeps the old value next to the new one).
SELECT l.at, l.old, l.new
FROM audit_log l
WHERE l.account_id = %(account_id)s AND l.action = 'event.status' AND l.target = 'event:' || %(event_id)s::text
ORDER BY l.at DESC, l.id DESC

-- name: export_labels
-- The RQ2 dataset: every event of the account, oldest first, with its current
-- outcome (open = not judged yet). No summary or evidence (they can name client
-- MACs); the BSSID only when it is an account AP.
SELECT p.id, p.ts, p.ts_end, se.name AS sensor, p.kind, p.type, {{_label}} AS type_label, p.severity,
       CASE WHEN a.id IS NOT NULL THEN upper(p.mac::text) END AS bssid,
       coalesce(nullif(su.ssid, ''), su.name_seen, p.ssid) AS ssid,
       p.status, p.status_reason, p.status_note, p.status_at
FROM detections p
JOIN sensors se ON se.id = p.sensor_id AND se.account_id = %(account_id)s
LEFT JOIN ap_summary su ON su.sensor_id = p.sensor_id AND su.device_key = p.device_key
LEFT JOIN kismet_alert_rules r ON p.kind = 'kismet' AND r.header = p.type
LEFT JOIN aps a ON a.account_id = se.account_id AND a.bssid = p.mac
ORDER BY p.ts, p.id

-- name: export_history
-- Every change of a label, oldest first (long format: one row per change).
SELECT l.at, split_part(l.target, ':', 2)::bigint AS event_id, d.kind, d.type,
       l.old->>'status' AS old_status, l.old->>'reason' AS old_reason, l.old->>'note' AS old_note,
       l.new->>'status' AS new_status, l.new->>'reason' AS new_reason, l.new->>'note' AS new_note
FROM audit_log l
LEFT JOIN detections d ON d.id = split_part(l.target, ':', 2)::bigint
WHERE l.account_id = %(account_id)s AND l.action = 'event.status'
ORDER BY l.at, l.id
