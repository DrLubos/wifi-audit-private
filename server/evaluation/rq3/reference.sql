-- RQ3: the continuous record over [from, to) - what the simulated audits are
-- compared with (README.md). Long format: metric, key, n.
-- psql variables: sensor, from, to. Read-only.
COPY (
WITH sen AS (SELECT id FROM sensors WHERE name = :'sensor'),
-- apref {
ch AS (                      -- collecting hours of the range
  SELECT count(*) AS n FROM sensor_hourly s, sen
  WHERE s.sensor_id = sen.id AND s.capture_polls > 0
    AND s.hour >= :'from'::timestamptz AND s.hour < :'to'::timestamptz
), apref AS MATERIALIZED (   -- every AP heard in the range: presence class and band
  SELECT h.device_key, count(*) AS hours,
         CASE WHEN count(*) <= 2 THEN 'transient'
              WHEN count(*) >= 0.9 * ch.n THEN 'permanent'
              WHEN count(*) >= 0.1 * ch.n THEN 'regular'
              ELSE 'occasional' END AS class,
         coalesce(min(b.band), 'unknown') AS band
  FROM ap_rssi_hourly h CROSS JOIN sen CROSS JOIN ch
  LEFT JOIN (SELECT a.device_key, a.band FROM account_aps a, sensors s2
             WHERE s2.name = :'sensor' AND a.sensor_id = s2.id) b ON b.device_key = h.device_key
  WHERE h.sensor_id = sen.id AND h.n_obs > 0
    AND h.hour >= :'from'::timestamptz AND h.hour < :'to'::timestamptz
  GROUP BY h.device_key, ch.n
),
-- apref }
sp AS (
  SELECT cur.valid_from, prev.level IS DISTINCT FROM cur.level AS level_changed
  FROM ap_security_periods cur
  JOIN sen ON sen.id = cur.sensor_id
  LEFT JOIN ap_security_periods prev ON prev.sensor_id = cur.sensor_id AND prev.device_key = cur.device_key
                                    AND prev.valid_to = cur.valid_from
  WHERE cur.valid_from > '-infinity'
    AND cur.valid_from >= :'from'::timestamptz AND cur.valid_from < :'to'::timestamptz
)
SELECT 'aps' AS metric, 'all' AS key, count(*) AS n FROM apref
UNION ALL SELECT 'aps_class', class, count(*) FROM apref GROUP BY class
UNION ALL SELECT 'aps_band', band, count(*) FROM apref GROUP BY band
UNION ALL SELECT 'hours', 'collecting', n FROM ch
UNION ALL
SELECT 'polls', 'collecting', count(*)
FROM polls p, sen
WHERE p.sensor_id = sen.id AND p.ok AND p.ds_running AND p.devices_active > 0
  AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz
UNION ALL
SELECT 'cfg', CASE WHEN c.field IN ('crypt', 'crypt_bits', 'mfp_sup', 'mfp_req') THEN 'security'
                   WHEN c.field IN ('adv_channel', 'ht_mode') THEN 'channel'
                   WHEN c.field IN ('ssid', 'cloaked') THEN 'identity' ELSE 'other' END, count(*)
FROM ap_config_changes c, sen
WHERE c.sensor_id = sen.id AND c.ts >= :'from'::timestamptz AND c.ts < :'to'::timestamptz
GROUP BY 2
UNION ALL SELECT 'sec_transitions', 'all', count(*) FROM sp
UNION ALL SELECT 'sec_transitions', 'level', count(*) FILTER (WHERE level_changed) FROM sp
UNION ALL
SELECT 'events', d.kind, count(*)
FROM detections d, sen
WHERE d.sensor_id = sen.id AND d.kind <> 'sensor'
  AND d.ts >= :'from'::timestamptz AND d.ts < :'to'::timestamptz
GROUP BY d.kind
) TO STDOUT WITH (FORMAT csv, HEADER);
