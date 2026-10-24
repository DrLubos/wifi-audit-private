-- Dataset overview: APs and networks heard in the range, per band and
-- security level (the AP's current configuration: account_aps). A network is
-- an SSID (or the name a hidden AP revealed); hidden APs without a name are
-- counted as APs only. Totals: band '*' / security '*'. psql variables:
-- sensor, from, to. Read-only.
COPY (
WITH a AS MATERIALIZED (
  SELECT coalesce(aa.band, 'unknown') AS band, coalesce(aa.security, 'unknown') AS security,
         aa.network_name, aa.hidden, aa.random_bssid, aa.mobile, aa.classification
  FROM account_aps aa
  JOIN sensors s ON s.id = aa.sensor_id
  JOIN ap_summary su ON su.sensor_id = aa.sensor_id AND su.device_key = aa.device_key
  WHERE s.name = :'sensor' AND su.first_obs < :'to'::timestamptz AND su.last_obs >= :'from'::timestamptz
)
SELECT coalesce(band, '*') AS band, coalesce(security, '*') AS security,
       count(*) AS aps,
       count(DISTINCT network_name) AS networks,
       count(*) FILTER (WHERE hidden) AS hidden_aps,
       count(*) FILTER (WHERE hidden AND network_name IS NULL) AS hidden_unnamed_aps,
       count(*) FILTER (WHERE random_bssid) AS random_bssid_aps,
       count(*) FILTER (WHERE mobile) AS mobile_aps,
       count(*) FILTER (WHERE classification = 'managed') AS managed_aps,
       count(*) FILTER (WHERE classification = 'friendly') AS friendly_aps,
       count(*) FILTER (WHERE classification = 'suspicious') AS suspicious_aps,
       count(*) FILTER (WHERE classification = 'ignored') AS ignored_aps
FROM a
GROUP BY GROUPING SETS ((band, security), (band), (security), ())
ORDER BY band NULLS LAST, security NULLS LAST
) TO STDOUT WITH (FORMAT csv, HEADER);
