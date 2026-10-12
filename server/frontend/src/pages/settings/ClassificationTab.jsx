// Settings > Classification (Settings board): the automatic rules that
// classify an AP (ap_classification()); a manual classification on the AP
// page always wins. PUT /api/v2/settings/classification, then the events are
// re-derived (the rule event "unknown BSSID with a managed SSID").

import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { fmtInt } from "../../format.js";
import { SaveBar, useDraft, useSave } from "./form.jsx";
import { classificationBody, classificationDraft, classificationProblem } from "./settings.js";

function Rule({ title, note, children, last = false }) {
  return (
    <div className={last ? "set-row last" : "set-row"}>
      <div className="set-row-text"><span className="em">{title}</span><span className="muted small">{note}</span></div>
      <div className="set-row-control">{children}</div>
    </div>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} aria-label={label} />
      {checked ? "on" : "off"}
    </label>
  );
}

export default function ClassificationTab() {
  const [key, url] = ep.settingsClassification();
  const q = useQuery(key, url);
  const { draft, setDraft, dirty, reset } = useDraft(q.data, classificationDraft, classificationBody);
  const save = useSave();
  const set = (k) => (v) => setDraft({ ...draft, [k]: v });
  const a = q.data?.affected;
  const problem = draft ? classificationProblem(draft) : null;
  const onSave = () => save.run(
    (w) => w.send("PUT", url, classificationBody(draft), ["settings.classification"]),
    "Saved. The classification and the rule events are re-derived.",
  );
  return (
    <section className="card set-card" aria-labelledby="cls-h">
      <div className="set-head">
        <h2 id="cls-h">Classification rules</h2>
        <span className="muted small">How APs are classified automatically. A manual classification on the AP page always wins.</span>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!draft && !q.error && <Skeleton height={240} />}
      {draft && (
        <div>
          <Rule title="Ignore hotspots with a randomized BSSID"
                note={`Locally administered MAC addresses (phones).${a ? ` ${fmtInt(a.random_bssid)} of the account's ${fmtInt(a.aps)} APs.` : ""}`}>
            <Toggle checked={draft.ignore_random_bssid} onChange={set("ignore_random_bssid")} label="Ignore randomized BSSIDs" />
          </Rule>
          <Rule title="Managed SSID from a BSSID that is not allowed → suspicious"
                note="Enables evil twin detection (a). Without a list of managed networks it stays off.">
            <Toggle checked={draft.suspicious_unallowed} onChange={set("suspicious_unallowed")} label="Suspicious when not allowed" />
          </Rule>
          <Rule title="AP from an allowed vendor with a managed SSID → managed"
                note="New campus BSSIDs classify themselves. Convenient, but less strict.">
            <Toggle checked={draft.managed_by_vendor} onChange={set("managed_by_vendor")} label="Managed by vendor" />
          </Rule>
          <Rule title="Ignore APs weaker than"
                note="Distant APs at the sensitivity limit of the adapter (its floor is −106 dBm).">
            <label className="check">
              <input type="checkbox" checked={draft.weaker_on} onChange={(e) => set("weaker_on")(e.target.checked)}
                     aria-label="Ignore weak APs" />
            </label>
            <input type="number" className="in num" value={draft.weaker_dbm} disabled={!draft.weaker_on} step="1"
                   min="-120" max="-30" aria-label="Threshold in dBm" onChange={(e) => set("weaker_dbm")(e.target.value)} />
            <span>dBm</span>
          </Rule>
          <Rule title="Mobile vendors" last
                note="An AP of these vendors (a prefix of the manufacturer name) counts as a mobile hotspot, as do randomized BSSIDs and APs that lived under 2 h.">
            <input type="text" className="in vendors" value={draft.vendors} aria-label="Mobile vendors, comma separated"
                   onChange={(e) => set("vendors")(e.target.value)} />
          </Rule>
        </div>
      )}
      {draft && (
        <SaveBar label="Save classification" dirty={dirty} problem={problem} save={save} onSave={onSave}
                 onDiscard={() => { reset(); save.clear(); }} />
      )}
    </section>
  );
}
