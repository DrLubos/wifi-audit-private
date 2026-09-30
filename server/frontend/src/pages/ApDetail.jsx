import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useApi } from "../api";
import RssiChart from "../components/RssiChart";
import Skeleton from "../components/Skeleton";
import StatTile from "../components/StatTile";
import { bandOf, fmtDateTime, fmtDb, fmtDbm, fmtDuration, fmtInt } from "../format";

// 1 h = the whole history from the hourly table; 15 min = the raw readings of
// the last 48 h of the AP's data (the api refuses longer raw windows).
const BUCKETS = [
  { s: 3600, label: "1 h" },
  { s: 900, label: "15 min, last 48 h" },
];
const RAW_WINDOW_MS = 48 * 3600 * 1000;

function rssiPath(deviceKey, bucket, lastObs) {
  if (bucket === 3600) return `/api/aps/${deviceKey}/rssi`;
  if (!lastObs) return null;                                  // wait for the AP's last reading
  const to = new Date(new Date(lastObs).getTime() + 1000);    // exclusive bound
  const from = new Date(to.getTime() - RAW_WINDOW_MS);
  return `/api/aps/${deviceKey}/rssi?bucket=900&from=${from.toISOString()}&to=${to.toISOString()}`;
}

// Configuration changes come from ap_config_changes (schema.sql), the cleaned
// reading of the config history: per poll ts only the fields that changed,
// old -> new. Security-relevant fields are marked; static fields (country,
// beacon rate, MFP, hidden SSID) are shown once in the header.
const CHANGE_FIELDS = {
  ssid: { label: "SSID", security: true },
  cloaked: { label: "Hidden", security: true },
  crypt: { label: "Encryption", security: true },
  crypt_bits: { label: "Crypt bits", security: true },
  mfp_sup: { label: "MFP supported", security: true },
  mfp_req: { label: "MFP required", security: true },
  country: { label: "Country", security: true },
  adv_channel: { label: "Channel" },
  ht_mode: { label: "HT" },
  beacon_rate: { label: "Beacon rate" },
};
const FIELD_ORDER = Object.keys(CHANGE_FIELDS);
const byFieldOrder = (a, b) => FIELD_ORDER.indexOf(a.field) - FIELD_ORDER.indexOf(b.field);

// Values arrive as text from the view: booleans 'true'/'false', crypt_bits decimal.
function fmtValue(field, v) {
  if (v === null || v === undefined) return "-";
  if (field === "crypt_bits") return "0x" + BigInt(v).toString(16);
  if (v === "true") return "yes";
  if (v === "false") return "no";
  return v;
}

const fmtMfp = (ap) => (ap.mfp_req ? "required" : ap.mfp_sup ? "supported" : ap.mfp_sup === false ? "no" : "-");

const CHART_HEIGHT = 320;
const RSSI_LOADING = "Loading RSSI history";

// Page-shaped placeholder while /api/aps/{key} loads: same blocks, same heights.
function DetailSkeleton() {
  return (
    <>
      <p className="crumbs"><Link to="/aps">← Access points</Link></p>
      <Skeleton height="1.7rem" width="40%" />
      <div className="ap-head" style={{ marginTop: "0.5rem" }}>
        <Skeleton height="1.1rem" width="70%" />
      </div>
      <div className="tiles skeleton-row" aria-hidden="true">
        {[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} />)}
      </div>
      <div className="chart-card">
        <div className="chart-head"><h2>RSSI over time</h2></div>
        <Skeleton height={CHART_HEIGHT} label={RSSI_LOADING} />
      </div>
    </>
  );
}

export default function ApDetail() {
  const { deviceKey } = useParams();
  const [bucket, setBucket] = useState(3600);
  const detail = useApi(`/api/aps/${deviceKey}`);
  const rssi = useApi(rssiPath(deviceKey, bucket, detail.data?.observations?.last_obs));

  if (detail.error) {
    return (
      <>
        <p className="crumbs"><Link to="/aps">← Access points</Link></p>
        <p className="error">Failed to load: {detail.error}</p>
      </>
    );
  }
  if (!detail.data) return <DetailSkeleton />;

  const { ap, baseline, observations, config_history: history } = detail.data;

  return (
    <>
      <p className="crumbs"><Link to="/aps">← Access points</Link></p>
      <h1>{ap.hidden ? (
        <>
          <span className="muted">&lt;hidden SSID&gt;</span>
          {ap.name_seen ? <span className="muted"> ({ap.name_seen})</span> : null}
        </>
      ) : ap.ssid}</h1>
      <div className="ap-head">
        <span className="mono">{ap.bssid}</span>
        <span>{ap.manuf ?? "unknown manufacturer"} <span className="mono muted">{ap.oui}</span></span>
        <span>{ap.crypt ?? "-"}</span>
        <span>ch {ap.adv_channel ?? "-"}{ap.ht_mode ? ` (${ap.ht_mode})` : ""}
          {baseline?.main_freq_khz ? ` · ${bandOf(baseline.main_freq_khz)}` : ""}</span>
        <span>MFP {fmtMfp(ap)}</span>
        <span>beacon rate {ap.beacon_rate ?? "-"}</span>
        <span>country {ap.country ?? "-"}</span>
        <span>first {fmtDateTime(ap.first_seen)} · last {fmtDateTime(ap.last_seen)} · {fmtDuration(ap.lifetime_s)}</span>
        {ap.country_foreign ? (
          <span className="flag warn"
                title="Advertised country differs from the one most of this sensor's APs advertise: a weak hint of a rogue or misconfigured AP">
            country {ap.country} ≠ {ap.country_expected}
          </span>
        ) : null}
        {ap.hidden_beacon ? <span className="flag">hidden SSID</span> : null}
        {ap.random_bssid ? <span className="flag">randomised BSSID</span> : null}
        {ap.trusted ? <span className="flag">trusted</span> : null}
      </div>

      {baseline ? (
        <div className="tiles">
          <StatTile label="Baseline median" value={fmtDbm(baseline.rssi_median)}
                    hint={`mean ${fmtDbm(baseline.rssi_mean)}`} />
          <StatTile label="Robust sd" value={fmtDb(baseline.rssi_robust_sd)} hint="1.4826 · MAD" />
          <StatTile label="Std dev" value={fmtDb(baseline.rssi_sd)} />
          <StatTile label="p5 .. p95" value={`${baseline.rssi_p5} .. ${baseline.rssi_p95} dBm`} />
          <StatTile label="Readings" value={fmtInt(baseline.n_obs)}
                    hint={`${baseline.n_days} days · ${fmtDateTime(baseline.window_start)} → ${fmtDateTime(baseline.window_end)}`} />
        </div>
      ) : (
        <p className="sub">
          No baseline: {fmtInt(observations.n_rssi)} readings with RSSI
          ({fmtInt(observations.n_obs)} observations); a baseline needs ≥ 200 readings on ≥ 2 days.
        </p>
      )}

      <div className="chart-card">
        <div className="chart-head">
          <h2>RSSI over time</h2>
          <span className="muted">median per bucket, min–max band{baseline ? ", baseline ± 3 robust sigma" : ""}</span>
          <div className="seg" role="group" aria-label="Bucket width">
            {BUCKETS.map((b) => (
              <button key={b.s} type="button" aria-pressed={bucket === b.s}
                      onClick={() => setBucket(b.s)}>{b.label}</button>
            ))}
          </div>
        </div>
        {rssi.error ? <p className="error">Failed to load: {rssi.error}</p> : null}
        {rssi.data ? (
          <RssiChart data={rssi.data} baseline={rssi.data.baseline} loading={rssi.loading}
                     height={CHART_HEIGHT} />
        ) : (!rssi.error ? <Skeleton height={CHART_HEIGHT} label={RSSI_LOADING} /> : null)}
      </div>

      <h2>Configuration changes
        <small>{fmtInt(history.total)} change{history.total === 1 ? "" : "s"}{history.total > history.rows.length ? `, newest ${history.rows.length} shown` : ""}</small>
      </h2>
      {history.rows.length === 0 ? (
        <p className="muted">No configuration change recorded ({fmtInt(history.raw_rows)} raw history rows).</p>
      ) : (
        <div className="table-wrap">
          <table className="small-table">
            <thead>
              <tr><th>When</th><th>Changes (old → new)</th></tr>
            </thead>
            <tbody>
              {history.rows.map((row) => (
                <tr key={row.ts}>
                  <td>{fmtDateTime(row.ts)}</td>
                  <td className="wrap">
                    {[...row.changes].sort(byFieldOrder).map((c) => {
                      const f = CHANGE_FIELDS[c.field] ?? { label: c.field };
                      return (
                        <span key={c.field} className={f.security ? "change security" : "change"}>
                          <span className="label">{f.label}</span>
                          <span className="mono">{fmtValue(c.field, c.old)}</span>
                          <span className="sep"> → </span>
                          <span className="mono">{fmtValue(c.field, c.new)}</span>
                        </span>
                      );
                    })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ padding: "0.3rem 0.6rem", margin: 0 }}>
            Dated when the old value was last seen; security-relevant fields are highlighted.
            {" "}{fmtInt(history.raw_rows)} raw history rows: empty beacon records written before collector v5
            (2026-09-27 19:57 UTC), Kismet's missing-field crypt bits and a hidden AP's alternating hidden/named
            records are not changes (findings §11).
          </p>
        </div>
      )}
    </>
  );
}
