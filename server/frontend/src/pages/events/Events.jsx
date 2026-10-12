// Events (/events, boards/Events.dc.html): the account's events newest first
// - detector results, Kismet alert episodes, security changes, sensor
// outages - with status chips (counts; Open by default, the work queue),
// type chips, "Medium and above", "Managed networks only", BSSID / SSID
// search, keyset pages (← Newer / Older →), and a row that expands into "Why
// it fired" and "Who saw it". Repeated low events of one AP, type and status
// on one local day are one row with a count that expands into its events
// (display only, decided 2026-10-12). Confirm / False positive (on a group:
// for all of it) and "Add to allowed BSSIDs" stay visible but locked until the
// write token (phase 5). Filter: sensor; page-local in the URL: status (none
// = Open, "all"), type, severity, managed, q, the cursor (before / after + p,
// the page number), id (the open event - the links of the Overview and
// Networks; shown on its own card when it is not on the page).

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
  actionsFor, apCell, groupActions, groupText, headText, KINDS, pagerText, parseStatus, rowOf, selected,
  SEVERITY_MIN, severityBadge, statusBadge, statusChips, statusParam, timeText, timeTitle, typeText, whoRows, whyRows,
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
    status: parseStatus(params.get("status")),
    type: csv(params.get("type"), KINDS.map(([k]) => k)),
    severity: params.get("severity") === SEVERITY_MIN ? SEVERITY_MIN : null,
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

  // the groups opened by hand (by their leading event); the group of the open event is open too
  const [opened, setOpened] = useState(() => new Set());

  const [key, url] = ep.events(sensor, f, LIMIT);
  const q = useQuery(key, url);
  const d = q.data;
  const holder = d ? rowOf(d.items, f.id) : null;
  const toggleEvent = (id) => set({ id: id === f.id ? null : id }, true);
  const toggleGroup = (e, expanded) => {
    const next = new Set(opened);
    if (expanded) {
      next.delete(e.id);
      if (holder === e) set({ id: null }, true);
    } else next.add(e.id);
    setOpened(next);
  };
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
                  onClick={() => set({ status: statusParam(toggleIn(f.status, c.key)) })}>{c.label}</button>
        ))}
        <span className="chip-sep" aria-hidden="true" />
        <span className="muted tiny">Type</span>
        {KINDS.map(([k, label]) => (
          <button key={k} type="button" className="chip" aria-pressed={f.type.includes(k)}
                  onClick={() => set({ type: toggleIn(f.type, k) })}>{label}</button>
        ))}
        <span className="chip-sep" aria-hidden="true" />
        <button type="button" className="chip" aria-pressed={!!f.severity}
                onClick={() => set({ severity: f.severity ? null : SEVERITY_MIN })}>Medium and above</button>
        <button type="button" className="chip" aria-pressed={f.managed}
                onClick={() => set({ managed: f.managed ? null : "1" })}>Managed networks only</button>
        <label className="muted tiny events-search">BSSID or SSID
          <input type="search" className="in" placeholder="e.g. EC:58:EA" value={text}
                 onChange={(e) => setText(e.target.value)} />
        </label>
      </section>

      {f.id && d && !holder && <LinkedEvent id={f.id} tz={tz} onClose={() => set({ id: null }, true)} />}

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
                  if (!e.group) {
                    return <EventRow key={e.id} e={e} tz={tz} open={e.id === f.id} onToggle={toggleEvent} />;
                  }
                  const expanded = opened.has(e.id) || holder === e;
                  return (
                    <Fragment key={e.id}>
                      <EventRow e={e} tz={tz} group expanded={expanded} onToggle={() => toggleGroup(e, expanded)} />
                      {expanded && e.group.members.map((m) => (
                        <EventRow key={m.id} e={{ ...e, ...m, group: null }} tz={tz} member open={m.id === f.id}
                                  onToggle={toggleEvent} />
                      ))}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {d && (
          <div className="pager muted">
            <span>{pagerText(f.page, LIMIT, d.items.length, selected(d.rows ?? d.counts, f.status),
                             selected(d.counts, f.status))}</span>
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

// One row of the feed: an event (its toggle opens the detail below it), a
// group (its toggle opens its events, the cells it shares are on it) or an
// event of an open group (member: only its time, summary and actions).
function EventRow({ e, tz, open = false, group = false, expanded = false, member = false, onToggle }) {
  const sev = severityBadge(e.severity);
  const st = statusBadge(e.status);
  const ap = apCell(e);
  const bg = open ? { background: ATTENTION_BG[e.severity] } : undefined;
  const actions = group ? groupActions(e) : actionsFor(e);
  const lock = group ? `${LOCKED}; it will label all ${e.group.n} events of the row` : LOCKED;
  const pressed = group ? expanded : open;
  return (
    <>
      <tr className={group ? "ev-group" : member ? "ev-member" : undefined} style={bg}>
        <td className="nowrap" title={group ? groupText(e, tz) : timeTitle(e, tz)}>{timeText(e, tz)}</td>
        <td>{!member && <span className={`b ${sev.cls}`}>{sev.text}</span>}</td>
        <td className="type">{!member && typeText(e)}</td>
        <td className="ev">
          <button type="button" className="row-toggle ev-toggle" aria-expanded={pressed}
                  onClick={() => onToggle(e.id)}>
            <span aria-hidden="true">{pressed ? "▾ " : "▸ "}</span>{e.summary}
            {group && <>{" "}<span className="b neu ev-count">{e.group.n}×</span></>}
          </button>
          {group && <span className="muted tiny ev-span">{groupText(e, tz)}</span>}
        </td>
        <td className={ap.hidden ? "net hidden-net" : "net"}>{!member && ap.text}</td>
        <td className="mono nowrap">{!member && (e.ap?.bssid ?? "–")}</td>
        <td>{!member && e.sensors.join(", ")}</td>
        <td>
          {!member && (
            <span className={`b ${st.cls}`} title={!group && e.status_at ? `set ${fmtDateTime(e.status_at, tz)}` : undefined}>
              {st.text}
            </span>
          )}
        </td>
        <td className="nowrap">
          {actions.map((a) => <button key={a} type="button" className="btn2 act" disabled title={lock}>{a}</button>)}
        </td>
      </tr>
      {open && (
        <tr style={bg}>
          <td colSpan={9} className="ev-detail-cell"><EventDetail id={e.id} tz={tz} /></td>
        </tr>
      )}
    </>
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
