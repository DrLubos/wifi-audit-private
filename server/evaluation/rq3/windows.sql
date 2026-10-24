-- RQ3: what each simulated one-shot audit [t0, t0 + dur_s) saw (README.md).
-- Long format: t0, metric, key, n (a missing row = 0).
-- psql variables: sensor, from, to (the range of the presence classes),
-- starts (a timestamptz[] literal), dur_s. Read-only.
COPY (
WITH sen AS (SELECT id FROM sensors WHERE name = :'sensor'),
w AS (SELECT x AS wid, x AS t0, x + make_interval(secs => :'dur_s'::integer) AS t1
      FROM unnest(:'starts'::timestamptz[]) AS x),
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
-- seen {
seen AS MATERIALIZED (       -- the APs heard in each window: candidates from the hourly
  SELECT w.wid, c.device_key --   rollup, then an index-only probe of observations_pkey
  FROM w CROSS JOIN sen
  JOIN LATERAL (SELECT DISTINCT h.device_key FROM ap_rssi_hourly h
                WHERE h.sensor_id = sen.id AND h.n_obs > 0
                  AND h.hour >= date_trunc('hour', w.t0) AND h.hour < w.t1) c ON true
  WHERE EXISTS (SELECT 1 FROM observations o
                WHERE o.sensor_id = sen.id AND o.device_key = c.device_key
                  AND o.ts >= w.t0 AND o.ts < w.t1)
),
-- seen }
out AS (
  SELECT s.wid, 'aps' AS metric, 'all' AS key, count(*) AS n FROM seen s GROUP BY s.wid
  UNION ALL SELECT s.wid, 'aps_class', coalesce(a.class, 'none'), count(*)
            FROM seen s LEFT JOIN apref a USING (device_key) GROUP BY 1, 3
  UNION ALL SELECT s.wid, 'aps_band', coalesce(a.band, 'none'), count(*)
            FROM seen s LEFT JOIN apref a USING (device_key) GROUP BY 1, 3
  UNION ALL
  SELECT w.wid, 'polls', 'collecting', count(p.*)
  FROM w CROSS JOIN sen
  LEFT JOIN polls p ON p.sensor_id = sen.id AND p.ts >= w.t0 AND p.ts < w.t1
                   AND p.ok AND p.ds_running AND p.devices_active > 0
  GROUP BY w.wid
  UNION ALL
  SELECT w.wid, 'cfg', CASE WHEN c.field IN ('crypt', 'crypt_bits', 'mfp_sup', 'mfp_req') THEN 'security'
                            WHEN c.field IN ('adv_channel', 'ht_mode') THEN 'channel'
                            WHEN c.field IN ('ssid', 'cloaked') THEN 'identity' ELSE 'other' END, count(*)
  FROM w CROSS JOIN sen
  JOIN ap_config_changes c ON c.sensor_id = sen.id AND c.ts >= w.t0 AND c.ts < w.t1
  GROUP BY 1, 3
  UNION ALL
  SELECT w.wid, 'sec_transitions', x.key, count(*)
  FROM w CROSS JOIN sen
  JOIN ap_security_periods cur ON cur.sensor_id = sen.id AND cur.valid_from >= w.t0 AND cur.valid_from < w.t1
  LEFT JOIN ap_security_periods prev ON prev.sensor_id = cur.sensor_id AND prev.device_key = cur.device_key
                                    AND prev.valid_to = cur.valid_from
  CROSS JOIN LATERAL (VALUES ('all'), (CASE WHEN prev.level IS DISTINCT FROM cur.level THEN 'level' END)) x(key)
  WHERE x.key IS NOT NULL
  GROUP BY 1, 3
  UNION ALL
  SELECT w.wid, 'events', d.kind, count(*)
  FROM w CROSS JOIN sen
  JOIN detections d ON d.sensor_id = sen.id AND d.kind <> 'sensor'
                   AND d.ts < w.t1 AND coalesce(d.ts_end, d.ts) >= w.t0
  GROUP BY 1, 3
)
SELECT to_char(o.wid AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS t0, o.metric, o.key, o.n
FROM out o
ORDER BY 1, 2, 3
) TO STDOUT WITH (FORMAT csv, HEADER);
