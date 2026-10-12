// Events (/events, boards/Events.dc.html): the account's events newest first
// - detector results, Kismet alert episodes, security changes, sensor
// outages - with status chips (counts), type chips, "Managed networks only",
// BSSID / SSID search, keyset pages (← Newer / Older →), and a row that
// expands into "Why it fired" and "Who saw it". Confirm / False positive and
// "Add to allowed BSSIDs" stay visible but locked until the write token
// (phase 5). Filter: sensor; page-local in the URL: status, type, managed, q,
// the cursor (before / after + p, the page number), id (the open event - the
// links of the Overview and Networks; shown on its own card when it is not
// on the page).

import { Fragment, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ATTENTION_BG } from "../../app/status.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useFilters } from "../../hooks/useFilters.js";
import { fmtDateTime } from "../../format.js";
import {
  actionsFor, apCell, headText, KINDS, pagerText, severityBadge, statusBadge, statusChips, STATUSES, timeText,
  timeTitle, typeText, whoRows, whyRows,
} from "./events.js";
import "./events.css";

const LIMIT = 50;
const LOCKED = "Write actions need the write token (dashboard phase 5)";

const csv = (v, allowed) => (v ?? "").split(",").filter((x) => allowed.includes(x));

export default function Events() {
  const { sensor } = useFilters();
  const account = useAccount();
  const tz = account.tz;
  const [params, setParams] = useSearchParams();
  const f = {
    status: csv(params.get("status"), STATUSES),
    type: csv(params.get("type"), KINDS.map(([k]) => k)),
    managed: params.get("managed") === "1",
    q: params.get("q") ?? "",
    before: params.get("before"),
    after: params.get("after"),
    page: Math.max(1, Number.parseInt(params.get("p") ?? "1", 10) || 1),
    id: Number.parseInt(params.get("id") ?? "", 10) || null,
  };
  // a filter change starts at the newest page again; the open event stays
  const set = (changes, keepCursor = false) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    for (const [k, v] of Object.entries(changes)) {
      if (v == null || v === "" || v === false || (Array.isArray(v) && !v.length)) next.delete(k);
      else next.set(k, Array.isArray(v) ? v.join(",") : String(v));
    }
    if (!keepCursor) ["before", "after", "p"].forEach((k) => next.delete(k));
    return next;
  }, { replace: true });
  const toggleIn = (list, k) => (list.includes(k) ? list.filter((x) => x !== k) : [...list, k]);

  const [text, setText] = useState(f.q);
  useEffect(() => { setText(f.q); }, [f.q]);
  useEffect(() => {
    if (text === f.q) return undefined;
    const t = setTimeout(() => set({ q: text.trim() || null }), 300);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps

  const [key, url] = ep.events(sensor, f, LIMIT);
  const q = useQuery(key, url);
  const d = q.data;
  const filtered = d && f.status.length ? f.status.reduce((a, s) => a + (d.counts[s] ?? 0), 0) : d?.counts.total;
  const onPage = d?.items.some((e) => e.id === f.id);
  return (
    <main className="content v2" style={{ gap: 18 }}>
      <div className="events-head">
        <h1>Events</h1>
        <span className="muted">{d ? headText(d.counts) : "…"}</span>
      </div>

      <section className="card events-filters" aria-label="Filters">
        <span className="muted tiny">Status</span>
        {statusChips(d?.counts).map((c) => (
          <button key={c.key} type="button" className="chip" aria-pressed={f.status.includes(c.key)}
                  onClick={() => set({ status: toggleIn(f.status, c.key) })}>{c.label}</button>
        ))}
        <span className="chip-sep" aria-hidden="true" />
        <span className="muted tiny">Type</span>
        {KINDS.map(([k, label]) => (
          <button key={k} type="button" className="chip" aria-pressed={f.type.includes(k)}
                  onClick={() => set({ type: toggleIn(f.type, k) })}>{label}</button>
        ))}
        <span className="chip-sep" aria-hidden="true" />
        <button type="button" className="chip" aria-pressed={f.managed}
                onClick={() => set({ managed: f.managed ? null : "1" })}>Managed networks only</button>
        <label className="muted tiny events-search">BSSID or SSID
          <input type="search" className="in" placeholder="e.g. EC:58:EA" value={text}
                 onChange={(e) => setText(e.target.value)} />
        </label>
      </section>

      {f.id && d && !onPage && <LinkedEvent id={f.id} tz={tz} onClose={() => set({ id: null }, true)} />}

      {q.error && <ErrorBox error={q.error} />}
      <section className="card table-card" aria-label="Event list" style={{ padding: "4px 0 0" }}>
        {!d && !q.error && <div style={{ padding: 18 }}><Skeleton height={320} /></div>}
        {d && (
          <div className={q.isPrevious ? "scroll-x stale" : "scroll-x"}>
            <table className="t events-table">
              <thead>
                <tr><th>Time</th><th>Severity</th><th>Type</th><th>Event</th><th>AP / network</th><th>BSSID</th>
                  <th>Sensors</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {d.items.length === 0 && (
                  <tr><td colSpan={9} className="muted">No event matches the filters.</td></tr>
                )}
                {d.items.map((e) => {
                  const open = e.id === f.id;
                  const sev = severityBadge(e.severity);
                  const st = statusBadge(e.status);
                  const ap = apCell(e);
                  const bg = open ? { background: ATTENTION_BG[e.severity] } : undefined;
                  return (
                    <Fragment key={e.id}>
                      <tr style={bg}>
                        <td className="nowrap" title={timeTitle(e, tz)}>{timeText(e, tz)}</td>
                        <td><span className={`b ${sev.cls}`}>{sev.text}</span></td>
                        <td className="type">{typeText(e)}</td>
                        <td className="ev">
                          <button type="button" className="row-toggle ev-toggle" aria-expanded={open}
                                  onClick={() => set({ id: open ? null : e.id }, true)}>
                            <span aria-hidden="true">{open ? "▾ " : "▸ "}</span>{e.summary}
                          </button>
                        </td>
                        <td className={ap.hidden ? "net hidden-net" : "net"}>{ap.text}</td>
                        <td className="mono nowrap">{e.ap?.bssid ?? "–"}</td>
                        <td>{e.sensors.join(", ")}</td>
                        <td><span className={`b ${st.cls}`} title={e.status_at ? `set ${fmtDateTime(e.status_at, tz)}` : undefined}>{st.text}</span></td>
                        <td className="nowrap">
                          {actionsFor(e).map((a) => (
                            <button key={a} type="button" className="btn2 act" disabled title={LOCKED}>{a}</button>
                          ))}
                        </td>
                      </tr>
                      {open && (
                        <tr style={bg}>
                          <td colSpan={9} className="ev-detail-cell"><EventDetail id={e.id} tz={tz} /></td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {d && (
          <div className="pager muted">
            <span>{pagerText(f.page, LIMIT, d.items.length, d.counts, filtered)}</span>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" className="btn2" disabled={!d.newer || f.page <= 1}
                      onClick={() => (f.page - 1 <= 1 ? set({}) : set({ after: d.newer, before: null, p: f.page - 1 }, true))}>
                ← Newer
              </button>
              <button type="button" className="btn2" disabled={!d.older}
                      onClick={() => set({ before: d.older, after: null, p: f.page + 1 }, true)}>Older →</button>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}

// the event linked to (?id=) when it is not on the page shown
function LinkedEvent({ id, tz, onClose }) {
  const [k, u] = ep.event(id);
  const q = useQuery(k, u);
  const e = q.data;
  return (
    <section className="card linked-event" aria-label="Linked event"
             style={e ? { background: ATTENTION_BG[e.severity] } : undefined}>
      <div className="linked-head">
        {e ? (
          <span>
            <span className={`b ${severityBadge(e.severity).cls}`}>{severityBadge(e.severity).text}</span>{" "}
            <b className="em">{e.summary}</b>
            <span className="muted"> · {typeText(e)} · {fmtDateTime(e.ts, tz)}</span>
          </span>
        ) : <span className="muted">Event #{id}</span>}
        <button type="button" className="btn2" onClick={onClose}>Close</button>
      </div>
      {q.error && <ErrorBox error={/\b404\b/.test(q.error) ? `There is no event #${id}.` : q.error} />}
      {!e && !q.error && <Skeleton height={120} />}
      {e && <DetailBody e={e} tz={tz} />}
    </section>
  );
}

function EventDetail({ id, tz }) {
  const [k, u] = ep.event(id);
  const q = useQuery(k, u);
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return <Skeleton height={120} />;
  return <DetailBody e={q.data} tz={tz} />;
}

function DetailBody({ e, tz }) {
  const why = whyRows(e, tz);
  const who = whoRows(e, tz);
  return (
    <div className="ev-detail">
      <div className="ev-col">
        <span className="ev-label">Why it fired</span>
        <dl className="kv">
          {why.map((r) => (
            <Fragment key={r.dt}><dt>{r.dt}</dt><dd className={r.mono ? "mono" : undefined}>{r.dd}</dd></Fragment>
          ))}
        </dl>
        {e.status_note && <span className="muted tiny">Marked: {e.status_note}</span>}
      </div>
      {e.kind !== "sensor" && (
        <div className="ev-col">
          <span className="ev-label">Who saw it</span>
          {who.length ? (
            <dl className="kv">
              {who.map((r) => <Fragment key={r.dt}><dt>{r.dt}</dt><dd>{r.dd}</dd></Fragment>)}
            </dl>
          ) : <span className="muted small">No sensor has this AP (the transmitter is not an AP of the account).</span>}
          <div className="ev-actions">
            {e.ap && <Link to={`/aps/${e.ap.bssid}`} className="btn">AP details</Link>}
            {e.type === "unknown_bssid_managed_ssid" && (
              <button type="button" className="btn2" disabled title={LOCKED}>Add to allowed BSSIDs</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
