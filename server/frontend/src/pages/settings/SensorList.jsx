// Settings > Sensors: the sensors of the account with "Add sensor" and, per
// sensor, "Edit" - a modal for name, location and description (a rename
// changes only the label: sensor_id is the key) with a guarded Delete: never a
// sensor with data (the api refuses and names the tables), and the sensor's
// name typed to confirm. Every write is audited (sensor.add / .rename /
// .meta / .delete). An added sensor is empty: it shows the multi-sensor
// chrome (selectors, lists, empty states) until an import files data under
// its name (seed/import_snapshot.py --sensor NAME).

import { useState } from "react";
import Modal from "../../components/Modal.jsx";
import { useWrite } from "../../data/write.js";
import { deleteGuard, renameNote, sensorBody, sensorNameProblem } from "./settings.js";

const LOCKED = "Unlock with the write token above first";
const REFETCH = ["settings.sensors", "sensors", "overview", "events"];

export default function SensorList({ d }) {
  const w = useWrite();
  const [modal, setModal] = useState(null);                   // {mode: "add"} | {mode: "edit", name}
  const [msg, setMsg] = useState(null);
  const done = (text) => { setModal(null); setMsg(text); };
  const sensor = modal?.mode === "edit" ? d.sensors.find((s) => s.name === modal.name) : null;
  return (
    <section className="card set-card" aria-labelledby="sen-list-hw">
      <div className="set-head set-head-row">
        <div className="stack">
          <h2 id="sen-list-hw">Sensors</h2>
          <span className="muted small">
            Where a sensor is and what it is for - shown on the Sensors page. Free text, no coordinates.
          </span>
        </div>
        <button type="button" className="btn2 btn-lg" disabled={!w.unlocked} title={w.unlocked ? undefined : LOCKED}
                onClick={() => { setMsg(null); setModal({ mode: "add" }); }}>Add sensor</button>
      </div>
      <div className="scroll-x">
        <table className="t set-table">
          <thead>
            <tr><th>Name</th><th>Location</th><th>Description</th><th>Data</th><th><span className="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            {d.sensors.map((s) => (
              <tr key={s.name}>
                <td className="em">{s.name}</td>
                <td>{s.meta?.location ?? <span className="muted">–</span>}</td>
                <td className="small">{s.meta?.description ?? <span className="muted">–</span>}</td>
                <td>{s.meta?.has_polls ? <span className="b ok">collecting</span> : <span className="b neu">no data yet</span>}</td>
                <td className="nowrap">
                  <button type="button" className="btn2" disabled={!w.unlocked} title={w.unlocked ? undefined : LOCKED}
                          onClick={() => { setMsg(null); setModal({ mode: "edit", name: s.name }); }}>Edit</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {msg && <span className="save-msg" role="status"><span className="ok-text">{msg}</span></span>}
      {modal && (
        <SensorModal mode={modal.mode} sensor={sensor} sensors={d.sensors} onClose={() => setModal(null)} onDone={done} />
      )}
    </section>
  );
}

function SensorModal({ mode, sensor, sensors, onClose, onDone }) {
  const w = useWrite();
  const edit = mode === "edit";
  const [draft, setDraft] = useState({ name: sensor?.name ?? "", location: sensor?.meta?.location ?? "",
                                       description: sensor?.meta?.description ?? "" });
  const [state, setState] = useState({ busy: false, error: null });
  const [del, setDel] = useState(false);
  const [typed, setTyped] = useState("");
  const set = (k) => (e) => setDraft({ ...draft, [k]: e.target.value });
  const problem = sensorNameProblem(draft.name, sensors, sensor?.name ?? null);
  const body = sensorBody(draft);
  const changed = !edit || body.name !== sensor.name || body.location !== (sensor.meta?.location ?? null)
    || body.description !== (sensor.meta?.description ?? null);
  const run = async (fn, okText) => {
    setState({ busy: true, error: null });
    try {
      const out = await fn();
      onDone(typeof okText === "function" ? okText(out) : okText);
    } catch (e) {
      setState({ busy: false, error: e.message });
    }
  };
  const save = (ev) => {
    ev.preventDefault();
    if (problem || !changed) return;
    if (edit) {
      run(() => w.send("PUT", `/api/v2/settings/sensors/${encodeURIComponent(sensor.name)}`, body, REFETCH),
          (out) => (out.renamed ? renameNote(sensor.name, body.name) : `Saved ${body.name}.`));
    } else {
      run(() => w.send("POST", "/api/v2/settings/sensors/new", body, REFETCH),
          `Added ${body.name}: an empty sensor - data arrives with an import under that name.`);
    }
  };
  const guard = edit ? deleteGuard(sensor.meta, sensor.name, typed) : null;
  const remove = () => run(() => w.send("DELETE",
    `/api/v2/settings/sensors/${encodeURIComponent(sensor.name)}?confirm=${encodeURIComponent(sensor.name)}`, undefined, REFETCH),
    `Deleted ${sensor.name}.`);
  return (
    <Modal title={edit ? `Edit ${sensor.name}` : "Add sensor"} onClose={onClose}>
      <form className="modal-form" onSubmit={save}>
        <label className="fl">Name
          <input className="in" value={draft.name} maxLength={40} autoFocus onChange={set("name")} aria-invalid={Boolean(problem)} />
          <span className={problem && draft.name ? "bad-text" : "muted tiny"}>
            {problem && draft.name ? problem : "A short handle - it is in URLs and in the importer's --sensor option."}
          </span>
        </label>
        {edit && body.name !== sensor.name && !problem && (
          <div className="warn-box">
            Renaming changes only the label (the data stays with the sensor). The importer files data under the name:
            use <span className="mono">--sensor {body.name}</span> from the next import on.
          </div>
        )}
        <label className="fl">Location
          <input className="in" value={draft.location} maxLength={120} onChange={set("location")} />
        </label>
        <label className="fl">Description
          <input className="in" value={draft.description} maxLength={500} onChange={set("description")} />
        </label>
        {state.error && <span className="bad-text" role="alert">{state.error}</span>}
        <div className="modal-foot">
          <button type="button" className="btn2" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn" disabled={Boolean(problem) || !changed || state.busy}
                  title={problem ?? (!changed ? "Nothing changed" : undefined)}>
            {state.busy ? "Saving…" : edit ? "Save" : "Add sensor"}
          </button>
        </div>
      </form>
      {edit && (
        <div className="danger-zone">
          {!del ? (
            <button type="button" className="btn2 danger" onClick={() => setDel(true)}>Delete sensor…</button>
          ) : (
            <>
              <span className="em">Delete {sensor.name}</span>
              {guard.allowed === false && !guard.ask ? (
                <span className="muted small">{guard.why}. Only a sensor that never collected anything can be deleted;
                  its measurements are the thesis data.</span>
              ) : (
                <>
                  <span className="muted small">
                    Removes the sensor with its configuration (hardware history, capture configurations, notes); the
                    api refuses while any poll, device, observation, alert, event or rollup row belongs to it.
                  </span>
                  <label className="fl">Type <b className="mono">{sensor.name}</b> to confirm
                    <input className="in" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
                  </label>
                  <div className="modal-foot">
                    <button type="button" className="btn2" onClick={() => { setDel(false); setTyped(""); }}>Keep it</button>
                    <button type="button" className="btn2 danger" disabled={!guard.allowed || state.busy}
                            title={guard.why} onClick={remove}>Delete {sensor.name}</button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
