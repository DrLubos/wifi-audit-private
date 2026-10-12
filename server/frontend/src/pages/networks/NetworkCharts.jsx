// "Networks over time" and "Network security over time" (Networks board): the
// two charts share the x axis, the y scale and the hovered bucket (the
// tooltip shows in the chart under the pointer); the security changes of the
// period below (the newest CHANGES_SHOWN, the rest behind "Show all"). One
// request (/api/v2/networks/timeseries) plus the changes from /api/v2/networks.

import { useState } from "react";
import { niceMax, UNIT, xLabels } from "../../charts/geometry.js";
import StackedColumns from "../../charts/StackedColumns.jsx";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { fmtInt } from "../../format.js";
import { collecting, labelsOf } from "../overview/period.js";
import { changeRows, changeSummary } from "./networks.js";

function coverageNotes(b) {
  const names = Object.keys(b.coverage);
  if (!names.some((n) => b.coverage[n] > 0)) return ["No sensor was collecting in this window"];
  return names.filter((n) => b.coverage[n] > 0 && b.coverage[n] < 0.995)
    .map((n) => `${n} collected ${Math.round(b.coverage[n] * 100)} % of this window`);
}

const CHANGES_SHOWN = 8;

export default function NetworkCharts({ ts, changes, tz, now }) {
  const [hov, setHov] = useState(null);           // {i, chart}
  const [allChanges, setAllChanges] = useState(false);
  if (ts.error) return <ErrorBox error={ts.error} />;
  if (!ts.data) return <div className="card panel"><Skeleton height={420} /></div>;
  const d = ts.data;
  const labels = labelsOf(d, now, tz);
  const on = collecting(d.buckets);
  const xl = xLabels(d.buckets, d.period.bucket, tz);
  const unit = UNIT[d.period.bucket];
  const c = d.categories;
  const sec = d.security;
  const totals = labels.map((_, i) => (c.configured[i] ?? 0) + (c.mobile[i] ?? 0) + (c.other[i] ?? 0));
  const top = niceMax(Math.max(0, ...totals));
  const last = labels.length - 1;
  const sub = on[last]
    ? `Networks (SSIDs) per ${unit} · latest window ${fmtInt(totals[last])}: ${fmtInt(c.configured[last])} configured, `
      + `${fmtInt(c.mobile[last])} mobile, ${fmtInt(c.other[last])} other`
    : `Networks (SSIDs) per ${unit} · latest window: no data`;
  const notes = d.buckets.map(coverageNotes);
  const nets = [
    { key: "configured", label: "configured", color: "var(--c1)", values: c.configured },
    { key: "mobile", label: "mobile", color: "var(--c2)", values: c.mobile },
    { key: "other", label: "other", color: "var(--c3)", values: c.other },
  ];
  const hasUnknown = sec.unknown.some((v) => v > 0);
  const levels = [
    { key: "open", label: "open", color: "var(--lvl-open)", values: sec.open },
    { key: "legacy", label: "legacy", color: "var(--lvl-legacy)", values: sec.legacy },
    { key: "psk", label: "WPA2-PSK without MFP", color: "var(--lvl-psk)", values: sec.psk_no_mfp },
    { key: "strong", label: "strong", color: "var(--lvl-strong)", values: sec.strong },
    ...(hasUnknown ? [{ key: "unknown", label: "unknown", color: "var(--lvl-unknown)", values: sec.unknown }] : []),
  ];
  const shared = (chart) => ({
    active: hov?.i ?? null,
    onActive: (i) => setHov(i == null ? null : { i, chart }),
    tip: hov?.chart === chart,
    top, labels, on, xlabels: xl, notes, height: 180, totalLabel: "networks in total",
  });
  const rows = changeRows(changes ?? [], tz);
  return (
    <>
      <section className="card panel" aria-labelledby="time-h">
        <div className="card-head">
          <div className="card-title">
            <h2 id="time-h">Networks over time</h2>
            <span className="muted small">{sub}</span>
          </div>
          <div className="lg">
            <span><span className="sw" style={{ background: "var(--c1)" }} />configured (managed + friendly)</span>
            <span><span className="sw" style={{ background: "var(--c2)" }} />mobile (hotspots)</span>
            <span><span className="sw" style={{ background: "var(--c3)" }} />other, not configured</span>
          </div>
        </div>
        <StackedColumns series={nets} {...shared("net")}
                        ariaLabel="Stacked columns of networks per period window; hover or focus a column for exact values"
                        columnAria={(i) => (on[i]
                          ? `${labels[i]}: ${fmtInt(c.configured[i])} configured, ${fmtInt(c.mobile[i])} mobile, ${fmtInt(c.other[i])} other networks`
                          : `${labels[i]}: no sensor collecting`)} />
      </section>
      <section className="card panel" aria-labelledby="secu-h">
        <div className="card-head">
          <div className="card-title">
            <h2 id="secu-h">Network security over time</h2>
            <span className="muted small">
              Networks (SSIDs) by the weakest security they offer · fixed networks rarely change, most of the
              variation comes from hotspots
            </span>
          </div>
          <div className="lg">
            <span><span className="sw" style={{ background: "var(--lvl-open)" }} />open</span>
            <span><span className="sw" style={{ background: "var(--lvl-legacy)" }} />legacy (WEP, WPA1/TKIP)</span>
            <span><span className="sw" style={{ background: "var(--lvl-psk)" }} />WPA2-PSK without MFP</span>
            <span><span className="sw" style={{ background: "var(--lvl-strong)" }} />strong (MFP or Enterprise)</span>
            {hasUnknown && <span><span className="sw" style={{ background: "var(--lvl-unknown)" }} />unknown</span>}
          </div>
        </div>
        <StackedColumns series={levels} {...shared("sec")}
                        ariaLabel="Stacked columns of networks by security level; hover or focus a column to see every level, including the thin ones"
                        columnAria={(i) => (on[i]
                          ? `${labels[i]}: ${fmtInt(sec.open[i])} open, ${fmtInt(sec.legacy[i])} legacy, `
                            + `${fmtInt(sec.psk_no_mfp[i])} WPA2-PSK without MFP, ${fmtInt(sec.strong[i])} strong`
                            + (hasUnknown ? `, ${fmtInt(sec.unknown[i])} unknown` : "")
                          : `${labels[i]}: no sensor collecting`)} />
        <div className="changes">
          <span style={{ fontWeight: 500 }}>
            Security changes in the period <span className="muted" style={{ fontWeight: 400 }}>· {changeSummary(changes ?? [])}</span>
          </span>
          {(allChanges ? rows : rows.slice(0, CHANGES_SHOWN)).map((r, i) => (
            <div className="row" key={i}>
              <span className={`b ${r.kind.cls}`}>{r.kind.label}</span>
              <span>{r.when} · <b style={{ fontWeight: 500 }}>{r.network}</b> · <span className="mono">{r.bssid}</span> · {r.what}</span>
            </div>
          ))}
          {rows.length > CHANGES_SHOWN && (
            <div>
              <button type="button" className="btn2" aria-expanded={allChanges} onClick={() => setAllChanges(!allChanges)}>
                {allChanges ? "Show fewer" : `Show all ${fmtInt(rows.length)} changes`}
              </button>
            </div>
          )}
        </div>
      </section>
    </>
  );
}
