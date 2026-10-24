-- Dataset overview: the collection period, polls, coverage, volumes (README.md).
-- Long format: metric, value, unit, note. psql variables: sensor, from, to (UTC). Read-only.
COPY (
WITH sen AS (SELECT id FROM sensors WHERE name = :'sensor'),
pl AS (
  SELECT p.* FROM polls p, sen
  WHERE p.sensor_id = sen.id AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz
), span AS (
  SELECT min(ts) AS first_poll, max(ts) AS last_poll, extract(epoch FROM max(ts) - min(ts)) AS span_s FROM pl
), per AS (                  -- sensor_periods clipped to the polls' span
  SELECT sp.kind, count(*) AS n,
         sum(extract(epoch FROM least(coalesce(sp.ends_at, span.last_poll), span.last_poll)
                                - greatest(sp.starts_at, span.first_poll))) AS s
  FROM sensor_periods sp, sen, span
  WHERE sp.sensor_id = sen.id AND sp.starts_at < span.last_poll
    AND coalesce(sp.ends_at, span.last_poll) > span.first_poll
  GROUP BY sp.kind
), live AS (
  SELECT count(*) AS n FROM pl
  JOIN sensor_uploads u ON u.sensor_id = pl.sensor_id AND u.received_at = pl.received_at AND u.via = 'ingest'
), hrs AS (
  SELECT count(*) FILTER (WHERE h.capture_polls > 0) AS collecting, sum(h.ap_obs) AS ap_obs
  FROM sensor_hourly h, sen
  WHERE h.sensor_id = sen.id AND h.hour >= :'from'::timestamptz AND h.hour < :'to'::timestamptz
)
SELECT * FROM (VALUES
  ('first_poll', (SELECT to_char(first_poll AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') FROM span), 'UTC', ''),
  ('last_poll', (SELECT to_char(last_poll AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') FROM span), 'UTC', ''),
  ('span_days', (SELECT round((span_s / 86400)::numeric, 2)::text FROM span), 'days', 'first to last poll'),
  ('polls', (SELECT count(*)::text FROM pl), 'polls', 'every 30 s'),
  ('polls_expected', (SELECT floor(span_s / 30 + 1)::text FROM span), 'polls', 'span / 30 s'),
  ('polls_ok', (SELECT count(*) FILTER (WHERE ok)::text FROM pl), 'polls', 'the collector reached Kismet'),
  ('polls_collecting', (SELECT count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0)::text FROM pl),
   'polls', 'ok, datasource running, devices > 0'),
  ('polls_limited', (SELECT count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0 AND ds_hop_ok = false)::text FROM pl),
   'polls', 'collecting, hop list degraded (known from buffer v4, 2026-09-27)'),
  ('polls_live', (SELECT n::text FROM live), 'polls', 'uploaded by the live ingest (from 2026-10-17)'),
  ('polls_snapshot', (SELECT (count(*) - (SELECT n FROM live))::text FROM pl), 'polls', 'loaded by snapshot imports'),
  ('polls_clock_corrected', (SELECT count(ts_offset_s)::text FROM pl), 'polls', 'placed on the true time line by the uploader'),
  ('outages', (SELECT coalesce((SELECT n FROM per WHERE kind = 'outage'), 0)::text), 'periods', 'sensor_periods outage'),
  ('outage_days', (SELECT round((coalesce((SELECT s FROM per WHERE kind = 'outage'), 0) / 86400)::numeric, 2)::text),
   'days', 'not collecting'),
  ('limited_periods', (SELECT coalesce((SELECT n FROM per WHERE kind = 'limited'), 0)::text), 'periods', 'sensor_periods limited'),
  ('limited_hours', (SELECT round((coalesce((SELECT s FROM per WHERE kind = 'limited'), 0) / 3600)::numeric, 2)::text),
   'hours', 'hop list degraded'),
  ('coverage', (SELECT round((1 - coalesce((SELECT s FROM per WHERE kind = 'outage'), 0) / nullif(span_s, 0))::numeric, 4)::text FROM span),
   'ratio', '1 - outage time / span'),
  ('collecting_hours', (SELECT collecting::text FROM hrs), 'hours', 'UTC hours with a collecting poll'),
  ('seq_lost', (SELECT coalesce(sum(polls), 0)::text FROM sensor_seq_gaps g, sen WHERE g.sensor_id = sen.id AND g.reason = 'lost'),
   'polls', 'pruned on the sensor before upload'),
  ('seq_unverified', (SELECT coalesce(sum(polls), 0)::text FROM sensor_seq_gaps g, sen WHERE g.sensor_id = sen.id AND g.reason = 'unverified'),
   'polls', 'boots without a clock anchor, never uploaded'),
  ('ap_observation_rows', (SELECT ap_obs::text FROM hrs), 'rows', 'AP observation rows on the server (only APs are kept)'),
  ('config_history_rows', (SELECT count(*)::text FROM device_config_history c, sen
                           WHERE c.sensor_id = sen.id AND c.ts >= :'from'::timestamptz AND c.ts < :'to'::timestamptz),
   'rows', 'raw history, incl. the pre-v5 churn'),
  ('config_changes', (SELECT count(*)::text FROM ap_config_changes c, sen
                      WHERE c.sensor_id = sen.id AND c.ts >= :'from'::timestamptz AND c.ts < :'to'::timestamptz),
   'changes', 'ap_config_changes (the rules of findings 11)'),
  ('security_changes', (SELECT count(*)::text FROM ap_security_periods s, sen
                        WHERE s.sensor_id = sen.id AND s.valid_from > '-infinity'
                          AND s.valid_from >= :'from'::timestamptz AND s.valid_from < :'to'::timestamptz),
   'periods', 'new ap_security_periods'),
  ('alerts', (SELECT count(*)::text FROM alerts a, sen
              WHERE a.sensor_id = sen.id AND a.ts >= :'from'::timestamptz AND a.ts < :'to'::timestamptz),
   'alerts', 'Kismet WIDS alerts (incl. SOURCEOPEN/SOURCEERROR)'),
  ('ap_load_hours', (SELECT count(*)::text FROM ap_load_hourly l, sen
                     WHERE l.sensor_id = sen.id AND l.hour >= :'from'::timestamptz AND l.hour < :'to'::timestamptz),
   'rows', 'per AP, hour and channel'),
  ('adapter', (SELECT coalesce(string_agg(DISTINCT coalesce(a.chipset, a.driver, '?') || ' ' || coalesce(a.usb_id, ''), '; '), '')
               FROM sensor_adapters a, sen WHERE a.sensor_id = sen.id), '', 'sensor_adapters')
) AS t(metric, value, unit, note)
) TO STDOUT WITH (FORMAT csv, HEADER);
