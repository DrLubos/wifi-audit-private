-- RQ3: repeat audits - the same one-shot audit at t1 and again at t2 (README.md).
-- For the APs heard both times: how many changed their security
-- configuration / advertised channel in (t1, t2] by the continuous record,
-- and how many the two audits see as changed (state at t1 <> state at t2).
-- psql variables: sensor, t1s, t2s (timestamptz[] literals of equal length),
-- dur_s, gap_s. Read-only.
COPY (
WITH sen AS (SELECT id FROM sensors WHERE name = :'sensor'),
p AS (SELECT a.i, a.t1, b.t2
      FROM unnest(:'t1s'::timestamptz[]) WITH ORDINALITY AS a(t1, i)
      JOIN unnest(:'t2s'::timestamptz[]) WITH ORDINALITY AS b(t2, i) USING (i)),
w AS (SELECT p.i * 10 + 1 AS wid, p.t1 AS t0, p.t1 + make_interval(secs => :'dur_s'::integer) AS t1 FROM p
      UNION ALL
      SELECT p.i * 10 + 2, p.t2, p.t2 + make_interval(secs => :'dur_s'::integer) FROM p),
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
s AS (SELECT wid / 10 AS i, wid % 10 AS side, device_key FROM seen),
both_ AS (SELECT s1.i, s1.device_key FROM s s1
          JOIN s s2 ON s2.i = s1.i AND s2.side = 2 AND s2.device_key = s1.device_key
          WHERE s1.side = 1),
ap AS (
  SELECT b.i, b.device_key,
         (SELECT count(*) FROM ap_security_periods x
          WHERE x.sensor_id = sen.id AND x.device_key = b.device_key
            AND x.valid_from > p.t1 AND x.valid_from <= p.t2) AS sec_tr,
         (SELECT ROW(x.crypt, x.crypt_bits, x.mfp_sup, x.mfp_req) FROM ap_security_periods x
          WHERE x.sensor_id = sen.id AND x.device_key = b.device_key
            AND x.valid_from <= p.t1 AND x.valid_to > p.t1 LIMIT 1)
           IS DISTINCT FROM
         (SELECT ROW(x.crypt, x.crypt_bits, x.mfp_sup, x.mfp_req) FROM ap_security_periods x
          WHERE x.sensor_id = sen.id AND x.device_key = b.device_key
            AND x.valid_from <= p.t2 AND x.valid_to > p.t2 LIMIT 1) AS sec_det,
         (SELECT count(*) FROM ap_config_changes c
          WHERE c.sensor_id = sen.id AND c.device_key = b.device_key AND c.field = 'adv_channel'
            AND c.ts > p.t1 AND c.ts <= p.t2) AS ch_tr,
         ch1.v IS DISTINCT FROM ch2.v AS ch_det
  FROM both_ b JOIN p USING (i) CROSS JOIN sen
  -- the advertised channel at t: the last change up to t, else the old value of
  -- the first later change, else the current value (ap_channel_at()'s reading)
  CROSS JOIN LATERAL (SELECT coalesce(
      (SELECT c.new_value FROM ap_config_changes c WHERE c.sensor_id = sen.id AND c.device_key = b.device_key
         AND c.field = 'adv_channel' AND c.ts <= p.t1 ORDER BY c.ts DESC LIMIT 1),
      (SELECT c.old_value FROM ap_config_changes c WHERE c.sensor_id = sen.id AND c.device_key = b.device_key
         AND c.field = 'adv_channel' AND c.ts > p.t1 ORDER BY c.ts LIMIT 1),
      (SELECT d.adv_channel FROM devices d WHERE d.sensor_id = sen.id AND d.device_key = b.device_key)) AS v) ch1
  CROSS JOIN LATERAL (SELECT coalesce(
      (SELECT c.new_value FROM ap_config_changes c WHERE c.sensor_id = sen.id AND c.device_key = b.device_key
         AND c.field = 'adv_channel' AND c.ts <= p.t2 ORDER BY c.ts DESC LIMIT 1),
      (SELECT c.old_value FROM ap_config_changes c WHERE c.sensor_id = sen.id AND c.device_key = b.device_key
         AND c.field = 'adv_channel' AND c.ts > p.t2 ORDER BY c.ts LIMIT 1),
      (SELECT d.adv_channel FROM devices d WHERE d.sensor_id = sen.id AND d.device_key = b.device_key)) AS v) ch2
)
SELECT :'gap_s'::integer AS gap_s,
       to_char(p.t1 AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS t1,
       to_char(p.t2 AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS t2,
       (SELECT count(*) FROM s WHERE s.i = p.i AND s.side = 1) AS aps_t1,
       (SELECT count(*) FROM s WHERE s.i = p.i AND s.side = 2) AS aps_t2,
       (SELECT count(*) FROM both_ b WHERE b.i = p.i) AS aps_both,
       (SELECT count(*) FROM ap WHERE ap.i = p.i AND ap.sec_tr > 0) AS sec_aps_changed,
       (SELECT coalesce(sum(ap.sec_tr), 0) FROM ap WHERE ap.i = p.i) AS sec_transitions,
       (SELECT count(*) FROM ap WHERE ap.i = p.i AND ap.sec_det) AS sec_aps_detected,
       (SELECT count(*) FROM ap WHERE ap.i = p.i AND ap.ch_tr > 0) AS ch_aps_changed,
       (SELECT coalesce(sum(ap.ch_tr), 0) FROM ap WHERE ap.i = p.i) AS ch_transitions,
       (SELECT count(*) FROM ap WHERE ap.i = p.i AND ap.ch_det) AS ch_aps_detected
FROM p
ORDER BY p.i
) TO STDOUT WITH (FORMAT csv, HEADER);
