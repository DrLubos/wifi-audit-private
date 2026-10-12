// Other networks nearby (Networks board): every network that is not
// configured, phone hotspots (randomized BSSIDs) as one summary row, the
// rest by BSSID count with presence (SPEC Q9; transient ones with their
// median sighting), paged by the api; the "Managed" / "Friendly" actions
// need the write token (phase 5).

import { fmtInt } from "../../format.js";
import { hiddenBssid, presenceText, securityBadge, siblingText, transientText, vendorList } from "./networks.js";

const LOCKED = "Write actions need the write token (dashboard phase 5)";
// a network is configured by its SSID; a hidden BSSID without a known name has none
const NO_NAME = "A hidden network without a known name cannot be configured by SSID";

export default function OtherTable({ other, page, setPage }) {
  const hs = other.hotspots;
  const first = (other.page - 1) * other.page_size;
  const pages = Math.max(1, Math.ceil(other.total_rows / other.page_size));
  return (
    <section className="card table-card" aria-labelledby="oth-h">
      <div className="table-head">
        <h2 id="oth-h">Other networks nearby</h2>
        <span className="muted small">
          {fmtInt(other.networks)} networks · {fmtInt(other.hidden)} hidden SSIDs · sorted by number of BSSIDs
        </span>
      </div>
      <div className="scroll-x">
        <table className="t nw">
          <thead>
            <tr><th>Network (SSID)</th><th>BSSIDs</th><th>Vendors</th><th>Security</th><th>Presence</th><th>Action</th></tr>
          </thead>
          <tbody>
            {hs.networks > 0 && page === 1 && (
              <tr>
                <td className="name">Phone hotspots <span className="muted" style={{ fontWeight: 400 }}>(randomized BSSIDs)</span></td>
                <td>{fmtInt(hs.bssids)}</td>
                <td>–</td>
                <td><span className="b neu">various</span></td>
                <td title="a sighting: observations without a gap over 10 minutes">{transientText(hs.median_sighting_s)}</td>
                <td>{hs.ignored ? <span className="b mute">Ignored automatically</span> : "–"}</td>
              </tr>
            )}
            {other.rows.map((r) => {
              const sec = securityBadge(r.security);
              const bssid = hiddenBssid(r.key);
              return (
                <tr key={r.key}>
                  <td className="name">
                    {r.hidden
                      ? <><i style={{ fontWeight: 400 }}>hidden network</i>{bssid && <span className="mono muted"> {bssid}</span>}</>
                      : r.network}
                    {r.sibling && <span className="sib muted tiny">{siblingText(r.sibling)}</span>}
                  </td>
                  <td>{fmtInt(r.bssids)}</td>
                  <td>{vendorList(r.vendors)}</td>
                  <td><span className={`b ${sec.cls}`}>{sec.text}</span></td>
                  <td title={r.presence === "transient" ? "a sighting: observations without a gap over 10 minutes" : undefined}>
                    {presenceText(r)}
                  </td>
                  <td>
                    {r.hidden && !r.network ? <span className="muted" title={NO_NAME}>–</span> : (
                      <div style={{ display: "flex", gap: 6 }}>
                        <button type="button" className="btn2" disabled title={LOCKED}>Managed</button>
                        <button type="button" className="btn2" disabled title={LOCKED}>Friendly</button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="pager muted">
        <span>
          {other.total_rows ? `${fmtInt(first + 1)}–${fmtInt(first + other.rows.length)} of ${fmtInt(other.total_rows)}` : "none"}
          {hs.networks > 0 ? ` (and ${fmtInt(hs.networks)} hotspot networks)` : ""}
        </span>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className="btn2" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Previous</button>
          <button type="button" className="btn2" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      </div>
    </section>
  );
}
