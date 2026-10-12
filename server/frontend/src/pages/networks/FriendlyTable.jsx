// Friendly networks (Networks board): someone else's, known and legitimate -
// not checked, only kept out of the unknown list.

import { Link } from "react-router-dom";
import { fmtDbm, fmtInt } from "../../format.js";
import { SensorChips } from "./ManagedTable.jsx";
import { securityBadge, vendorList } from "./networks.js";

export default function FriendlyTable({ friendly }) {
  return (
    <section className="card table-card" aria-labelledby="fr-h">
      <div className="table-head"><h2 id="fr-h">Friendly networks</h2></div>
      <div className="scroll-x">
        <table className="t nw">
          <thead>
            <tr><th>Network (SSID)</th><th>BSSIDs</th><th>Vendors</th><th>Security</th><th>Sensors</th><th>Note</th></tr>
          </thead>
          <tbody>
            {friendly.length === 0 && (
              <tr>
                <td colSpan={6} className="muted" style={{ whiteSpace: "normal" }}>
                  No friendly network is configured. They are set in <Link to="/settings/networks">Settings</Link>.
                </td>
              </tr>
            )}
            {friendly.map((n) => {
              const sec = securityBadge(n.security);
              return (
                <tr key={n.id}>
                  <td className="name">{n.ssid}</td>
                  <td>{fmtInt(n.bssids)}</td>
                  <td>{vendorList(n.vendors)}</td>
                  <td>{n.bssids ? <span className={`b ${sec.cls}`}>{sec.text}</span> : "–"}</td>
                  <td>{n.bssids ? <SensorChips fmt={fmtDbm} entries={Object.entries(n.rssi).sort((a, b) => b[1] - a[1])} />
                    : <span className="muted">not seen in the period</span>}</td>
                  <td className="muted">{n.note ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
