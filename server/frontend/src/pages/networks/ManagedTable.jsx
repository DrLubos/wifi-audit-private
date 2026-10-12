// Managed networks (Networks board): policy vs actual security, compliance
// (BSSIDs of unknown security are not assessed, counted under the badge),
// BSSIDs (active now), vendors, sensors, clients now; a row expands to its
// BSSIDs (channel, security, clients, RSSI by sensor, last seen).

import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { LEVELS } from "../../app/status.js";
import { fmtAgo, fmtDateTime, fmtDbm, fmtInt, toDate } from "../../format.js";
import { actualText, bssidCount, complianceBadge, levelBadge, notAssessedText, vendorList } from "./networks.js";

function channelText(b) {
  if (!b.channel) return "–";
  const ht = b.ht_mode && b.ht_mode !== "HT20" ? ` (${b.ht_mode})` : "";
  return `${b.channel}${b.band ? ` · ${b.band} GHz` : ""}${ht}`;
}

function seen(t, now, tz) {
  const d = toDate(t);
  if (!d) return "–";
  return now - d.getTime() < 3600e3 ? fmtAgo(d, now) : fmtDateTime(d, tz);
}

// "pi-fri 31" (BSSIDs per sensor) or "pi-fri −80" (RSSI, fmt = fmtDbm)
export function SensorChips({ entries, fmt = fmtInt }) {
  return entries.map(([name, v]) => (
    <span className="sen" key={name}>{v == null ? name : `${name} ${fmt(v)}`}</span>
  ));
}

function BssidRows({ rows, now, tz }) {
  return (
    <table className="t nw sub-table">
      <thead>
        <tr><th>BSSID</th><th>Channel</th><th>Security</th><th>Clients</th><th>RSSI by sensor</th><th>Last seen</th></tr>
      </thead>
      <tbody>
        {rows.map((b) => (
          <tr key={b.bssid}>
            <td className="mono">{b.bssid}</td>
            <td>{channelText(b)}</td>
            <td><span className={`b ${levelBadge(b.security)}`} title={LEVELS[b.security]?.label}>{b.label}</span>
              {b.assessed === false ? <span className="muted tiny"> not assessed</span>
                : b.reason && <span className="muted tiny"> {b.reason}</span>}</td>
            <td>{b.clients == null ? "–" : fmtInt(b.clients)}</td>
            <td><SensorChips fmt={fmtDbm} entries={Object.entries(b.rssi).sort((x, y) => (y[1] ?? -999) - (x[1] ?? -999))} /></td>
            <td>{seen(b.last_seen, now, tz)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ManagedTable({ managed, now, tz }) {
  const [open, setOpen] = useState(() => new Set());
  const toggle = (id) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });
  return (
    <section className="card table-card" aria-labelledby="own-h">
      <div className="table-head">
        <h2 id="own-h">Managed networks</h2>
        <span className="muted small">compliance = actual security of every BSSID compared with the network policy</span>
      </div>
      <div className="scroll-x">
        <table className="t nw">
          <thead>
            <tr><th>Network (SSID)</th><th>BSSIDs</th><th>Vendors</th><th>Policy</th><th>Actual</th>
              <th>Compliance</th><th>Sensors</th><th>Clients now</th></tr>
          </thead>
          <tbody>
            {managed.length === 0 && (
              <tr><td colSpan={8} className="muted">No managed network is configured. <Link to="/settings/networks">Settings</Link></td></tr>
            )}
            {managed.map((n) => {
              const isOpen = open.has(n.id);
              const badge = complianceBadge(n);
              const na = notAssessedText(n);
              return (
                <Fragment key={n.id}>
                  <tr className={isOpen ? "expanded" : undefined}>
                    <td>
                      <button type="button" className="row-toggle" aria-expanded={isOpen} onClick={() => toggle(n.id)}
                              disabled={!n.bssid_rows.length}>
                        <span aria-hidden="true">{n.bssid_rows.length ? (isOpen ? "▾ " : "▸ ") : ""}</span>{n.ssid}
                      </button>
                    </td>
                    <td>{bssidCount(n)}</td>
                    <td>{vendorList(n.vendors)}</td>
                    <td>{n.policy.label}</td>
                    <td>{actualText(n.actual)}</td>
                    <td>
                      <span className={`b ${badge.cls}`} title={badge.title}>{badge.text}</span>
                      {na && <span className="na muted tiny" title={na.title}>{na.text}</span>}
                    </td>
                    <td><SensorChips entries={Object.entries(n.sensors)} /></td>
                    <td>{fmtInt(n.clients_now)}</td>
                  </tr>
                  {isOpen && (
                    <tr className="expanded">
                      <td colSpan={8} className="sub"><BssidRows rows={n.bssid_rows} now={now} tz={tz} /></td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
