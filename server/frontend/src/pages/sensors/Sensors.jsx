// Sensors (/sensors, boards/Sensors.dc.html): a card per sensor (status and
// hop list, hardware, last poll and upload, coverage of the last 7 days,
// capture crashes of the last 24 h, the buffer, APs seen, the capture
// configuration and since when it is in effect), the coverage per day of the
// last 14 days and the outages and limited periods of one sensor (the
// segmented control with more than one sensor; ?s= in the URL). No filters:
// every sensor of the account. The 7-day and 24-hour figures end at the
// sensor's last poll (SPEC Q7). "Sensor settings" leads to Settings (phase 5).

import { Fragment } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { sensorState } from "../../app/status.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useSensors } from "../../hooks/useFilters.js";
import { useNow } from "../../hooks/useNow.js";
import { fmtDateTime } from "../../format.js";
import CoverageChart from "./CoverageChart.jsx";
import {
  adapterText, apsText, bufferText, configText, coverageColumns, coverageText, coverageTitle, crashText, footText,
  headText, hopBadge, lastPollText, legendItems, liveText, localDate, outageRows, PLANS, restartText, sinceText, stateNote,
  uploadText,
} from "./sensors.js";
import "./sensors.css";

export default function Sensors() {
  const account = useAccount();
  const now = useNow(30_000);
  const q = useSensors();
  const d = q.data;
  const tz = account.tz;
  const [params, setParams] = useSearchParams();
  const names = d?.sensors.map((s) => s.name) ?? [];
  const pick = names.includes(params.get("s")) ? params.get("s") : names[0];
  const choose = (name) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (name === names[0]) next.delete("s"); else next.set("s", name);
    return next;
  }, { replace: true });
  return (
    <main className="content v2" style={{ gap: 20 }}>
      <div className="sensors-head">
        <h1>Sensors</h1>
        <span className="muted">{d ? headText(d.sensors, d.offline_after_min, now, tz) : "…"}</span>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={320} />}
      {d && (
        <div className="sen-cards">
          {d.sensors.map((s) => <SensorCard key={s.name} s={s} account={account} now={now} n={d.sensors.length} />)}
          {d.sensors.length === 0 && <section className="card sen-card muted">No sensor in the account yet.</section>}
        </div>
      )}
      {pick && <SensorHistory name={pick} names={names} onPick={choose} tz={tz} now={now} />}
    </main>
  );
}

function SensorCard({ s, account, now, n }) {
  const tz = account.tz;
  const st = sensorState(s, account.offlineMin, now, tz);
  const hop = hopBadge(s);
  const note = stateNote(s, st, tz);
  const foot = footText(s, tz);
  const restarts = restartText(s.crashes_24h);
  const ends = s.last_poll ? `ends at the last poll, ${fmtDateTime(s.last_poll, tz)}` : undefined;
  const rows = [
    ["Hardware", s.hardware ?? "–"],
    ["Capture adapter", adapterText(s.adapter, s.last_hardware_change, tz),
     "read-only; a hardware change entered in Settings is a data boundary: RSSI baselines start again from it"],
    ["Last poll", lastPollText(s, now, tz)],
    ["Last upload", uploadText(s.last_upload, now, tz)],
    ...(s.contact ? [["Live upload", liveText(s, now),
                      "the sensor's contact (immediate); clock: the server's view of the sensor's clock; "
                      + "lost: polls pruned on the sensor before upload; time-unverified: polls of a boot that never got a clock anchor"]] : []),
    ["Coverage, last 7 days", coverageText(s), coverageTitle(s, tz)],
    ["Capture crashes, last 24 h", restarts ? `${crashText(s.crashes_24h)} · ${restarts}` : crashText(s.crashes_24h), ends],
    ["Buffer on the sensor", bufferText(s.last_upload),
     s.last_upload ? `at the ${s.last_upload.via === "ingest" ? "upload" : "import"} of ${fmtDateTime(s.last_upload.at, tz)}` : undefined],
    ["APs seen, last 7 days", apsText(s, n), ends],
    ["Capture configuration", configText(s.config), PLANS[s.config?.plan]?.label],
  ];
  return (
    <section className="card sen-card" aria-labelledby={`sen-${s.name}`}>
      <div className="sen-top">
        <div className="stack">
          <h2 id={`sen-${s.name}`}>{s.name}</h2>
          <span className="muted small">{[s.location, sinceText(s, tz)].filter(Boolean).join(" · ")}</span>
        </div>
        <div className="badges">
          <span className={`b ${st.tone}`}>{st.badge}</span>
          {hop && <span className={`b ${hop.cls}`} title={hop.title}>{hop.text}</span>}
        </div>
      </div>
      <dl className="kv sen-kv">
        {rows.map(([dt, dd, title]) => (
          <Fragment key={dt}><dt>{dt}</dt><dd title={title}>{dd}</dd></Fragment>
        ))}
      </dl>
      {note && <div className={note.bad ? "sen-note bad-note" : "sen-note"}>{note.text}</div>}
      <div className="sen-foot">
        <span className={foot.warn ? "small warn-text" : "muted small"}>{foot.text}</span>
        <Link to={`/settings/sensors?sensor=${encodeURIComponent(s.name)}`} className="btn2 sen-btn">Sensor settings</Link>
      </div>
    </section>
  );
}

// coverage per day and the outages of one sensor (both from its endpoints)
function SensorHistory({ name, names, onPick, tz, now }) {
  const [ck, cu] = ep.sensorCoverage(name, 14);
  const cov = useQuery(ck, cu);
  const [ok, ou] = ep.sensorOutages(name);
  const out = useQuery(ok, ou);
  const items = out.data?.items;
  const cols = cov.data ? coverageColumns(cov.data.days, name, items, tz, localDate(now, tz)) : null;
  const rows = outageRows(items, tz);
  return (
    <>
      <section className="card sen-panel" aria-labelledby="cov-h">
        <div className="sen-panel-head">
          <div className="stack" style={{ gap: 6 }}>
            <h2 id="cov-h">Coverage per day – last 14 days{names.length === 1 ? ` · ${name}` : ""}</h2>
            <div className="lg">
              {legendItems(cov.data?.days).map((c) => (
                <span key={c.key}>
                  <span className={c.hatch ? "sw hatch" : "sw"} style={c.hatch ? undefined : { background: c.color }} />{c.label}
                </span>
              ))}
            </div>
          </div>
          {names.length > 1 && (
            <div role="group" aria-label="Sensor" className="seg">
              {names.map((n) => (
                <button key={n} type="button" aria-pressed={n === name} onClick={() => onPick(n)}>{n}</button>
              ))}
            </div>
          )}
        </div>
        {cov.error && <ErrorBox error={cov.error} />}
        {!cols && !cov.error && <Skeleton height={160} />}
        {cols && <CoverageChart cols={cols} />}
      </section>

      <section className="card table-card sen-table" aria-labelledby="gap-h">
        <div style={{ padding: "0 22px 10px" }}><h2 id="gap-h">Outages and limited periods – {name}</h2></div>
        {out.error && <div style={{ padding: "0 22px 14px" }}><ErrorBox error={out.error} /></div>}
        {!items && !out.error && <div style={{ padding: "0 22px 14px" }}><Skeleton height={160} /></div>}
        {items && (
          <div className="scroll-x">
            <table className="t">
              <thead><tr><th>From</th><th>Duration</th><th>Type</th><th>Cause</th></tr></thead>
              <tbody>
                {rows.length === 0 && <tr><td colSpan={4} className="muted">No outage or limited period so far.</td></tr>}
                {rows.map((r) => (
                  <tr key={r.key}>
                    <td className="nowrap" title={r.title}>{r.from}</td>
                    <td className="nowrap">{r.duration}</td>
                    <td><span className={`b ${r.type.cls}`}>{r.type.text}</span></td>
                    <td className={r.cause === "not documented" ? "muted" : undefined}>
                      {r.cause}
                      {r.documented && <span className="muted tiny" title="from the sensor's documented history, not visible in its polls"> · documented</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
