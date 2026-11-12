// Settings > Sensors > Discard mode (schema 20, docs/discard-mode.md): per
// sensor its state and "Turn on…" / "Turn off now". While a window is on,
// the sensor keeps uploading as usual and the server answers every upload
// but stores only its heartbeat (status, contact, adapter); the window is an
// 'excluded' entry of the configuration history - never an outage, lost
// polls or coverage, and no event comes from it. It switches itself off at
// the end (at most 24 h). For a test away from the sensor's place (e.g. a
// power measurement elsewhere). Nothing changes on the sensor.

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import Modal from "../../components/Modal.jsx";
import { useWrite } from "../../data/write.js";
import { fmtDateTime } from "../../format.js";
import { useNow } from "../../hooks/useNow.js";
import { DISCARD_HOURS, discardBody, discardLine, discardProblem, utcToZoned, zonedToUtc } from "./settings.js";

const LOCKED = "Unlock with the write token above first";
const REFETCH = ["settings.sensors", "sensors"];

export default function DiscardCard({ d, tz }) {
  const w = useWrite();
  const now = useNow(30_000);
  const [params] = useSearchParams();
  const [sensor, setSensor] = useState(null);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const first = params.get("sensor");
  const stop = async (name) => {
    if (!window.confirm(`Turn discard mode off for ${name}? From its next upload on its data is stored again.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const out = await w.send("DELETE", `/api/v2/settings/sensors/${encodeURIComponent(name)}/discard`, undefined, REFETCH);
      setMsg({ ok: `Discard mode off for ${name}: ${out.discard.polls} polls were discarded; its data is stored again.` });
    } catch (e) {
      setMsg({ error: e.message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card set-card" aria-labelledby="discard-h">
      <div className="set-head">
        <h2 id="discard-h">Discard mode</h2>
        <span className="muted small">
          For a test away from the sensor's place: the sensor keeps uploading as usual, the server accepts every
          upload but stores only its heartbeat (status, contact, adapter). The window is recorded in the
          configuration history as excluded - never an outage, lost polls or coverage, and no event comes from it.
          It switches itself off at its end (at most 24 h). Nothing changes on the sensor.
        </span>
      </div>
      <div className="hw-lines">
        {d.sensors.map((s) => (
          <div key={s.name} className={s.name === first ? "hw-line picked" : "hw-line"}>
            <span className="em hw-sensor">{s.name}</span>
            <span className="hw-now">
              {s.discard ? <span className="b warn">Discard mode on</span> : null} {discardLine(s.discard, tz, now)}
              {s.discard?.note && <span className="muted tiny hw-src">Note: {s.discard.note}</span>}
            </span>
            {s.discard ? (
              <button type="button" className="btn2" disabled={!w.unlocked || busy} title={w.unlocked ? undefined : LOCKED}
                      onClick={() => stop(s.name)}>Turn off now</button>
            ) : (
              <button type="button" className="btn2" disabled={!w.unlocked} title={w.unlocked ? undefined : LOCKED}
                      onClick={() => { setMsg(null); setSensor(s.name); }}>Turn on…</button>
            )}
          </div>
        ))}
      </div>
      {msg && (
        <span className="save-msg" role="status">
          {msg.error ? <span className="bad-text">{msg.error}</span> : <span className="ok-text">{msg.ok}</span>}
        </span>
      )}
      {sensor && <StartModal sensor={sensor} tz={tz} onClose={() => setSensor(null)}
                             onDone={(text) => { setSensor(null); setMsg({ ok: text }); }} />}
    </section>
  );
}

function StartModal({ sensor, tz, onClose, onDone }) {
  const w = useWrite();
  const [mode, setMode] = useState("duration");
  const [hours, setHours] = useState(6);
  const [until, setUntil] = useState(() => utcToZoned(new Date(Date.now() + 6 * 3600e3), tz));
  const [note, setNote] = useState("");
  const [state, setState] = useState({ busy: false, error: null });
  const form = { mode, hours, untilUtc: zonedToUtc(until, tz), note };
  const problem = discardProblem(form);
  const save = async (ev) => {
    ev.preventDefault();
    if (problem) return;
    setState({ busy: true, error: null });
    try {
      const out = await w.send("POST", `/api/v2/settings/sensors/${encodeURIComponent(sensor)}/discard`,
                               discardBody(form), REFETCH);
      onDone(`Discard mode on for ${sensor} until ${fmtDateTime(out.discard.until, tz)}: its uploads are accepted and not stored. It switches itself off then.`);
    } catch (e) {
      setState({ busy: false, error: e.message });
    }
  };
  return (
    <Modal title={`Turn discard mode on for ${sensor}`} onClose={onClose}>
      <form className="modal-form" onSubmit={save}>
        <fieldset className="plans">
          <legend className="em">Ends</legend>
          <label className={mode === "duration" ? "plan on" : "plan"}>
            <input type="radio" name="discard-end" checked={mode === "duration"} onChange={() => setMode("duration")} />
            <span className="stack">
              <span className="em">After</span>
              <select className="in sel-sm" value={hours} onChange={(e) => { setMode("duration"); setHours(Number(e.target.value)); }}>
                {DISCARD_HOURS.map((h) => <option key={h} value={h}>{h} h</option>)}
              </select>
            </span>
          </label>
          <label className={mode === "until" ? "plan on" : "plan"}>
            <input type="radio" name="discard-end" checked={mode === "until"} onChange={() => setMode("until")} />
            <span className="stack">
              <span className="em">At ({tz})</span>
              <input type="datetime-local" className="in" value={until}
                     onChange={(e) => { setMode("until"); setUntil(e.target.value); }} />
            </span>
          </label>
        </fieldset>
        <label className="fl">Note (required)
          <input className="in" value={note} maxLength={500} placeholder="e.g. PoE power measurement in the lab"
                 onChange={(e) => setNote(e.target.value)} autoFocus />
        </label>
        <div className="warn-box">
          From now until the end, nothing {sensor} uploads is stored - polls, observations, alerts, configuration
          changes. Its status and contact still show. The window is excluded from loss, outages and coverage, and
          rows from it that arrive later are dropped too. At most 24 h; it switches itself off at the end.
        </div>
        {state.error && <span className="bad-text" role="alert">{state.error}</span>}
        <div className="modal-foot">
          <button type="button" className="btn2" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn" disabled={Boolean(problem) || state.busy} title={problem ?? undefined}>
            {state.busy ? "Turning on…" : "Turn discard mode on"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
