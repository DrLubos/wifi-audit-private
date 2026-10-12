// Settings > Sensors (SettingsSensors board): the capture configuration for
// the selected sensors (saved as the desired configuration - not delivered
// until the sensor upload exists, Q10), each sensor's location and
// description, its capture hardware (adapter and antenna from the catalog;
// a change is a data boundary - RSSI baselines start again from it, schema
// 11) with the catalog itself, and the configuration history with its data
// boundaries. ?sensor=<name> preselects a sensor (the Sensors page link).

import { Fragment, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { sensorState } from "../../app/status.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useNow } from "../../hooks/useNow.js";
import { fmtDate, fmtDateTime } from "../../format.js";
import { useSave } from "./form.jsx";
import {
  captureText, configState, cycleText, DWELLS, historyRows, hwDetail, hwText, planList, PLANS, planText, POLLS,
  utcToZoned, zonedToUtc,
} from "./settings.js";

export default function SensorsTab() {
  const [key, url] = ep.settingsSensors();
  const q = useQuery(key, url);
  const d = q.data;
  const account = useAccount();
  if (q.error) return <ErrorBox error={q.error} />;
  if (!d) return <Skeleton height={420} />;
  return (
    <>
      <ConfigCard d={d} account={account} />
      <DetailsCard d={d} />
      <HardwareCard d={d} tz={account.tz} />
      <HistoryCard d={d} tz={account.tz} />
    </>
  );
}

function SaveMsg({ save }) {
  return (
    <span className="save-msg" role="status">
      {save.state.error && <span className="bad-text">{save.state.error}</span>}
      {!save.state.error && save.state.ok && <span className="ok-text">{save.state.ok}</span>}
    </span>
  );
}

const lockedWhy = (w) => (w.unlocked ? undefined : "Unlock with the write token above first");

function ConfigCard({ d, account }) {
  const [params] = useSearchParams();
  const now = useNow(60_000);
  const names = d.sensors.map((s) => s.name);
  const [sel, setSel] = useState(() => {
    const want = params.get("sensor");
    return names.includes(want) ? [want] : names.slice(0, 1);
  });
  const first = d.sensors.find((s) => sel.includes(s.name));
  const base = first?.pending ?? first?.current;
  const [plan, setPlan] = useState(PLANS.some((p) => p.key === base?.channel_plan) ? base.channel_plan : "all_89");
  const [lock, setLock] = useState(base?.channel_plan === "locked" ? base.channels?.[0] ?? "6" : "6");
  const [custom, setCustom] = useState(base?.channel_plan === "custom" ? (base.channels ?? []).join(",") : "");
  const [dwell, setDwell] = useState(base?.dwell_s ?? 0.2);
  const [poll, setPoll] = useState(base?.poll_s ?? 30);
  const save = useSave();
  const live = Object.fromEntries((account.sensors ?? []).map((s) => [s.name, s]));
  const picked = planList(plan, d.plans, lock, custom);
  const toggle = (n) => setSel(sel.includes(n) ? sel.filter((x) => x !== n) : [...sel, n]);
  const all = sel.length === names.length;
  const plansOf = new Set(d.sensors.filter((s) => sel.includes(s.name) && s.current).map((s) => s.current.channel_plan));
  const label = !sel.length ? "Select at least one sensor" : sel.length === 1 ? `Save for ${sel[0]}` : `Save for ${sel.length} sensors`;
  const why = lockedWhy(save.w) || (!sel.length ? "Select at least one sensor" : picked.error);
  const onSave = () => save.run(
    (w) => w.send("POST", "/api/v2/settings/sensors", {
      sensors: sel,
      config: { channel_plan: plan, channels: plan === "custom" ? picked.list : undefined,
                lock_channel: plan === "locked" ? lock : undefined, dwell_s: dwell, poll_s: poll },
    }, ["settings.sensors", "sensors"]),
    (out) => `Saved as version ${out.sensors.map((s) => `${s.version} for ${s.name}`).join(", ")} - not delivered: the sensor upload is not built yet.`,
  );
  return (
    <section className="card set-card" aria-labelledby="sen-h">
      <div className="set-head">
        <h2 id="sen-h">Capture configuration</h2>
        <span className="muted small">
          Select one, several or all sensors and give them a common configuration. It is saved as the desired
          configuration; delivery to the sensor comes with the sensor upload (a later phase), until then it waits
          as “saved, not delivered”. “Sensor settings” on the Sensors page opens this tab with that sensor selected.
        </span>
      </div>
      <div className="scroll-x">
        <table className="t nw">
          <thead>
            <tr>
              <th>
                <label className="check">
                  <input type="checkbox" checked={all} onChange={() => setSel(all ? [] : names)} />All
                </label>
              </th>
              <th>Sensor</th><th>Status</th><th>Channel plan</th><th>Dwell</th><th>Poll</th><th>Configuration</th>
            </tr>
          </thead>
          <tbody>
            {d.sensors.map((s) => {
              const st = live[s.name] ? sensorState(live[s.name], account.offlineMin, now, account.tz) : null;
              const cs = configState(s, account.tz);
              const c = s.current;
              return (
                <tr key={s.name} className={sel.includes(s.name) ? "picked" : undefined}>
                  <td>
                    <label className="check">
                      <input type="checkbox" checked={sel.includes(s.name)} onChange={() => toggle(s.name)} />
                      <span className="sr-only">Select {s.name}</span>
                    </label>
                  </td>
                  <td className="em">{s.name}</td>
                  <td>{st && <span className={`b ${st.tone}`}>{st.badge}</span>}</td>
                  <td>{planText(c)}{s.pending && <> → <b className="em">{planText(s.pending)}</b></>}</td>
                  <td>{c ? `${c.dwell_s} s` : "–"}</td>
                  <td>{c ? `${c.poll_s} s` : "–"}{s.pending && s.pending.poll_s !== c?.poll_s && <> → <b className="em">{s.pending.poll_s} s</b></>}</td>
                  <td><span className={`b ${cs.cls}`} title={cs.title}>{cs.text}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {plansOf.size > 1 && (
        <div className="warn-box">
          The selected sensors use different channel plans now ({[...plansOf].map((p) => planText({ channel_plan: p })).join(" and ")}).
          After saving all get the same one, so their measurements become directly comparable.
        </div>
      )}
      <div className={sel.length ? "cfg-form" : "cfg-form off"}>
        <fieldset className="plans">
          <legend className="em">Channel plan</legend>
          {PLANS.map((p) => {
            const n = p.key === "all_89" ? d.plans.all_89.length : p.key === "ht20_37" ? d.plans.ht20_37.length : null;
            return (
              <label key={p.key} className={plan === p.key ? "plan on" : "plan"}>
                <input type="radio" name="plan" checked={plan === p.key} onChange={() => setPlan(p.key)} />
                <span className="stack">
                  <span className="em">{p.title}</span>
                  <span className="muted small">{n ? `${cycleText(n, dwell)} · ` : ""}{p.desc}</span>
                </span>
              </label>
            );
          })}
          {plan === "locked" && (
            <label className="fl plan-extra">Channel
              <select className="in sel-sm" value={lock} onChange={(e) => setLock(e.target.value)}>
                {d.plans.lock.map((c) => <option key={c} value={c}>channel {c}</option>)}
              </select>
            </label>
          )}
          {plan === "custom" && (
            <label className="fl plan-extra">Channels, comma separated
              <input type="text" className="in mono" placeholder="1,6,11,36,40,44,48" value={custom}
                     onChange={(e) => setCustom(e.target.value)} />
              <span className={picked.error && custom ? "bad-text" : "muted"}>
                {picked.error && custom ? picked.error
                  : picked.list ? `${picked.list.length} entries · ${cycleText(picked.list.length, dwell)} · every entry visited`
                    : "The list length is checked: it must be coprime with Kismet's hop stride, otherwise some channels are never visited."}
              </span>
            </label>
          )}
        </fieldset>
        <div className="cfg-side">
          <label className="fl"><span className="em">Dwell time per channel</span>
            <select className="in" value={dwell} onChange={(e) => setDwell(Number(e.target.value))}>
              {DWELLS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <span className="muted tiny">Longer = more frames per channel, but a longer cycle.</span>
          </label>
          <label className="fl"><span className="em">Collector poll interval</span>
            <select className="in" value={poll} onChange={(e) => setPoll(Number(e.target.value))}>
              {POLLS.map((v) => <option key={v} value={v}>{v} s</option>)}
            </select>
            <span className="muted tiny">Shorter = finer RSSI series, ~2× more data on the sensor and the server.</span>
          </label>
          <div className="info-box">
            <span className="em">Upload to the server: not a setting</span>
            <span>Snapshot imports by the developer for now; later automatic after every poll, with the backlog of the 14-day buffer after an outage.</span>
          </div>
          <button type="button" className="btn" disabled={Boolean(why) || save.state.busy} title={why} onClick={onSave}>
            {save.state.busy ? "Saving…" : label}
          </button>
          <SaveMsg save={save} />
        </div>
      </div>
    </section>
  );
}

function DetailsCard({ d }) {
  const save = useSave();
  const [edit, setEdit] = useState(() => Object.fromEntries(d.sensors.map((s) => [s.name, {
    location: s.meta?.location ?? "", description: s.meta?.description ?? "" }])));
  useEffect(() => {
    setEdit((prev) => Object.fromEntries(d.sensors.map((s) => [s.name, prev[s.name] ?? {
      location: s.meta?.location ?? "", description: s.meta?.description ?? "" }])));
  }, [d]);
  return (
    <section className="card set-card" aria-labelledby="det-sen-h">
      <div className="set-head">
        <h2 id="det-sen-h">Sensor details</h2>
        <span className="muted small">Where a sensor is and what it is for - shown on the Sensors page. Free text, no coordinates.</span>
      </div>
      {d.sensors.map((s) => {
        const e = edit[s.name] ?? { location: "", description: "" };
        const changed = e.location.trim() !== (s.meta?.location ?? "") || e.description.trim() !== (s.meta?.description ?? "");
        const why = lockedWhy(save.w) || (!changed ? "Nothing changed" : undefined);
        const set = (k) => (ev) => setEdit({ ...edit, [s.name]: { ...e, [k]: ev.target.value } });
        return (
          <form key={s.name} className="meta-row" onSubmit={(ev) => {
            ev.preventDefault();
            save.run((w) => w.send("PUT", `/api/v2/settings/sensors/${encodeURIComponent(s.name)}`,
                                   { location: e.location, description: e.description }, ["settings.sensors", "sensors"]),
                     `Saved the details of ${s.name}.`);
          }}>
            <span className="em meta-name">{s.name}</span>
            <label className="fl" style={{ flex: "1 1 220px" }}>Location
              <input className="in" value={e.location} maxLength={120} onChange={set("location")} />
            </label>
            <label className="fl" style={{ flex: "2 1 300px" }}>Description
              <input className="in" value={e.description} maxLength={500} onChange={set("description")} />
            </label>
            <button type="submit" className="btn2 btn-lg" disabled={Boolean(why) || save.state.busy} title={why}>Save</button>
          </form>
        );
      })}
      <SaveMsg save={save} />
    </section>
  );
}

const EMPTY_ITEM = { id: null, kind: "antenna", name: "", gain_dbi: "", bands: ["2.4", "5"], connector: "", note: "" };

function HardwareCard({ d, tz }) {
  const [params] = useSearchParams();
  const save = useSave();
  const names = d.sensors.map((s) => s.name);
  const [sensor, setSensor] = useState(names.includes(params.get("sensor")) ? params.get("sensor") : names[0]);
  const s = d.sensors.find((x) => x.name === sensor);
  const adapters = d.catalog.filter((x) => x.kind === "adapter");
  const antennas = d.catalog.filter((x) => x.kind === "antenna");
  const [adapter, setAdapter] = useState(s?.capture?.adapter_id ?? adapters[0]?.id ?? "");
  const [antenna, setAntenna] = useState(s?.capture?.antenna_id ?? "");
  const [since, setSince] = useState(() => utcToZoned(new Date(), tz));
  const [note, setNote] = useState("");
  const [item, setItem] = useState(EMPTY_ITEM);
  const by = useMemo(() => Object.fromEntries(d.catalog.map((x) => [x.id, x])), [d.catalog]);
  useEffect(() => {
    setAdapter(s?.capture?.adapter_id ?? adapters[0]?.id ?? "");
    setAntenna(s?.capture?.antenna_id ?? "");
  }, [sensor, d]); // eslint-disable-line react-hooks/exhaustive-deps
  const same = s?.capture && Number(adapter) === s.capture.adapter_id && (Number(antenna) || null) === s.capture.antenna_id;
  const sinceUtc = zonedToUtc(since, tz);
  const adapterChange = s?.capture && Number(adapter) !== s.capture.adapter_id;
  const why = lockedWhy(save.w) || (!adapter ? "Add an adapter to the catalog first" : same ? "That is the hardware attached now"
    : !sinceUtc ? "Give the time it was attached" : undefined);
  const record = () => save.run(
    (w) => w.send("POST", `/api/v2/settings/sensors/${encodeURIComponent(sensor)}/hardware`, {
      adapter_id: Number(adapter), antenna_id: antenna ? Number(antenna) : null, valid_from: sinceUtc, note: note || null,
    }, ["settings.sensors", "sensors"]),
    `Recorded for ${sensor}: a data boundary from ${fmtDateTime(sinceUtc, tz)}. Its RSSI baselines start again with the next import.`,
  );
  const itemWhy = lockedWhy(save.w) || (!item.name.trim() ? "Give it a name" : !item.bands.length ? "Pick its bands" : undefined);
  const saveItem = () => save.run(
    (w) => w.send(item.id ? "PUT" : "POST", item.id ? `/api/v2/settings/hardware/${item.id}` : "/api/v2/settings/hardware", {
      kind: item.kind, name: item.name, gain_dbi: item.gain_dbi === "" ? null : Number(item.gain_dbi), bands: item.bands,
      connector: item.connector || null, note: item.note || null,
    }, ["settings.sensors"]).then((out) => { setItem(EMPTY_ITEM); return out; }),
    item.id ? `Saved ${item.name}.` : `Added ${item.name} to the catalog.`,
  );
  return (
    <section className="card set-card" aria-labelledby="hw-h">
      <div className="set-head">
        <h2 id="hw-h">Capture hardware</h2>
        <span className="muted small">
          The adapter and antenna each sensor has attached, from the catalog below. A change is a data boundary: an
          antenna’s gain shifts every RSSI, so RSSI baselines start again from it (evil twin (b) waits until the
          new hardware has 200 readings on 2 days) and measurements before and after are evaluated apart.
        </span>
      </div>
      <dl className="kv">
        {d.sensors.map((x) => (
          <Fragment key={x.name}>
            <dt>{x.name}</dt>
            <dd>{captureText(x.capture, d.catalog)}{x.capture && <span className="muted"> · since {fmtDate(x.capture.valid_from, tz)} {new Date(x.capture.valid_from).getUTCFullYear()}</span>}</dd>
          </Fragment>
        ))}
      </dl>
      <form className="hw-form" onSubmit={(e) => { e.preventDefault(); if (!why) record(); }}>
        <span className="em hw-title">Record a hardware change</span>
        {names.length > 1 && (
          <label className="fl">Sensor
            <select className="in sel-sm" value={sensor} onChange={(e) => setSensor(e.target.value)}>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        )}
        <label className="fl">Adapter
          <select className="in" value={adapter} onChange={(e) => setAdapter(e.target.value)}>
            {adapters.map((hw) => <option key={hw.id} value={hw.id}>{hwText(hw)}</option>)}
          </select>
        </label>
        <label className="fl">Antenna
          <select className="in" value={antenna ?? ""} onChange={(e) => setAntenna(e.target.value)}>
            <option value="">the adapter’s own antenna</option>
            {antennas.map((hw) => <option key={hw.id} value={hw.id}>{hwText(hw)}</option>)}
          </select>
        </label>
        <label className="fl">Attached since ({tz})
          <input type="datetime-local" className="in" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="fl" style={{ flex: "1 1 220px" }}>Note
          <input className="in" value={note} maxLength={500} placeholder="e.g. antenna moved to the window" onChange={(e) => setNote(e.target.value)} />
        </label>
        <button type="submit" className="btn" disabled={Boolean(why) || save.state.busy} title={why}>Record the change</button>
      </form>
      {adapterChange && (
        <div className="warn-box">
          Another adapter also changes the RSSI floor values: −106 / −120 dBm are the RTL8821CU’s (rssi_valid()).
          The new adapter’s readings need their own floors before its RSSI is trusted.
        </div>
      )}
      <SaveMsg save={save} />

      <span className="em">Catalog</span>
      <div className="scroll-x">
        <table className="t">
          <thead><tr><th>Kind</th><th>Name</th><th>Gain</th><th>Bands / connector</th><th>Note</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {d.catalog.length === 0 && <tr><td colSpan={6} className="muted">The catalog is empty.</td></tr>}
            {d.catalog.map((hw) => (
              <tr key={hw.id}>
                <td>{hw.kind}</td>
                <td className="em">{hw.name}</td>
                <td>{hw.gain_dbi == null ? "–" : `${hw.gain_dbi} dBi`}</td>
                <td>{hwDetail(hw) || "–"}</td>
                <td className="muted small">{hw.note ?? ""}</td>
                <td className="nowrap">
                  <button type="button" className="btn2" onClick={() => setItem({
                    id: hw.id, kind: hw.kind, name: hw.name, gain_dbi: hw.gain_dbi ?? "", bands: hw.bands, connector: hw.connector ?? "", note: hw.note ?? "" })}>
                    Edit
                  </button>{" "}
                  {hw.used ? <span className="muted tiny" title="in a sensor's hardware history - it stays">in use</span> : (
                    <button type="button" className="btn2" disabled={!save.w.unlocked} title={lockedWhy(save.w)}
                            onClick={() => save.run((w) => w.send("DELETE", `/api/v2/settings/hardware/${hw.id}`, undefined, ["settings.sensors"]),
                                                    `Removed ${hw.name} from the catalog.`)}>
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="hw-form" onSubmit={(e) => { e.preventDefault(); if (!itemWhy) saveItem(); }}>
        <span className="em hw-title">{item.id ? `Edit ${by[item.id]?.name ?? "the item"}` : "Add to the catalog"}</span>
        <label className="fl">Kind
          <select className="in sel-sm" value={item.kind} disabled={Boolean(item.id)}
                  onChange={(e) => setItem({ ...item, kind: e.target.value })}>
            <option value="antenna">antenna</option><option value="adapter">adapter</option>
          </select>
        </label>
        <label className="fl" style={{ flex: "1 1 200px" }}>Name
          <input className="in" value={item.name} maxLength={80} onChange={(e) => setItem({ ...item, name: e.target.value })} />
        </label>
        <label className="fl">Gain (dBi)
          <input type="number" className="in num" step="0.5" value={item.gain_dbi} onChange={(e) => setItem({ ...item, gain_dbi: e.target.value })} />
        </label>
        <fieldset className="bands">
          <legend className="fl">Bands</legend>
          {["2.4", "5", "6"].map((b) => (
            <label key={b} className="check">
              <input type="checkbox" checked={item.bands.includes(b)}
                     onChange={(e) => setItem({ ...item, bands: e.target.checked ? [...item.bands, b] : item.bands.filter((x) => x !== b) })} />
              {b} GHz
            </label>
          ))}
        </fieldset>
        <label className="fl">Connector
          <input className="in" value={item.connector} maxLength={40} placeholder="RP-SMA" onChange={(e) => setItem({ ...item, connector: e.target.value })} />
        </label>
        <label className="fl" style={{ flex: "1 1 220px" }}>Note
          <input className="in" value={item.note} maxLength={500} onChange={(e) => setItem({ ...item, note: e.target.value })} />
        </label>
        <button type="submit" className="btn2 btn-lg" disabled={Boolean(itemWhy) || save.state.busy} title={itemWhy}>
          {item.id ? "Save" : "Add"}
        </button>
        {item.id && <button type="button" className="btn2 btn-lg" onClick={() => setItem(EMPTY_ITEM)}>Cancel</button>}
      </form>
    </section>
  );
}

function HistoryCard({ d, tz }) {
  const rows = historyRows(d.history, tz);
  return (
    <section className="card set-card" aria-labelledby="hist-h">
      <div className="set-head">
        <h2 id="hist-h">Configuration history</h2>
        <span className="muted small">
          Every channel-plan or hardware change is a boundary in the data; measurements before and after are evaluated separately.
        </span>
      </div>
      <div className="scroll-x">
        <table className="t">
          <thead><tr><th>Valid from</th><th>Sensor</th><th>Change</th><th>Effect on data</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={4} className="muted">No documented change.</td></tr>}
            {rows.map((r) => (
              <tr key={r.key}>
                <td className="nowrap">{r.ts}</td>
                <td>{r.sensor}</td>
                <td>{r.label}</td>
                <td>{r.boundary ? <><span className="b neu">boundary</span> {r.effect}</> : <span className="muted">{r.effect}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
