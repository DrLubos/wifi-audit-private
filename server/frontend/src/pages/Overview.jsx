import { Link } from "react-router-dom";
import { useApi } from "../api";
import StatTile from "../components/StatTile";
import DataTable from "../components/DataTable";
import { fmtDateTime, fmtDb, fmtDuration, fmtInt, fmtNum, fmtPct } from "../format";

function Section({ title, children, hint }) {
  return (
    <section>
      <h2>{title}{hint ? <small>{hint}</small> : null}</h2>
      {children}
    </section>
  );
}

function Status({ state, children }) {
  if (state.error) return <p className="error">Failed to load: {state.error}</p>;
  if (!state.data) return <p className="muted">Loading…</p>;
  return children(state.data);
}

const ALERT_COLUMNS = [
  { key: "ts", label: "Time", render: (a) => fmtDateTime(a.ts) },
  { key: "header", label: "Alert" },
  { key: "class", label: "Class" },
  { key: "severity", label: "Sev.", align: "right" },
  { key: "source_mac", label: "Source", className: "mono" },
  { key: "dest_mac", label: "Destination", className: "mono" },
  { key: "channel", label: "Ch" },
  { key: "device_ssid", label: "Device",
    render: (a) => (a.device_key
      ? <Link to={`/aps/${a.device_key}`}>{a.device_ssid || (a.device_type === "ap" ? "<hidden>" : a.device_type || a.device_key)}</Link>
      : "-") },
  { key: "text", label: "Text", className: "wrap" },
];

export default function Overview() {
  const overview = useApi("/api/overview");
  const findings = useApi("/api/findings");
  const alerts = useApi("/api/alerts?limit=50");

  return (
    <>
      <h1>Overview</h1>
      <Status state={overview}>
        {(o) => (
          <>
            <p className="sub">
              Sensor <b>{o.sensor.name}</b>{o.sensor.location ? ` · ${o.sensor.location}` : ""} · {o.sensor.tz}
              {" · capture "}{fmtDateTime(o.capture.first_poll)} → {fmtDateTime(o.capture.last_poll)}
              {" ("}{fmtDuration(o.capture.span_s)}{")"}
            </p>
            <div className="tiles">
              <StatTile label="Polls" value={fmtInt(o.polls.total)} hint={`${fmtInt(o.polls.ok)} ok, every 30 s`} />
              <StatTile label="Coverage" value={fmtPct(o.polls.coverage_pct)} hint="successful polls" />
              <StatTile label="Gaps > 90 s" value={fmtInt(o.polls.gaps.count)}
                        hint={o.polls.gaps.count ? `${fmtDuration(o.polls.gaps.total_s)} total, largest ${fmtDuration(o.polls.gaps.max_s)}` : "none"} />
              <StatTile label="Observations" value={fmtInt(o.observations)} hint="device × poll rows" />
              <StatTile label="Access points" value={fmtInt(o.devices.ap)} hint={<Link to="/aps">inventory</Link>} />
              <StatTile label="Clients" value={fmtInt(o.devices.client)} />
              <StatTile label="Bridged" value={fmtInt(o.devices.bridged)} hint={`${fmtInt(o.devices.other)} other, ${fmtInt(o.devices.total)} devices`} />
              <StatTile label="Kismet alerts" value={fmtInt(o.alerts.total)} hint={o.alerts.last_ts ? `last ${fmtDateTime(o.alerts.last_ts)}` : ""} />
              <StatTile label="AP baselines" value={fmtInt(o.baselines)} hint="≥ 200 readings on ≥ 2 days" />
            </div>
            {o.polls.gaps.list.length ? (
              <details>
                <summary className="muted">Largest gaps</summary>
                <ul className="muted">
                  {o.polls.gaps.list.map((g) => (
                    <li key={g.gap_start}>{fmtDateTime(g.gap_start)} → {fmtDateTime(g.gap_end)} ({fmtDuration(g.gap_s)})</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )}
      </Status>

      <Section title="RSSI stability of fixed APs"
               hint="one baseline per AP: median and MAD-robust sigma of its readings">
        <Status state={findings}>
          {(f) => (
            <>
              <div className="tiles">
                <StatTile label="APs with a baseline" value={fmtInt(f.aps)}
                          hint={`≥ ${f.criteria.min_obs} readings on ≥ ${f.criteria.min_days} days · ${fmtInt(f.readings)} readings`} />
                <StatTile label="Median std dev" value={fmtDb(f.sd.median)}
                          hint={`Q3 ${fmtDb(f.sd.q3)} · p90 ${fmtDb(f.sd.p90)}`} />
                <StatTile label="Median robust sd" value={fmtDb(f.robust_sd.median)}
                          hint={`Q3 ${fmtDb(f.robust_sd.q3)} · p90 ${fmtDb(f.robust_sd.p90)}`} />
              </div>
              <div className="inline-tables">
                <div className="table-wrap">
                  <table className="small-table">
                    <thead>
                      <tr><th>APs whose RSSI spread is within</th>
                        {f.within.map((w) => <th key={w.db} className="num">{w.db} dB</th>)}</tr>
                    </thead>
                    <tbody>
                      <tr><td>std dev</td>{f.within.map((w) => (
                        <td key={w.db} className="num">{fmtInt(w.sd)} ({fmtNum(100 * w.sd / f.aps, 0)} %)</td>))}</tr>
                      <tr><td>robust sd (1.4826 · MAD)</td>{f.within.map((w) => (
                        <td key={w.db} className="num">{fmtInt(w.robust_sd)} ({fmtNum(100 * w.robust_sd / f.aps, 0)} %)</td>))}</tr>
                    </tbody>
                  </table>
                </div>
                <div className="table-wrap">
                  <table className="small-table">
                    <thead>
                      <tr><th>Band</th><th className="num">APs</th><th className="num">median sd</th>
                          <th className="num">median robust sd</th><th className="num">sd ≤ 3 dB</th><th className="num">sd ≤ 5 dB</th></tr>
                    </thead>
                    <tbody>
                      {f.by_band.map((b) => (
                        <tr key={b.band}>
                          <td>{b.band} GHz</td>
                          <td className="num">{fmtInt(b.aps)}</td>
                          <td className="num">{fmtDb(b.sd_median)}</td>
                          <td className="num">{fmtDb(b.rsd_median)}</td>
                          <td className="num">{fmtInt(b.sd_le_3)} ({fmtNum(100 * b.sd_le_3 / b.aps, 0)} %)</td>
                          <td className="num">{fmtInt(b.sd_le_5)} ({fmtNum(100 * b.sd_le_5 / b.aps, 0)} %)</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              <p className="muted">
                Baselines computed {fmtDateTime(f.computed_at)}. A reading is Kismet's last-frame RSSI at
                poll time, one per 30 s poll in which the AP was active.
              </p>
            </>
          )}
        </Status>
      </Section>

      <Section title="Kismet alerts" hint="the sensor's native WIDS engine">
        <Status state={alerts}>
          {(a) => (
            <>
              <p className="sub">
                {fmtInt(a.total)} alerts ·{" "}
                {Object.entries(a.by_header).map(([h, n]) => `${h} ${n}`).join(" · ") || "none"}
              </p>
              <DataTable columns={ALERT_COLUMNS} rows={a.alerts} rowKey={(r) => r.id}
                         initialSort={{ key: "ts", dir: "desc" }} empty="No alerts." />
            </>
          )}
        </Status>
      </Section>
    </>
  );
}
