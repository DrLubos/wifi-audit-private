import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useApi } from "../api";
import RssiChart from "../components/RssiChart";
import Skeleton from "../components/Skeleton";
import StatTile from "../components/StatTile";
import { bandOf, fmtDateTime, fmtDb, fmtDbm, fmtDuration, fmtInt } from "../format";

const BUCKETS = [
  { s: 900, label: "15 min" },
  { s: 3600, label: "1 h" },
  { s: 21600, label: "6 h" },
];

// Config-history rows hold the configuration that was REPLACED at ts. The
// state that replaced it is the next-newer row, or the current config for the
// newest row; cells that differ from that are highlighted.
const HISTORY_FIELDS = [
  ["ssid", "SSID"], ["crypt", "Encryption"], ["adv_channel", "Channel"], ["ht_mode", "HT"],
  ["beacon_rate", "Beacon rate"], ["country", "Country"], ["cloaked", "Cloaked"],
  ["mfp_sup", "MFP sup."], ["mfp_req", "MFP req."],
];

const CHART_HEIGHT = 320;
// The first view of an AP reads its whole history from disk; say so instead of
// looking stuck.
const RSSI_LOADING = "Loading RSSI history — the first view of an access point reads its full history";

const show = (v) => (v === true ? "yes" : v === false ? "no" : v ?? "-");

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
  const rssi = useApi(`/api/aps/${deviceKey}/rssi?bucket=${bucket}`);

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
  const current = ap;

  return (
    <>
      <p className="crumbs"><Link to="/aps">← Access points</Link></p>
      <h1>{ap.hidden ? <span className="muted">&lt;hidden SSID&gt;</span> : ap.ssid}</h1>
      <div className="ap-head">
        <span className="mono">{ap.bssid}</span>
        <span>{ap.manuf ?? "unknown manufacturer"} <span className="mono muted">{ap.oui}</span></span>
        <span>{ap.crypt ?? "-"}</span>
        <span>ch {ap.adv_channel ?? "-"}{ap.ht_mode ? ` (${ap.ht_mode})` : ""}
          {baseline?.main_freq_khz ? ` · ${bandOf(baseline.main_freq_khz)}` : ""}</span>
        <span>first {fmtDateTime(ap.first_seen)} · last {fmtDateTime(ap.last_seen)} · {fmtDuration(ap.lifetime_s)}</span>
        {ap.random_bssid ? <span className="flag">randomised BSSID</span> : null}
        {ap.trusted ? <span className="flag">trusted</span> : null}
        {ap.mfp_req ? <span className="flag">MFP required</span> : null}
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

      <h2>Configuration history
        <small>{fmtInt(history.total)} changes{history.total > history.rows.length ? `, newest ${history.rows.length} shown` : ""}</small>
      </h2>
      {history.rows.length === 0 ? (
        <p className="muted">No configuration change recorded.</p>
      ) : (
        <div className="table-wrap">
          <table className="small-table">
            <thead>
              <tr><th>Replaced at</th>{HISTORY_FIELDS.map(([k, label]) => <th key={k}>{label}</th>)}</tr>
            </thead>
            <tbody>
              {history.rows.map((row, i) => {
                const newer = i === 0 ? current : history.rows[i - 1];
                return (
                  <tr key={row.ts}>
                    <td>{fmtDateTime(row.ts)}</td>
                    {HISTORY_FIELDS.map(([k]) => (
                      <td key={k} className={(row[k] ?? null) !== (newer[k] ?? null) ? "changed" : ""}>
                        {show(row[k])}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="muted" style={{ padding: "0.3rem 0.6rem", margin: 0 }}>
            Each row is the configuration that was replaced at that time; highlighted cells differ from what followed.
          </p>
        </div>
      )}
    </>
  );
}
