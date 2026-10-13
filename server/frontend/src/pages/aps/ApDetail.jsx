// The AP page (/aps/:bssid, boards/ApDetail.dc.html): identity, security and
// policy, current load, the classification (locked until the write token),
// signal strength by sensor over 7 days with the baselines, the clients by
// weekday and hour (4 weeks, the AP's own scale), the utilization of its
// channel (7 days), the configuration history, its events and the note.
// A Kismet device key of the previous pages (/aps/<key>) redirects here.

import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import Heatmap from "../../charts/Heatmap.jsx";
import { RAMP_COLORS, rampIndex, rampLabels } from "../../charts/ramp.js";
import { xLabels } from "../../charts/geometry.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useWrite } from "../../data/write.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useNow } from "../../hooks/useNow.js";
import { fmtDateTime, fmtInt, fmtPct } from "../../format.js";
import { cellText, siblingText } from "../networks/networks.js";
import { collecting, labelsOf } from "../overview/period.js";
import {
  bssidOfKey, changesPerDayText, channelDetail, channelLine, classBadge, CLASSES, encryptionText, eventBadges,
  historyRows, isActive, isBssid, lastSeenText, lastSeenTitle, latestCapture, loadNote, loadNumbers, mfpText,
  policyBadge, ratingBadge, recommendation, statusBadge, utilSummary,
} from "./aps.js";
import RssiChart from "./RssiChart.jsx";
import UtilColumns from "./UtilColumns.jsx";
import "./aps.css";

const COLORS = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)"];
const LOCKED = "Unlock with the write token in Settings first";
// after a classification: the rule events and every AP list change
const REFETCH_CLASS = ["ap|", "aps", "events", "event|", "networks", "overview"];
const HISTORY_SHOWN = 12;
const EVENTS_SHOWN = 5;
const AXIS = ["00:00", "06:00", "12:00", "18:00", "23:00"];

export default function ApDetail() {
  const { id } = useParams();
  const fromKey = bssidOfKey(id);
  if (fromKey) return <Navigate to={`/aps/${fromKey}`} replace />;
  if (!isBssid(id)) {
    return (
      <main className="content v2">
        <h1>Access point</h1>
        <p className="muted">“{id}” is not a BSSID. <Link to="/aps">All access points</Link></p>
      </main>
    );
  }
  return <ApPage bssid={id.toUpperCase().replaceAll("-", ":")} />;
}

function ApPage({ bssid }) {
  const account = useAccount();
  const now = useNow(30_000);
  const tz = account.tz;
  const [k, u] = ep.ap(bssid);
  const q = useQuery(k, u);
  const d = q.data;
  if (q.error) {
    return (
      <main className="content v2">
        <h1 className="mono">{bssid}</h1>
        <ErrorBox error={/\b404\b/.test(q.error) ? "This BSSID is not an access point of the account." : q.error} />
        <Link to="/aps">All access points</Link>
      </main>
    );
  }
  if (!d) return <main className="content v2"><Skeleton height={420} /></main>;
  const id = d.identity;
  const colors = Object.fromEntries(d.sensors.map((s, i) => [s.name, COLORS[i % COLORS.length]]));
  const name = id.network ?? (id.hidden ? "hidden network" : id.ssid || id.bssid);
  const st = statusBadge(isActive(id.last_seen, d.sensors, account.sensors));
  // "last seen" counts from the AP's sensors' latest capture (decided 2026-10-12)
  const ref = latestCapture(account.sensors, d.sensors.map((s) => s.name), now);
  const rating = ratingBadge(d.security.level);
  return (
    <main className="content v2 ap-page">
      <div className="ap-head">
        <div className="page-head" style={{ gap: 6 }}>
          <div className="title-row">
            <h1 className={id.network ? undefined : "hidden"}>{name}</h1>
            <span className={`b ${st.cls}`}>{st.text}</span>
            <span className={`b ${rating.cls}`}>{rating.text}</span>
          </div>
          <span className="muted">
            <span className="mono">{id.bssid}</span> · {id.manuf ?? (id.random_bssid ? "randomized BSSID" : "vendor unknown")}
            {" · "}{channelLine(id)} · seen by {d.sensors.length} {d.sensors.length === 1 ? "sensor" : "sensors"}
          </span>
          {id.sibling && <span className="muted small">{siblingText(id.sibling)} – a hint, the BSSIDs stay separate APs</span>}
        </div>
        <Classification bssid={bssid} c={d.classification} tz={tz} />
      </div>

      <div className="row-wrap">
        <Identity d={d} ref_={ref} tz={tz} />
        <Security sec={d.security} />
        <Load load={d.load} ref_={ref} tz={tz} />
      </div>

      <RssiCard bssid={bssid} sensors={d.sensors} colors={colors} tz={tz} ref_={ref} />

      <div className="row-wrap">
        <ClientsCard bssid={bssid} />
        <UtilCard bssid={bssid} channel={id.channel} tz={tz} now={now} />
      </div>

      <div className="row-wrap">
        <History d={d} tz={tz} />
        <Events events={d.events} bssid={bssid} tz={tz} />
      </div>

      <Note bssid={bssid} note={d.note} tz={tz} />
    </main>
  );
}

function Identity({ d, ref_, tz }) {
  const id = d.identity;
  return (
    <section className="card panel kv-card" aria-labelledby="id-h" style={{ flex: "1 1 340px" }}>
      <h2 id="id-h">Identity</h2>
      <dl className="kv">
        <dt>SSID</dt><dd>{id.hidden ? <i>hidden{id.name_seen ? ` (name seen: ${id.name_seen})` : ""}</i> : id.ssid}</dd>
        <dt>Network</dt>
        <dd>{id.network ? <Link to="/networks">{id.network}</Link> : "–"}{id.network_kind ? ` · ${id.network_kind}` : ""}</dd>
        <dt>BSSID</dt><dd className="mono">{id.bssid}{id.random_bssid ? " (randomized)" : ""}</dd>
        <dt>Vendor (OUI)</dt><dd>{id.manuf ?? "–"} · {id.oui}</dd>
        <dt>Channel</dt><dd>{channelDetail(id)}</dd>
        <dt>Country</dt><dd>{id.country ?? "–"}</dd>
        <dt>First seen</dt><dd>{fmtDateTime(id.first_seen, tz)}</dd>
        <dt>Last seen</dt><dd title={lastSeenTitle(id.last_seen, ref_, tz)}>{lastSeenText(id.last_seen, ref_, tz)}</dd>
      </dl>
    </section>
  );
}

function Security({ sec }) {
  const rating = ratingBadge(sec.level);
  const pol = policyBadge(sec.policy, sec.level);
  const advice = recommendation(sec.policy, sec.level);
  return (
    <section className="card panel kv-card" aria-labelledby="sec-h" style={{ flex: "1 1 340px" }}>
      <h2 id="sec-h">Security</h2>
      <dl className="kv">
        <dt>Encryption</dt><dd>{encryptionText(sec)}</dd>
        <dt>MFP (802.11w)</dt><dd>{mfpText(sec.mfp)}</dd>
        <dt>Rating</dt><dd><span className={`b ${rating.cls}`}>{rating.text}</span></dd>
        <dt>Network policy</dt>
        <dd>{pol ? <><span className={`b ${pol.cls}`}>{pol.text}</span>{pol.reason && <span className="muted"> {pol.reason}</span>}</>
          : <span className="muted">no policy (not a managed network)</span>}</dd>
      </dl>
      {advice && <div className="advice">Recommendation: {advice} <Link to="/settings/networks">Edit policy</Link></div>}
    </section>
  );
}

// the AP's own BSS Load report first, the sensor's cumulative count second
// and labelled so (decided 2026-10-12, SPEC section 4)
function Load({ load, ref_, tz }) {
  return (
    <section className="card panel" aria-labelledby="load-h" style={{ flex: "1 1 260px", gap: 12 }}>
      <h2 id="load-h">Current load</h2>
      <div className="bigs">
        {loadNumbers(load).map((x) => (
          <div className={x.secondary ? "big secondary" : "big"} key={x.label} title={x.title}>
            <span className="muted tiny">{x.label}</span><span className="num">{x.value}</span>
          </div>
        ))}
      </div>
      <span className="muted tiny">{loadNote(load, ref_, tz)}</span>
    </section>
  );
}

function RssiCard({ bssid, sensors, colors, tz, ref_ }) {
  const [k, u] = ep.apRssi(bssid, 7);
  const q = useQuery(k, u);
  const d = q.data;
  return (
    <section className="card panel" aria-labelledby="rssi-h" style={{ gap: 12 }}>
      <div className="card-head">
        <h2 id="rssi-h">Signal strength by sensor – last 7 days (hourly medians)</h2>
        <div className="lg">
          {sensors.map((s) => (
            <span key={s.name}><span className="line-key" style={{ background: colors[s.name] }} />{s.name}</span>
          ))}
          {sensors.filter((s) => s.baseline).map((s) => (
            <span key={`b-${s.name}`}>
              <span className="sw" style={{ background: colors[s.name], opacity: 0.25 }} />{s.name} baseline (median ± 2 robust σ)
            </span>
          ))}
        </div>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={240} />}
      {d && <RssiChart d={d} colors={colors} tz={tz} />}
      {d && <span className="muted tiny">The 7 days before the AP was last seen. Floor readings (−106 / −120 dBm) are the capture adapter's censored values, not levels: an hour with only those is a gap.</span>}
      <div className="scroll-x">
        <table className="t nw">
          <thead>
            <tr><th>Sensor</th><th>Median RSSI</th><th>Spread (robust σ)</th><th>Floor share (−106/−120)</th>
              <th>Observations (7 days)</th><th>First seen</th><th>Last seen</th></tr>
          </thead>
          <tbody>
            {sensors.map((s) => {
              const med = s.baseline?.median ?? s.rssi_typical;
              return (
                <tr key={s.name}>
                  <td><span className="sensor-key"><span className="line-key" style={{ background: colors[s.name] }} />{s.name}</span></td>
                  <td>{med == null ? "–" : `${String(Math.round(med)).replace("-", "−")} dBm`}</td>
                  <td>{s.baseline?.robust_sd == null ? "–" : `${s.baseline.robust_sd.toFixed(1)} dB`}</td>
                  <td>{s.baseline?.floor_share == null ? "–" : fmtPct(s.baseline.floor_share)}</td>
                  <td>{fmtInt(s.obs_7d)}</td>
                  <td>{fmtDateTime(s.first_capture ?? s.first_seen, tz)}</td>
                  <td title={lastSeenTitle(s.last_capture ?? s.last_seen, ref_, tz)}>{lastSeenText(s.last_capture ?? s.last_seen, ref_, tz)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {sensors.some((s) => s.baseline) && (
        <span className="muted tiny">
          Baseline: {sensors.filter((s) => s.baseline).map((s) => `${s.name} ${fmtInt(s.baseline.n_obs)} readings on `
            + `${s.baseline.n_days} days, ${fmtDateTime(s.baseline.window.from, tz)} – ${fmtDateTime(s.baseline.window.to, tz)}`).join("; ")}.
        </span>
      )}
    </section>
  );
}

function ClientsCard({ bssid }) {
  const [k, u] = ep.apLoad(bssid, "clients_week");
  const q = useQuery(k, u);
  const d = q.data;
  const rows = d ? d.rows.map((row) => ({
    key: row.label,
    label: row.label,
    cells: row.cells.map((cell, h) => ({
      bg: cell.state === "ok" ? RAMP_COLORS[rampIndex(cell.value, d.ramp)] : "var(--hatch)",
      text: cellText("week", row, h, cell),
    })),
  })) : [];
  const legend = d ? rampLabels(d.ramp) : [];
  return (
    <section className="card panel" aria-labelledby="hm-h" style={{ flex: "1 1 520px", gap: 10 }}>
      <h2 id="hm-h">Clients by day and hour (average, 4 weeks)</h2>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={200} />}
      {d && (
        <Heatmap rows={rows} axis={AXIS} hint="Hover a cell for the weekday, hour and average number of clients."
                 ariaLabel="Heatmap of the average number of clients by weekday and hour; hover a cell for the value" />
      )}
      {d && (
        <span className="ramp" aria-label={`Color scale, clients: ${legend.join(", ")}`}>
          {legend.map((t, i) => <span className="step" key={t}><span>{t}</span><i style={{ background: RAMP_COLORS[i] }} /></span>)}
          <span>clients · the AP's own scale, up to its busiest hour</span>
        </span>
      )}
    </section>
  );
}

function UtilCard({ bssid, channel, tz, now }) {
  const [k, u] = ep.apLoad(bssid, "util");
  const q = useQuery(k, u);
  const d = q.data;
  const labels = d ? labelsOf(d, now, tz) : [];
  const on = d ? collecting(d.buckets) : [];
  const summary = d ? utilSummary(d.avg, d.peak, labels) : null;
  return (
    <section className="card panel" aria-labelledby="util-h" style={{ flex: "1 1 360px", gap: 10 }}>
      <h2 id="util-h">Utilization of channel {channel ?? "–"} – last 7 days</h2>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={180} />}
      {d && !d.reports_util && (
        <p className="muted small" style={{ margin: 0 }}>This AP sends no BSS Load element: it reports no utilization.</p>
      )}
      {d && d.reports_util && (
        <UtilColumns avg={d.avg} peak={d.peak} on={on} labels={labels} xlabels={xLabels(d.buckets, d.period.bucket, tz)} />
      )}
      {d && d.reports_util && <span className="muted tiny">{summary ?? "No report in the period."}</span>}
    </section>
  );
}

function History({ d, tz }) {
  const [all, setAll] = useState(false);
  const rows = historyRows(d.config_history, d.first_capture, tz);
  const shown = all ? rows : rows.slice(0, HISTORY_SHOWN);
  const cpd = changesPerDayText(d.channel_changes_per_day);
  return (
    <section className="card panel" aria-labelledby="cfg-h" style={{ flex: "1 1 460px", gap: 10 }}>
      <div className="card-head">
        <h2 id="cfg-h">Configuration history</h2>
        {cpd && <span className="muted tiny">{cpd}</span>}
      </div>
      {rows.length === 0 && <p className="muted small" style={{ margin: 0 }}>No change recorded.</p>}
      <ol className="history">
        {shown.map((r, i) => (
          <li key={`${r.ts}-${i}`}>
            <span className="muted when">{r.when}</span>
            {r.first ? <span>First captured by {r.sensor}</span> : (
              <span>{r.parts.map((p, j) => (
                <span key={j}>{j > 0 ? "; " : ""}{p.what} <b>{p.from} → {p.to}</b></span>
              ))}</span>
            )}
          </li>
        ))}
      </ol>
      {(rows.length > HISTORY_SHOWN || d.config_history.total > d.config_history.rows.length) && (
        <div className="history-foot">
          {rows.length > HISTORY_SHOWN && (
            <button type="button" className="btn2" aria-expanded={all} onClick={() => setAll(!all)}>
              {all ? "Show fewer" : `Show all ${fmtInt(rows.length)}`}
            </button>
          )}
          {d.config_history.total > d.config_history.rows.length && (
            <span className="muted tiny">
              the newest {fmtInt(d.config_history.rows.length)} of {fmtInt(d.config_history.total)} changes
            </span>
          )}
        </div>
      )}
    </section>
  );
}

function Events({ events, bssid, tz }) {
  const [all, setAll] = useState(false);
  const shown = all ? events : events.slice(0, EVENTS_SHOWN);
  return (
    <section className="card panel" aria-labelledby="det-h" style={{ flex: "1 1 360px", gap: 10 }}>
      <h2 id="det-h">Events of this AP</h2>
      {events.length === 0 && <p className="muted small" style={{ margin: 0 }}>No event of this AP.</p>}
      <div className="ap-events">
        {shown.map((e) => {
          const b = eventBadges(e);
          return (
            <div className="ap-event" key={e.id}>
              <div className="badges">
                <span className={`b ${b.severity.cls}`}>{b.severity.text}</span>
                {b.status && <span className={`b ${b.status.cls}`}>{b.status.text}</span>}
                <span className="muted tiny">{fmtDateTime(e.ts, tz)} · {e.sensors.join(", ")}</span>
              </div>
              <span>{e.summary}</span>
              {e.status_note && <span className="muted tiny">Marked: {e.status_note}</span>}
            </div>
          );
        })}
      </div>
      <div className="history-foot">
        {events.length > EVENTS_SHOWN && (
          <button type="button" className="btn2" aria-expanded={all} onClick={() => setAll(!all)}>
            {all ? "Show fewer" : `Show all ${fmtInt(events.length)}`}
          </button>
        )}
        <Link to={`/events?q=${encodeURIComponent(bssid)}&status=all`} className="small">All events of this AP</Link>
      </div>
    </section>
  );
}

// the manual classification (PUT /aps/{bssid}/classification): a class, or
// back to the rules; the rule events are re-derived by the api
function Classification({ bssid, c, tz }) {
  const w = useWrite();
  const [state, setState] = useState({ busy: false, error: null });
  const put = async (value) => {
    setState({ busy: true, error: null });
    try {
      await w.send("PUT", `/api/v2/aps/${bssid}/classification`, { classification: value }, REFETCH_CLASS);
      setState({ busy: false, error: null });
    } catch (e) {
      setState({ busy: false, error: e.message });
    }
  };
  const locked = !w.unlocked || state.busy;
  return (
    <div role="group" aria-label="AP classification" className="fl">
      <span>Classification in this account</span>
      <div className="seg seg-lg" title={!w.unlocked ? LOCKED : undefined}>
        {CLASSES.map((k) => (
          <button key={k} type="button" aria-pressed={c.value === k} aria-disabled={locked ? "true" : undefined}
                  className={c.value === k && k === "suspicious" ? "sus" : undefined}
                  onClick={() => { if (!locked && !(c.by === "manual" && c.value === k)) put(k); }}>
            {classBadge(k).text}
          </button>
        ))}
      </div>
      <span className="muted tiny">
        {c.by === "manual" ? `set by hand ${fmtDateTime(c.at, tz)}` : "set by the rules"}
        {c.by === "manual" && w.unlocked && (
          <> · <button type="button" className="link-btn" disabled={state.busy} onClick={() => put(null)}>back to the rules</button></>
        )}
        {!w.unlocked && " · changing it needs the write token"}
      </span>
      {state.error && <span className="bad-text tiny" role="alert">{state.error}</span>}
    </div>
  );
}

function Note({ bssid, note, tz }) {
  const w = useWrite();
  const [text, setText] = useState(note.text ?? "");
  const [state, setState] = useState({ busy: false, ok: null, error: null });
  useEffect(() => { setText(note.text ?? ""); }, [note.text]);
  const changed = text.trim() !== (note.text ?? "");
  const save = async () => {
    setState({ busy: true, ok: null, error: null });
    try {
      await w.send("PUT", `/api/v2/aps/${bssid}/note`, { note: text }, ["ap|"]);
      setState({ busy: false, ok: "Saved.", error: null });
    } catch (e) {
      setState({ busy: false, ok: null, error: e.message });
    }
  };
  const why = !w.unlocked ? LOCKED : !changed ? "Nothing changed" : undefined;
  return (
    <section className="card panel" aria-labelledby="note-h" style={{ gap: 10 }}>
      <h2 id="note-h">Note</h2>
      <label htmlFor="ap-note" className="muted tiny">Internal note about this AP (visible in the account)</label>
      <textarea id="ap-note" rows={2} className="note" value={text} maxLength={2000} readOnly={!w.unlocked}
                title={!w.unlocked ? LOCKED : undefined} placeholder="No note yet" onChange={(e) => setText(e.target.value)} />
      <div className="note-foot">
        <button type="button" className="btn" disabled={Boolean(why) || state.busy} title={why} onClick={save}>
          {state.busy ? "Saving…" : "Save"}
        </button>
        <span className="muted tiny">
          {state.error ? <span className="bad-text">{state.error}</span> : state.ok
            ?? (note.at ? `last saved ${fmtDateTime(note.at, tz)}` : !w.unlocked ? "Saving requires the write token until login exists." : "")}
        </span>
      </div>
    </section>
  );
}
