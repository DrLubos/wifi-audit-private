// Settings > Detectors (Settings board): the detector parameters of the
// account. The sensor-offline minutes apply at once (the sensor events are
// re-derived after the save); the batch detectors still take their
// command-line options and read these in a later step - said on the page.

import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { SaveBar, useDraft, useSave } from "./form.jsx";
import { detectorsBody, detectorsDraft, detectorsProblem, GUARDS } from "./settings.js";

function Row({ title, note, children, last = false }) {
  return (
    <div className={last ? "set-row last" : "set-row"}>
      <div className="set-row-text"><span className="em">{title}</span><span className="muted small">{note}</span></div>
      <div className="set-row-control">{children}</div>
    </div>
  );
}

export default function DetectorsTab() {
  const [key, url] = ep.settingsDetectors();
  const q = useQuery(key, url);
  const { draft, setDraft, dirty, reset } = useDraft(q.data, detectorsDraft, detectorsBody);
  const save = useSave();
  const num = (k, label, width = 80) => (
    <input type="number" className="in num" style={{ width }} value={draft[k]} aria-label={label}
           onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
  );
  const problem = draft ? detectorsProblem(draft) : null;
  const onSave = () => save.run(
    (w) => w.send("PUT", url, detectorsBody(draft), ["settings.detectors"]),
    "Saved. The sensor events are re-derived.",
  );
  return (
    <section className="card set-card" aria-labelledby="det-h">
      <div className="set-head">
        <h2 id="det-h">Detectors</h2>
        <span className="muted small">
          Detector sensitivity, stored with the account. The sensor-offline minutes apply at once; the batch
          detectors (deauth flood, evil twin) still take their command-line options and read these in a later step.
        </span>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!draft && !q.error && <Skeleton height={240} />}
      {draft && (
        <div>
          <Row title="Deauth flood – minimum number of bursts in a window"
               note="Kismet DEAUTHFLOOD is always reported; this catches slow attacks below its threshold.">
            {num("deauth_min_bursts", "Number of bursts")}<span>in</span>{num("deauth_window_s", "Window in seconds", 96)}<span>s</span>
          </Row>
          <Row title="Evil twin (b) – RSSI deviation from baseline" note="Multiple of the robust σ and the minimum deviation in dB.">
            <span>k =</span>{num("evil_twin_k", "Sigma multiple", 72)}<span>min.</span>{num("evil_twin_min_db", "Minimum deviation in dB", 72)}<span>dB</span>
          </Row>
          <Row title="Evil twin (b) – guard period after an AP channel change"
               note="Fewer false positives, but also a blind spot (with 1 h, up to 41 % of the time for campus APs).">
            <select className="in sel-sm" aria-label="Guard period" value={draft.evil_twin_guard_s}
                    onChange={(e) => setDraft({ ...draft, evil_twin_guard_s: Number(e.target.value) })}>
              {GUARDS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </Row>
          <Row title="Alert when a sensor sends no data for longer than" note="Creates an event of type Sensor." last>
            {num("sensor_offline_min", "Minutes")}<span>min</span>
          </Row>
        </div>
      )}
      {draft && (
        <SaveBar label="Save detectors" dirty={dirty} problem={problem} save={save} onSave={onSave}
                 onDiscard={() => { reset(); save.clear(); }} />
      )}
    </section>
  );
}
