// Other networks nearby (Networks board): every network that is not
// configured, phone hotspots (randomized BSSIDs) as one summary row, the
// rest by BSSID count with presence (SPEC Q9; transient ones with their
// median sighting), paged by the api. "Friendly" adds the network as known
// and legitimate; "Managed" asks for its expected security and allows the
// BSSIDs seen in the period - without them every BSSID of the SSID would be
// classified suspicious. Both need the write token (POST
// /api/v2/settings/networks; the events are re-derived).

import { useState } from "react";
import { useWrite } from "../../data/write.js";
import { fmtInt } from "../../format.js";
import { EXPECTED } from "../settings/settings.js";
import { hiddenBssid, presenceText, securityBadge, siblingText, transientText, vendorList } from "./networks.js";

const LOCKED = "Unlock with the write token in Settings first";
const REFETCH = ["networks", "settings.networks", "aps", "ap|", "events", "event|", "overview"];
// a network is configured by its SSID; a hidden BSSID without a known name has none
const NO_NAME = "A hidden network without a known name cannot be configured by SSID";

export default function OtherTable({ other, page, setPage }) {
  const w = useWrite();
  const [ask, setAsk] = useState(null);           // the row asked to become managed
  const [policy, setPolicy] = useState("psk_or_better");
  const [state, setState] = useState({ busy: null, error: null, done: {} });
  const add = async (r, kind) => {
    setState({ ...state, busy: r.key, error: null });
    const body = kind === "managed"
      ? { ssid: r.network, kind, expected_security: policy, allowed_bssids: r.bssid_list ?? [] }
      : { ssid: r.network, kind };
    try {
      await w.send("POST", "/api/v2/settings/networks", body, REFETCH);
      setState({ busy: null, error: null, done: { ...state.done, [r.key]: kind } });
      setAsk(null);
    } catch (e) {
      setState({ ...state, busy: null, error: `${r.network}: ${e.message}` });
    }
  };
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
                    {r.hidden && !r.network ? <span className="muted" title={NO_NAME}>–</span>
                      : state.done[r.key] ? <span className="b ok">Added as {state.done[r.key]}</span>
                        : ask === r.key ? (
                          <div className="managed-ask">
                            <span className="small">Allow its {fmtInt(r.bssid_list?.length ?? 0)} BSSIDs; expected security</span>
                            <select className="in sel-sm" value={policy} aria-label="Expected security"
                                    onChange={(e) => setPolicy(e.target.value)}>
                              {EXPECTED.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
                            </select>
                            <button type="button" className="btn2" disabled={state.busy === r.key} onClick={() => add(r, "managed")}>Save</button>
                            <button type="button" className="btn2" onClick={() => setAsk(null)}>Cancel</button>
                          </div>
                        ) : (
                          <div style={{ display: "flex", gap: 6 }}>
                            <button type="button" className="btn2" disabled={!w.unlocked || state.busy === r.key}
                                    title={w.unlocked ? "a network you run: checked against a policy" : LOCKED}
                                    onClick={() => setAsk(r.key)}>Managed</button>
                            <button type="button" className="btn2" disabled={!w.unlocked || state.busy === r.key}
                                    title={w.unlocked ? "someone else's known network: kept out of the unknown list" : LOCKED}
                                    onClick={() => add(r, "friendly")}>Friendly</button>
                          </div>
                        )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {state.error && <div className="bad-text small" role="alert" style={{ padding: "8px 18px 0" }}>{state.error}</div>}
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
