-- GET /api/v2/settings/* (the stored rows) and the configured networks the
-- Networks page lists even when none of their BSSIDs was seen.

-- name: networks
SELECT n.id, n.ssid, n.kind, n.expected_security, n.require_mfp,
       ARRAY(SELECT upper(b::text) FROM unnest(n.allowed_bssids) AS b ORDER BY 1) AS allowed_bssids,
       ARRAY(SELECT upper(left(trunc(o)::text, 8)) FROM unnest(n.allowed_ouis) AS o ORDER BY 1) AS allowed_ouis,
       n.note, n.created_at, n.updated_at
FROM networks n
WHERE n.account_id = %(account_id)s
ORDER BY n.kind, n.ssid
