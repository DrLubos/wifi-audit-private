// Access points (/aps, boards/Aps.dc.html): the account's APs seen by the
// selected sensors in the period - search, grouping, classification chips
// with counts, band / Open / New only / Hide hotspots, the table (status,
// SSID, BSSID, vendor, classification, security, channel, clients,
// utilization, seen by sensors with their RSSI, last seen) and offset pages.
// Filters sensor + range; page-local in the URL: q, class, band, open, new,
// hotspots (shown), group, page.

import { Fragment, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useFilters } from "../../hooks/useFilters.js";
import { useNow } from "../../hooks/useNow.js";
import { fmtInt } from "../../format.js";
import { siblingText } from "../networks/networks.js";
import {
  apName, channelText, chips, classBadge, CLASSES, groupLabel, lastSeenText, pagerText, securityCell, seenByChips,
  statusBadge, utilText, vendorText,
} from "./aps.js";
import "./aps.css";

const PAGE_SIZE = 50;

export default function ApList() {
  const { sensor, range } = useFilters();
  const account = useAccount();
  const now = useNow(30_000);
  const [params, setParams] = useSearchParams();
  const f = {
    q: params.get("q") ?? "",
    cls: CLASSES.includes(params.get("class")) ? params.get("class") : null,
    band: ["2.4", "5"].includes(params.get("band")) ? params.get("band") : null,
    open: params.get("open") === "1",
    new: params.get("new") === "1",
    hotspots: params.get("hotspots") === "show",
    group: ["ssid", "sensor"].includes(params.get("group")) ? params.get("group") : "none",
    page: Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1),
  };
  const set = (changes, keepPage = false) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    for (const [k, v] of Object.entries(changes)) {
      if (v == null || v === "" || v === false) next.delete(k); else next.set(k, String(v));
    }
    if (!keepPage) next.delete("page");
    return next;
  }, { replace: true });

  // the search box: typed locally, into the URL 300 ms after the last key
  const [text, setText] = useState(f.q);
  useEffect(() => { setText(f.q); }, [f.q]);
  useEffect(() => {
    if (text === f.q) return undefined;
    const t = setTimeout(() => set({ q: text.trim() || null }), 300);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps

  const [key, url] = ep.aps(sensor, range, f, PAGE_SIZE);
  const q = useQuery(key, url);
  const d = q.data;
  const tz = account.tz;
  const ch = chips(d?.counts);
  const toggleOn = { band24: f.band === "2.4", band5: f.band === "5", open: f.open, new: f.new, hotspots: !f.hotspots };
  const toggle = (k) => {
    if (k === "band24") set({ band: f.band === "2.4" ? null : "2.4" });
    else if (k === "band5") set({ band: f.band === "5" ? null : "5" });
    else if (k === "hotspots") set({ hotspots: f.hotspots ? null : "show" });
    else set({ [k]: f[k] ? null : "1" });
  };
  const pages = d ? Math.max(1, Math.ceil(d.total / d.page_size)) : 1;
  const sizes = Object.fromEntries((d?.groups ?? []).map((g) => [g.key, g.n]));
  let lastGroup;
  return (
    <main className="content v2" style={{ gap: 18 }}>
      <div className="page-head">
        <h1>Access points</h1>
        <span className="muted">
          {d ? `${fmtInt(d.header.aps)} APs in the account · ${fmtInt(d.header.active)} active now · `
            + `${fmtInt(d.header.new_24h)} new in the last 24 h` : "…"}
        </span>
      </div>

      <section className="card filters-card" aria-label="Filters">
        <div className="filter-row">
          <label className="fl" style={{ flex: "1 1 280px" }}>Search SSID, BSSID or vendor
            <input type="search" className="in" placeholder="e.g. IK-WIFI, EC:58:EA, Ruckus" value={text}
                   onChange={(e) => setText(e.target.value)} />
          </label>
          <div role="group" aria-label="Group by" className="fl">
            <span>Group by</span>
            <div className="seg seg-lg">
              {[["none", "No grouping"], ["ssid", "Network (SSID)"], ["sensor", "Sensor"]].map(([k, label]) => (
                <button key={k} type="button" aria-pressed={f.group === k}
                        onClick={() => set({ group: k === "none" ? null : k })}>{label}</button>
              ))}
            </div>
          </div>
        </div>
        <div className="chip-row">
          <span className="muted tiny" style={{ marginRight: 4 }}>Classification</span>
          {ch.classes.map((c) => (
            <button key={c.key ?? "all"} type="button" className="chip" aria-pressed={f.cls === c.key}
                    onClick={() => set({ class: c.key })}>{c.label}</button>
          ))}
          <span className="chip-sep" aria-hidden="true" />
          {ch.toggles.map((t) => (
            <button key={t.key} type="button" className="chip" aria-pressed={toggleOn[t.key]}
                    onClick={() => toggle(t.key)}>{t.label}</button>
          ))}
        </div>
      </section>

      {q.error && <ErrorBox error={q.error} />}
      <section className="card table-card" aria-label="Access point list" style={{ padding: "4px 0 0" }}>
        {!d && !q.error && <div style={{ padding: 18 }}><Skeleton height={320} /></div>}
        {d && (
          <div className={q.isPrevious ? "scroll-x stale" : "scroll-x"}>
            <table className="t nw ap-table">
              <thead>
                <tr><th>Status</th><th>SSID</th><th>BSSID</th><th>Vendor</th><th>Classification</th><th>Security</th>
                  <th>Channel</th><th>Clients</th><th>Utilization</th><th className="wrap">Seen by sensors (RSSI)</th><th className="wrap">Last seen</th></tr>
              </thead>
              <tbody>
                {d.items.length === 0 && (
                  <tr><td colSpan={11} className="muted" style={{ whiteSpace: "normal" }}>No access point matches the filters.</td></tr>
                )}
                {d.items.map((a) => {
                  const name = apName(a);
                  const sec = securityCell(a);
                  const st = statusBadge(a.active);
                  const cl = classBadge(a.classification);
                  const head = d.group !== "none" && a.group !== lastGroup;
                  lastGroup = a.group;
                  return (
                    <Fragment key={a.bssid}>
                      {head && (
                        <tr className="group-row"><td colSpan={11}>{groupLabel(d.group, a.group, sizes[a.group] ?? 0)}</td></tr>
                      )}
                      <tr className={a.classification === "suspicious" ? "suspicious" : undefined}>
                        <td><span className={`b ${st.cls}`} title="seen within 5 min of its sensor's latest capture">{st.text}</span></td>
                        <td>
                          <Link to={`/aps/${a.bssid}`} className={name.hidden ? "ap-name hidden" : "ap-name"}>{name.text}</Link>
                          {a.sibling && <span className="sib muted tiny">{siblingText(a.sibling)}</span>}
                        </td>
                        <td className="mono">{a.bssid}</td>
                        <td><span className="vendor" title={a.manuf ?? undefined}>{vendorText(a)}</span></td>
                        <td><span className={`b ${cl.cls}`} title={a.classified_by === "manual" ? "set by hand" : "set by the rules"}>{cl.text}</span></td>
                        <td>
                          <span className={`b ${sec.cls}`}>{sec.text}</span>
                          {sec.note && <span className="sec-note muted tiny" title={sec.title}>{sec.note}</span>}
                        </td>
                        <td>{channelText(a)}</td>
                        <td>{a.clients == null ? "–" : fmtInt(a.clients)}</td>
                        <td>{utilText(a.util)}</td>
                        <td>{seenByChips(a.seen_by).map((s) => <span className="sen" key={s.name}>{s.text}</span>)}</td>
                        <td>{lastSeenText(a.last_seen, now, tz)}</td>
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {d && (
          <div className="pager muted">
            <span>{pagerText(d.page, d.page_size, d.total, d.hidden_hotspots, d.items.length)}</span>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" className="btn2" disabled={f.page <= 1}
                      onClick={() => set({ page: f.page - 1 > 1 ? f.page - 1 : null }, true)}>← Previous</button>
              <button type="button" className="btn2" disabled={f.page >= pages}
                      onClick={() => set({ page: f.page + 1 }, true)}>Next →</button>
            </div>
          </div>
        )}
      </section>

      <span className="muted small">
        <b className="em">Seen by sensors</b>: every sensor that hears the AP, with its median RSSI in the period; the
        first one is the strongest, so the AP is closest to it.
      </span>
    </main>
  );
}
