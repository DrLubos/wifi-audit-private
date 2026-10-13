// Settings > Sensors > Capture hardware: one line per sensor with what is
// attached now and "Edit / record change" (a modal), and a small "Manage
// catalog". Only the ANTENNA is ever entered by hand - it is passive, nothing
// on the sensor can detect it - and its gain is optional: the data boundary
// needs only that a change happened and which antenna. The ADAPTER is
// seeded; in the live-upload phase the sensor reports it (USB VID:PID,
// chipset, driver, MAC, interface - the RSSI floors follow from the chipset),
// so it is never typed (PLAN.md 11.1). A change is a dated data boundary: RSSI
// baselines start again from it (schema 11).

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Modal from "../../components/Modal.jsx";
import { useWrite } from "../../data/write.js";
import { fmtDate, fmtDateTime } from "../../format.js";
import {
  antennaBody, antennaDraft, antennaProblem, captureText, EMPTY_ANTENNA, hwDetail, hwText, utcToZoned, zonedToUtc,
} from "./settings.js";

const LOCKED = "Unlock with the write token above first";
const REFETCH = ["settings.sensors", "sensors"];

// the antenna's fields: name, gain (optional), bands, connector, note
function AntennaFields({ value, onChange }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  return (
    <div className="antenna-fields">
      <label className="fl" style={{ flex: "2 1 220px" }}>Name
        <input className="in" value={value.name} maxLength={80} onChange={set("name")} placeholder="e.g. dipole, 5 dBi" />
      </label>
      <label className="fl" style={{ flex: "1 1 130px" }}>Gain (dBi, optional)
        <input type="number" className="in" step="0.5" value={value.gain_dbi} onChange={set("gain_dbi")} />
      </label>
      <label className="fl" style={{ flex: "1 1 130px" }}>Connector
        <input className="in" value={value.connector} maxLength={40} placeholder="RP-SMA" onChange={set("connector")} />
      </label>
      <fieldset className="bands">
        <legend className="fl">Bands</legend>
        {["2.4", "5", "6"].map((b) => (
          <label key={b} className="check">
            <input type="checkbox" checked={value.bands.includes(b)}
                   onChange={(e) => onChange({ ...value, bands: e.target.checked ? [...value.bands, b] : value.bands.filter((x) => x !== b) })} />
            {b} GHz
          </label>
        ))}
      </fieldset>
    </div>
  );
}

export default function HardwareCard({ d, tz }) {
  const w = useWrite();
  const [params] = useSearchParams();
  const [modal, setModal] = useState(null);                  // {sensor} | "catalog"
  const [msg, setMsg] = useState(null);
  const done = (text) => { setModal(null); setMsg(text); };
  const first = params.get("sensor");
  return (
    <section className="card set-card" aria-labelledby="hw-hw">
      <div className="set-head set-head-row">
        <div className="stack">
          <h2 id="hw-hw">Capture hardware</h2>
          <span className="muted small">
            A change is a data boundary: an antenna’s gain shifts every RSSI, so RSSI baselines start again from it
            (evil twin (b) waits until the new hardware has 200 readings on 2 days) and measurements before and after
            are evaluated apart. Only the antenna is entered by hand; the adapter is detected from the sensor in the
            live-upload phase.
          </span>
        </div>
        <button type="button" className="btn2 small-btn" onClick={() => { setMsg(null); setModal("catalog"); }}>Manage catalog</button>
      </div>
      <div className="hw-lines">
        {d.sensors.map((s) => (
          <div key={s.name} className={s.name === first ? "hw-line picked" : "hw-line"}>
            <span className="em hw-sensor">{s.name}</span>
            <span className="hw-now">
              {captureText(s.capture, d.catalog)}
              {s.capture && <span className="muted"> · since {fmtDate(s.capture.valid_from, tz)} {new Date(s.capture.valid_from).getUTCFullYear()}</span>}
            </span>
            <button type="button" className="btn2" disabled={!w.unlocked} title={w.unlocked ? undefined : LOCKED}
                    onClick={() => { setMsg(null); setModal({ sensor: s.name }); }}>Edit / record change</button>
          </div>
        ))}
      </div>
      {msg && <span className="save-msg" role="status"><span className="ok-text">{msg}</span></span>}
      {modal === "catalog" && <CatalogModal d={d} onClose={() => setModal(null)} />}
      {modal?.sensor && <ChangeModal d={d} tz={tz} sensor={d.sensors.find((s) => s.name === modal.sensor)}
                                     onClose={() => setModal(null)} onDone={done} />}
    </section>
  );
}

// record a hardware change: pick the antenna (or give a new one), when it was attached
function ChangeModal({ d, tz, sensor, onClose, onDone }) {
  const w = useWrite();
  const adapters = d.catalog.filter((x) => x.kind === "adapter");
  const antennas = d.catalog.filter((x) => x.kind === "antenna");
  const cap = sensor.capture;
  const [adapter, setAdapter] = useState(String(cap?.adapter_id ?? adapters[0]?.id ?? ""));
  const [antenna, setAntenna] = useState(cap?.antenna_id ? String(cap.antenna_id) : "");   // "" = its own, "new"
  const [fresh, setFresh] = useState(EMPTY_ANTENNA);
  const [since, setSince] = useState(() => utcToZoned(new Date(), tz));
  const [note, setNote] = useState("");
  const [state, setState] = useState({ busy: false, error: null });
  const sinceUtc = zonedToUtc(since, tz);
  const same = cap && Number(adapter) === cap.adapter_id && antenna !== "new" && (Number(antenna) || null) === cap.antenna_id;
  const problem = !adapter ? "the catalog has no adapter" : same ? "that is the hardware attached now"
    : !sinceUtc ? "give the time it was attached" : antenna === "new" ? antennaProblem(fresh) : null;
  const save = async (ev) => {
    ev.preventDefault();
    if (problem) return;
    setState({ busy: true, error: null });
    try {
      let antennaId = antenna ? Number(antenna) : null;
      if (antenna === "new") {
        const out = await w.send("POST", "/api/v2/settings/hardware", antennaBody(fresh), REFETCH);
        antennaId = out.item.id;
      }
      await w.send("POST", `/api/v2/settings/sensors/${encodeURIComponent(sensor.name)}/hardware`,
                   { adapter_id: Number(adapter), antenna_id: antennaId, valid_from: sinceUtc, note: note || null }, REFETCH);
      onDone(`Recorded for ${sensor.name}: a data boundary from ${fmtDateTime(sinceUtc, tz)}. Its RSSI baselines start again with the next import.`);
    } catch (e) {
      setState({ busy: false, error: e.message });
    }
  };
  return (
    <Modal title={`Capture hardware of ${sensor.name}`} onClose={onClose}>
      <form className="modal-form" onSubmit={save}>
        <div className="muted small">Now: {captureText(cap, d.catalog)}</div>
        <label className="fl">Antenna
          <select className="in" value={antenna} onChange={(e) => setAntenna(e.target.value)}>
            <option value="">none recorded (the adapter’s own antenna)</option>
            {antennas.map((hw) => <option key={hw.id} value={hw.id}>{hwText(hw)}{hwDetail(hw) ? ` · ${hwDetail(hw)}` : ""}</option>)}
            <option value="new">+ a new antenna…</option>
          </select>
        </label>
        {antenna === "new" && <AntennaFields value={fresh} onChange={setFresh} />}
        {adapters.length > 1 ? (
          <label className="fl">Adapter
            <select className="in" value={adapter} onChange={(e) => setAdapter(e.target.value)}>
              {adapters.map((hw) => <option key={hw.id} value={hw.id}>{hwText(hw)}</option>)}
            </select>
          </label>
        ) : (
          <div className="muted small">Adapter: {hwText(adapters[0])} - detected from the sensor in the live-upload phase, not typed.</div>
        )}
        {Number(adapter) !== cap?.adapter_id && (
          <div className="warn-box">
            Another adapter also changes the RSSI floor values: −106 / −120 dBm are the RTL8821CU’s (rssi_valid()).
            The new adapter’s readings need their own floors before its RSSI is trusted.
          </div>
        )}
        <label className="fl">Attached since ({tz})
          <input type="datetime-local" className="in" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="fl">Note (optional)
          <input className="in" value={note} maxLength={500} placeholder="e.g. antenna moved to the window" onChange={(e) => setNote(e.target.value)} />
        </label>
        {state.error && <span className="bad-text" role="alert">{state.error}</span>}
        <div className="modal-foot">
          <button type="button" className="btn2" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn" disabled={Boolean(problem) || state.busy} title={problem ?? undefined}>
            {state.busy ? "Saving…" : "Record the change"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// the catalog: antennas (add / edit / delete an unused one), the adapters read-only
function CatalogModal({ d, onClose }) {
  const w = useWrite();
  const [item, setItem] = useState(EMPTY_ANTENNA);
  const [state, setState] = useState({ busy: false, ok: null, error: null });
  useEffect(() => { setItem(EMPTY_ANTENNA); }, [d.catalog.length]);
  const antennas = d.catalog.filter((x) => x.kind === "antenna");
  const adapters = d.catalog.filter((x) => x.kind === "adapter");
  const problem = antennaProblem(item);
  const run = async (fn, ok) => {
    setState({ busy: true, ok: null, error: null });
    try {
      await fn();
      setState({ busy: false, ok, error: null });
    } catch (e) {
      setState({ busy: false, ok: null, error: e.message });
    }
  };
  const save = (ev) => {
    ev.preventDefault();
    if (problem) return;
    const body = antennaBody(item);
    run(async () => {
      await w.send(item.id ? "PUT" : "POST", item.id ? `/api/v2/settings/hardware/${item.id}` : "/api/v2/settings/hardware",
                   body, REFETCH);
      setItem(EMPTY_ANTENNA);
    }, item.id ? `Saved ${body.name}.` : `Added ${body.name} to the catalog.`);
  };
  return (
    <Modal title="Capture hardware catalog" wide onClose={onClose}>
      <span className="em">Antennas</span>
      <table>
        <thead><tr><th>Name</th><th>Gain</th><th>Bands / connector</th><th><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {antennas.length === 0 && <tr><td colSpan={4} className="muted">No antenna yet.</td></tr>}
          {antennas.map((hw) => (
            <tr key={hw.id}>
              <td className="em">{hw.name}</td>
              <td>{hw.gain_dbi == null ? <span className="muted">–</span> : `${hw.gain_dbi} dBi`}</td>
              <td>{hwDetail(hw) || "–"}</td>
              <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                <button type="button" className="btn2" onClick={() => setItem(antennaDraft(hw))}>Edit</button>{" "}
                {hw.used ? <span className="muted tiny" title="in a sensor's hardware history - it stays">in use</span> : (
                  <button type="button" className="btn2 danger" disabled={state.busy}
                          onClick={() => run(() => w.send("DELETE", `/api/v2/settings/hardware/${hw.id}`, undefined, REFETCH),
                                             `Removed ${hw.name} from the catalog.`)}>Delete</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="modal-form" onSubmit={save}>
        <span className="em">{item.id ? `Edit ${item.name || "the antenna"}` : "Add an antenna"}</span>
        <AntennaFields value={item} onChange={setItem} />
        <div className="modal-foot">
          {state.error && <span className="bad-text grow" role="alert">{state.error}</span>}
          {!state.error && state.ok && <span className="ok-text grow" role="status">{state.ok}</span>}
          {item.id && <button type="button" className="btn2" onClick={() => setItem(EMPTY_ANTENNA)}>Cancel edit</button>}
          <button type="submit" className="btn" disabled={Boolean(problem) || state.busy} title={problem ?? undefined}>
            {item.id ? "Save" : "Add antenna"}
          </button>
        </div>
      </form>
      <span className="em">Adapters</span>
      <table>
        <tbody>
          {adapters.map((hw) => (
            <tr key={hw.id}><td className="em">{hw.name}</td><td>{hwDetail(hw) || "–"}</td>
              <td className="muted small">{hw.note ?? ""}</td></tr>
          ))}
        </tbody>
      </table>
      <span className="muted small">
        Adapters are not typed: the sensor reports its adapter (USB VID:PID, chipset, driver, MAC, interface) in the
        live-upload phase and the RSSI floors follow from the chipset. Until then the seeded one stands.
      </span>
    </Modal>
  );
}
