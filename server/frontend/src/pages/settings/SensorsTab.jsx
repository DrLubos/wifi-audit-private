// Settings > Sensors (SettingsSensors board): the capture configuration for
// the selected sensors (saved as the desired configuration - not delivered
// until the sensor upload exists, Q10), each sensor's location and
// description, its capture hardware (adapter and antenna from the catalog;
// a change is a data boundary - RSSI baselines start again from it, schema
// 11) with the catalog itself, and the configuration history with its data
// boundaries. ?sensor=<name> preselects a sensor (the Sensors page link).

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { sensorState } from "../../app/status.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useNow } from "../../hooks/useNow.js";
import { useSave } from "./form.jsx";
import HardwareCard from "./HardwareCard.jsx";
import SensorList from "./SensorList.jsx";
import { configState, cycleText, DWELLS, historyRows, planList, PLANS, planText, POLLS } from "./settings.js";

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
      <SensorList d={d} />
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
