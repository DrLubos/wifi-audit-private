-- RQ2: one row per event (detections, every kind) with the operator's label
-- and the facts the cause rules read (README.md). No MAC address in any
-- column; MAC-shaped text in the label note is masked.
-- psql variables: sensor, from, to (UTC; events by ts). Read-only.
COPY (
WITH sen AS (
  SELECT id, name, account_id FROM sensors WHERE name = :'sensor'
), aa AS MATERIALIZED (          -- account_aps once (a per-row call costs ~90 ms)
  SELECT DISTINCT ON (a.bssid) a.bssid, a.network_kind, a.classification
  FROM account_aps a JOIN sen ON a.account_id = sen.account_id
  ORDER BY a.bssid, a.sensor_id = (SELECT id FROM sen) DESC
), ev AS (
  SELECT d.*, sen.name AS sensor, sen.account_id, coalesce(d.ts_end, d.ts) AS te
  FROM detections d JOIN sen ON sen.id = d.sensor_id
  WHERE d.ts >= :'from'::timestamptz AND d.ts < :'to'::timestamptz
)
SELECT e.id, e.sensor, e.kind, e.type,
       CASE
         WHEN e.kind = 'detection' AND jsonb_typeof(e.evidence -> 'rules') = 'object' THEN
           e.type || ':' || coalesce((SELECT string_agg(r.key, '+' ORDER BY r.key)
                                      FROM jsonb_each(e.evidence -> 'rules') r
                                      WHERE r.value = 'true'::jsonb), 'none')
         WHEN e.type = 'security_change' THEN
           'security_change:' || CASE
             WHEN security_rank(e.evidence #>> '{to,level}') < security_rank(e.evidence #>> '{from,level}')
               THEN CASE WHEN e.evidence #>> '{to,level}' = 'open' THEN 'downgrade_to_open' ELSE 'downgrade' END
             WHEN security_rank(e.evidence #>> '{to,level}') > security_rank(e.evidence #>> '{from,level}')
               THEN 'upgrade'
             WHEN e.evidence #>> '{to,level}' IS DISTINCT FROM e.evidence #>> '{from,level}' THEN 'unknown_level'
             ELSE 'same_level' END
         ELSE e.type END                                        AS rule,
       e.severity, e.ts, e.ts_end,
       round(extract(epoch FROM e.te - e.ts))::bigint           AS duration_s,
       e.evidence ->> 'alerts'                                  AS n_alerts,
       (e.mac IS NOT NULL OR e.device_key IS NOT NULL)          AS has_subject,
       aa.network_kind,
       e.status, e.status_reason, e.status_at,
       regexp_replace(coalesce(e.status_note, ''), '([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}', '<mac>', 'g') AS note,
       sp.level                                                 AS ap_level_at,
       mfp_state(sp.mfp_sup, sp.mfp_req)                        AS ap_mfp_at,
       EXISTS (SELECT 1 FROM sensor_periods p
               WHERE p.sensor_id = e.sensor_id AND p.kind = 'outage'
                 AND p.starts_at <= e.te AND coalesce(p.ends_at, 'infinity') >= e.ts) AS in_outage,
       EXISTS (SELECT 1 FROM sensor_periods p
               WHERE p.sensor_id = e.sensor_id AND p.kind = 'limited'
                 AND p.starts_at <= e.te AND coalesce(p.ends_at, 'infinity') >= e.ts) AS in_limited,
       (SELECT string_agg(DISTINCT an.kind, '|')
        FROM sensor_annotations an
        WHERE an.sensor_id = e.sensor_id AND an.kind IN ('gap', 'no_frames', 'coverage')
          AND an.starts_at <= e.te AND coalesce(an.ends_at, an.starts_at) >= e.ts) AS degraded_kinds,
       CASE WHEN e.device_key IS NOT NULL THEN
         EXISTS (SELECT 1 FROM ap_config_changes c
                 WHERE c.sensor_id = e.sensor_id AND c.device_key = e.device_key AND c.field = 'adv_channel'
                   AND c.ts BETWEEN e.ts - interval '1 hour' AND e.te + interval '1 hour') END AS channel_change_1h,
       -- a security change undone: the next period has the state the change left
       CASE WHEN e.type = 'security_change' THEN
         (SELECT round(extract(epoch FROM nx.valid_from - e.ts))::bigint
          FROM ap_security_periods cur
          JOIN ap_security_periods nx ON nx.sensor_id = cur.sensor_id AND nx.device_key = cur.device_key
                                     AND nx.valid_from = cur.valid_to
          WHERE cur.sensor_id = e.sensor_id AND cur.device_key = e.device_key AND cur.valid_from = e.ts
            AND nx.crypt IS NOT DISTINCT FROM e.evidence #>> '{from,crypt}'
            AND mfp_state(nx.mfp_sup, nx.mfp_req) IS NOT DISTINCT FROM e.evidence #>> '{from,mfp}') END AS reverted_within_s,
       -- the event called polls at risk that the server already held (event 866)
       CASE WHEN e.type = 'upload_retention_risk' AND e.evidence ? 'oldest_unsent' THEN
         EXISTS (SELECT 1 FROM polls p
                 WHERE p.sensor_id = e.sensor_id
                   AND p.ts >= (e.evidence ->> 'oldest_unsent')::timestamptz
                   AND p.ts < (e.evidence ->> 'oldest_unsent')::timestamptz + interval '1 hour'
                   AND coalesce(p.received_at, '-infinity') <= e.ts) END AS backlog_on_server,
       aa.classification                                        AS classification_now,
       -- the BSSID's OUI is allowed for the network now, but was not in the
       -- allow-list the event recorded
       CASE WHEN e.type = 'unknown_bssid_managed_ssid' AND e.mac IS NOT NULL THEN
         upper(left(trunc(e.mac)::text, 8)) <> ALL (ARRAY(SELECT jsonb_array_elements_text(
                                                              coalesce(e.evidence -> 'allowed_ouis', '[]'))))
         AND EXISTS (SELECT 1 FROM networks n
                     WHERE n.account_id = e.account_id AND n.ssid = coalesce(e.evidence ->> 'network', e.ssid)
                       AND trunc(e.mac) IN (SELECT trunc(o) FROM unnest(n.allowed_ouis) AS o)) END AS oui_allowed_later,
       e.evidence #>> '{rssi,baseline,median}'                  AS baseline_median
FROM ev e
LEFT JOIN aa ON aa.bssid = e.mac
LEFT JOIN LATERAL (SELECT s.level, s.mfp_sup, s.mfp_req
                   FROM ap_security_periods s
                   WHERE s.sensor_id = e.sensor_id AND s.device_key = e.device_key
                     AND s.valid_from <= e.ts AND s.valid_to > e.ts
                   LIMIT 1) sp ON true
ORDER BY e.ts, e.id
) TO STDOUT WITH (FORMAT csv, HEADER);
