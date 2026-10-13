// Settings > Sensors > Capture hardware: per sensor one read-only adapter
// line (its identity is VID:PID + MAC; seeded now, reported by the sensor in
// the live-upload phase - interface names like wlan1 are display only) and
// "Add hardware change": a dated entry of the configuration history (when,
// an optional note such as "swapped antenna"). The antenna is not recorded -
// it is passive, nothing on the sensor can detect it and its gain feeds no
// computation - but a change of it (or anything else that shifts every RSSI)
// is a data boundary: RSSI baselines start again from the entry (schema 11/13).

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import Modal from "../../components/Modal.jsx";
import { useWrite } from "../../data/write.js";
import { fmtDateTime } from "../../format.js";
import {
  adapterLine, adapterSource, hardwareChangeBody, hardwareChangeProblem, utcToZoned, zonedToUtc,
} from "./settings.js";

const LOCKED = "Unlock with the write token above first";
const REFETCH = ["settings.sensors", "sensors"];

export default function HardwareCard({ d, tz }) {
  const w = useWrite();
  const [params] = useSearchParams();
  const [sensor, setSensor] = useState(null);
  const [msg, setMsg] = useState(null);
  const first = params.get("sensor");
  const changes = (name) => d.history.filter((h) => h.sensor === name && h.kind === "hardware_change");
  return (
    <section className="card set-card" aria-labelledby="hw-h">
      <div className="set-head">
        <h2 id="hw-h">Capture hardware</h2>
        <span className="muted small">
          The adapter is read-only: the sensor will report it itself in the live-upload phase. The antenna is not
          recorded; when it is swapped - or anything else shifts every RSSI - add a hardware change. It is a data
          boundary: RSSI baselines start again from it (evil twin (b) waits until the new hardware has 200 readings on
          2 days) and measurements before and after are evaluated apart.
        </span>
      </div>
      <div className="hw-lines">
        {d.sensors.map((s) => {
          const last = changes(s.name)[0];
          return (
            <div key={s.name} className={s.name === first ? "hw-line picked" : "hw-line"}>
              <span className="em hw-sensor">{s.name}</span>
              <span className="hw-now">
                {adapterLine(s.adapter)}
                <span className="muted tiny hw-src">
                  {[adapterSource(s.adapter), last ? `last hardware change ${fmtDateTime(last.ts, tz)}` : "no hardware change recorded"]
                    .filter(Boolean).join(" · ")}
                </span>
              </span>
              <button type="button" className="btn2" disabled={!w.unlocked} title={w.unlocked ? undefined : LOCKED}
                      onClick={() => { setMsg(null); setSensor(s.name); }}>Add hardware change</button>
            </div>
          );
        })}
      </div>
      {msg && <span className="save-msg" role="status"><span className="ok-text">{msg}</span></span>}
      {sensor && <ChangeModal sensor={sensor} tz={tz} onClose={() => setSensor(null)}
                              onDone={(text) => { setSensor(null); setMsg(text); }} />}
    </section>
  );
}

function ChangeModal({ sensor, tz, onClose, onDone }) {
  const w = useWrite();
  const [since, setSince] = useState(() => utcToZoned(new Date(), tz));
  const [note, setNote] = useState("");
  const [state, setState] = useState({ busy: false, error: null });
  const sinceUtc = zonedToUtc(since, tz);
  const problem = hardwareChangeProblem(sinceUtc);
  const save = async (ev) => {
    ev.preventDefault();
    if (problem) return;
    setState({ busy: true, error: null });
    try {
      await w.send("POST", `/api/v2/settings/sensors/${encodeURIComponent(sensor)}/hardware-change`,
                   hardwareChangeBody(sinceUtc, note), REFETCH);
      onDone(`Recorded for ${sensor}: a data boundary from ${fmtDateTime(sinceUtc, tz)}. Its RSSI baselines start again with the next import.`);
    } catch (e) {
      setState({ busy: false, error: e.message });
    }
  };
  return (
    <Modal title={`Add a hardware change to ${sensor}`} onClose={onClose}>
      <form className="modal-form" onSubmit={save}>
        <label className="fl">When ({tz})
          <input type="datetime-local" className="in" value={since} onChange={(e) => setSince(e.target.value)} autoFocus />
        </label>
        <label className="fl">Note (optional)
          <input className="in" value={note} maxLength={500} placeholder="e.g. swapped antenna" onChange={(e) => setNote(e.target.value)} />
        </label>
        <div className="warn-box">
          A data boundary: RSSI baselines never span it, so right after it evil twin (b) has no baseline until the
          new hardware has 200 readings on 2 days. Enter the time the change really happened - it can be in the past.
        </div>
        {state.error && <span className="bad-text" role="alert">{state.error}</span>}
        <div className="modal-foot">
          <button type="button" className="btn2" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn" disabled={Boolean(problem) || state.busy} title={problem ?? undefined}>
            {state.busy ? "Saving…" : "Add hardware change"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
