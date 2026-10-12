// Settings > Networks (Settings board): the managed and friendly networks -
// type, expected security, MFP required, the allowed BSSIDs and vendor
// prefixes - edited as a draft and saved as a whole (PUT
// /api/v2/settings/networks: one transaction, then the events are re-derived:
// the classification and the rule event read the networks).

import { useState } from "react";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { SaveBar, useDraft, useSave } from "./form.jsx";
import {
  addAllowed, EXPECTED, networkDraft, networksBody, ouiText, parseAllowed, removeAllowed, ssidProblem,
} from "./settings.js";

const toDraft = (d) => d.networks.map(networkDraft);

export default function NetworksTab() {
  const [key, url] = ep.settingsNetworks();
  const q = useQuery(key, url);
  const { draft, setDraft, dirty, reset } = useDraft(q.data, toDraft, networksBody);
  const save = useSave();
  const [ssid, setSsid] = useState("");
  const [kind, setKind] = useState("managed");
  const [adding, setAdding] = useState({});
  const [addError, setAddError] = useState({});
  const vendors = q.data?.vendors ?? {};
  const edit = (k, fn) => setDraft(draft.map((d) => (d.key === k ? fn(d) : d)));
  const newProblem = draft ? ssidProblem(ssid, draft) : null;
  const addNetwork = () => {
    if (newProblem) return;
    setDraft([...draft, networkDraft({ ssid, kind, expected_security: kind === "managed" ? "psk_or_better" : null })]);
    setSsid("");
  };
  const addEntry = (d) => {
    const entry = parseAllowed(adding[d.key]);
    if (entry.error) {
      setAddError({ ...addError, [d.key]: entry.error });
      return;
    }
    edit(d.key, (x) => addAllowed(x, entry));
    setAdding({ ...adding, [d.key]: "" });
    setAddError({ ...addError, [d.key]: null });
  };
  const onSave = () => save.run(
    (w) => w.send("PUT", url, networksBody(draft), ["settings.networks"]),
    (out) => `Saved. Events re-derived for ${Object.keys(out?.refreshed ?? {}).join(", ") || "no sensor"}.`,
  );
  return (
    <section className="card set-card" aria-labelledby="net-h">
      <div className="set-head">
        <h2 id="net-h">Managed and friendly networks</h2>
        <span className="muted small">
          <b className="em">Managed</b>: a network you run. Its security is checked and every BSSID using its name
          that is not in the allowed list raises an alert (evil twin). <b className="em">Friendly</b>: someone
          else’s known, legitimate network (a neighbour, the next building). It is not checked and raises no
          events; it is only kept out of the unknown list.
        </span>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!draft && !q.error && <Skeleton height={200} />}
      {draft && (
        <div className="scroll-x">
          <table className="t set-table">
            <thead>
              <tr><th>SSID</th><th>Type</th><th>Expected security</th><th>MFP required</th>
                <th>Allowed BSSIDs / vendors</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {draft.length === 0 && <tr><td colSpan={6} className="muted">No network yet - add the ones you run below.</td></tr>}
              {draft.map((d) => (
                <tr key={d.key}>
                  <td className="em">{d.ssid}{d.id == null && <span className="b info new-tag">new</span>}</td>
                  <td>
                    <select className="in sel-sm" aria-label={`Type of ${d.ssid}`} value={d.kind}
                            onChange={(e) => edit(d.key, (x) => ({ ...x, kind: e.target.value,
                              expected_security: e.target.value === "managed" ? (x.expected_security ?? "psk_or_better") : null }))}>
                      <option value="managed">Managed</option><option value="friendly">Friendly</option>
                    </select>
                  </td>
                  <td>
                    {d.kind === "managed" ? (
                      <select className="in sel-sm" aria-label={`Expected security of ${d.ssid}`}
                              value={d.expected_security ?? ""}
                              onChange={(e) => edit(d.key, (x) => ({ ...x, expected_security: e.target.value || null }))}>
                        <option value="">no policy</option>
                        {EXPECTED.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
                      </select>
                    ) : <span className="muted">not checked</span>}
                  </td>
                  <td>
                    {d.kind === "managed" ? (
                      <label className="check">
                        <input type="checkbox" checked={d.require_mfp}
                               onChange={(e) => edit(d.key, (x) => ({ ...x, require_mfp: e.target.checked }))} />
                        {d.require_mfp ? "yes" : "no"}
                      </label>
                    ) : <span className="muted">–</span>}
                  </td>
                  <td className="allowed">
                    <div className="tags">
                      {d.allowed_ouis.map((o) => (
                        <span className="tag" key={o}>
                          {vendors[o] && <span>{vendors[o].replace(/ Wireless$/, "")}</span>}<span className="mono">{o}</span>
                          <button type="button" className="tag-x" aria-label={`Remove ${ouiText(o, vendors)}`}
                                  onClick={() => edit(d.key, (x) => removeAllowed(x, "oui", o))}>×</button>
                        </span>
                      ))}
                      {d.allowed_bssids.map((b) => (
                        <span className="tag" key={b}>
                          <span className="mono">{b}</span>
                          <button type="button" className="tag-x" aria-label={`Remove ${b}`}
                                  onClick={() => edit(d.key, (x) => removeAllowed(x, "bssid", b))}>×</button>
                        </span>
                      ))}
                      {!d.allowed_ouis.length && !d.allowed_bssids.length && <span className="muted tiny">none allowed</span>}
                    </div>
                    <div className="add-allowed">
                      <input className="in in-sm mono" placeholder="BSSID or vendor prefix" value={adding[d.key] ?? ""}
                             aria-label={`Allow a BSSID or vendor prefix for ${d.ssid}`}
                             onChange={(e) => setAdding({ ...adding, [d.key]: e.target.value })}
                             onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addEntry(d); } }} />
                      <button type="button" className="btn2" onClick={() => addEntry(d)}>Allow</button>
                    </div>
                    {addError[d.key] && <span className="bad-text tiny">{addError[d.key]}</span>}
                  </td>
                  <td>
                    <button type="button" className="btn2" onClick={() => setDraft(draft.filter((x) => x.key !== d.key))}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {draft && (
        <form className="add-net" onSubmit={(e) => { e.preventDefault(); addNetwork(); }}>
          <label className="fl" style={{ flex: "1 1 220px" }}>Add network (SSID)
            <input type="text" className="in" placeholder="e.g. eduroam" value={ssid} onChange={(e) => setSsid(e.target.value)} />
          </label>
          <label className="fl">Type
            <select className="in" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="managed">Managed</option><option value="friendly">Friendly</option>
            </select>
          </label>
          <button type="submit" className="btn2 btn-lg add-btn" disabled={Boolean(newProblem)} title={ssid ? newProblem ?? undefined : undefined}>
            Add
          </button>
          {ssid && newProblem && <span className="bad-text tiny">{newProblem}</span>}
        </form>
      )}
      {draft && (
        <SaveBar label="Save networks" dirty={dirty} save={save} onSave={onSave}
                 onDiscard={() => { reset(); save.clear(); }} />
      )}
    </section>
  );
}
