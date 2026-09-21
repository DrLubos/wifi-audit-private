import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useApi } from "../api";
import DataTable from "../components/DataTable";
import { fmtDateTime, fmtDateTimeSec, fmtDuration, fmtInt, fmtNum } from "../format";

// Server-side detections (server/detection/), read-only: list, filter, expand
// a row for its evidence. Rows are written by the batch detectors on demand.

const SEVERITIES = ["info", "low", "medium", "high", "critical"];
const STATES = [
  { id: "all", label: "All", query: null },
  { id: "open", label: "Open", query: "false" },
  { id: "acked", label: "Acknowledged", query: "true" },
];

function Sev({ level }) {
  return <span className={`sev sev-${level}`}>{level}</span>;
}

function apLabel(d) {
  if (d.ap_device_key) {
    const name = d.ap_hidden ? <span className="muted">&lt;hidden&gt;</span> : d.ap_ssid;
    return <Link to={`/aps/${d.ap_device_key}`}>{name}</Link>;
  }
  if (d.ssid) return d.ssid;
  return <span className="muted">&lt;unknown AP&gt;</span>;
}

const COLUMNS = [
  { key: "ts", label: "Time", render: (d) => fmtDateTimeSec(d.ts),
    sortValue: (d) => new Date(d.ts).getTime() },
  { key: "severity", label: "Severity", render: (d) => <Sev level={d.severity} />,
    sortValue: (d) => SEVERITIES.indexOf(d.severity) },
  { key: "type", label: "Type", className: "mono" },
  { key: "ap", label: "AP", sortValue: (d) => d.ap_ssid ?? d.ssid ?? d.mac,
    render: (d) => <>{apLabel(d)}<br /><span className="muted mono">{d.ap_bssid ?? d.mac ?? "-"}</span></> },
  { key: "summary", label: "Summary", className: "wrap" },
  { key: "acked", label: "Ack", render: (d) => (d.acked ? "yes" : "-") },
];

// --- evidence -----------------------------------------------------------------

function Rows({ rows }) {
  return (
    <div className="table-wrap">
      <table className="small-table">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}><th>{label}</th><td>{value ?? "-"}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const list = (xs) => (xs && xs.length ? xs.join(", ") : "-");
const num = (v, digits = 2) => (v == null ? "-" : fmtNum(v, digits));

function DeauthFloodEvidence({ ev }) {
  const a = ev.alerts ?? {};
  const b = ev.bursts ?? {};
  const base = ev.baseline ?? {};
  const obs = ev.observed ?? {};
  const corroborating = Object.entries(a.corroborating ?? {}).filter(([, ids]) => ids?.length);
  return (
    <>
      <div className="inline-tables">
        <Rows rows={[
          ["Flood alerts", fmtInt(a.n)],
          ["Span", a.span_s == null ? "-" : `${fmtNum(a.span_s, a.span_s < 10 ? 2 : 0)} s`],
          ["By header", Object.entries(a.by_header ?? {}).map(([h, n]) => `${h} ${n}`).join(", ") || "-"],
          ["Sources", <span className="mono">{list(a.sources)}</span>],
          ["Direction", list(a.directions?.map((d) => d.replace("_", " ")))],
          ["Broadcast", a.broadcast ? "yes" : "no"],
          ["Alert ids", list(a.ids)],
          ["Corroborating", corroborating.length
            ? corroborating.map(([h, ids]) => `${h} #${ids.join(", #")}`).join("; ") : "-"],
        ]} />
        <Rows rows={[
          ["Burst polls", fmtInt(b.n)],
          ["Max in window / threshold", b.max_in_window == null ? "-" : `${b.max_in_window} / ${b.threshold ?? "-"}`],
          ["Polls", b.polls?.length
            ? <span className="mono">{b.polls.map((t, i) => (
                <span key={t}>{fmtDateTimeSec(t)} {b.values?.[i] ? `${b.values[i][0]} → ${b.values[i][1]}` : ""}<br /></span>))}</span>
            : "-"],
        ]} />
        <Rows rows={[
          ["Baseline range", base.from ? <>{fmtDateTime(base.from)} → {fmtDateTime(base.to)}{base.fallback ? <span className="muted"> (scan range, no lookback data)</span> : null}</> : "-"],
          ["Active hours", num(base.active_hours, 1)],
          ["Bursts (rate)", base.bursts == null ? "-" : `${base.bursts} (${num(base.bursts_per_hour, 4)}/h)`],
          ["Flood alerts before", base.flood_alerts ?? "-"],
          ["Non-data frames / poll", base.mgmt_per_poll_median == null ? "-"
            : `median ${num(base.mgmt_per_poll_median, 1)}, robust sd ${num(base.mgmt_per_poll_robust_sd, 2)}`],
          ["Observed bursts/h", obs.bursts_per_hour == null ? "-"
            : `${num(obs.bursts_per_hour, 2)}${obs.ratio_to_baseline != null ? ` (${num(obs.ratio_to_baseline, 1)}× baseline)` : ""}`],
          ["Observed non-data / poll", obs.mgmt_per_poll_max == null ? "-"
            : `max ${num(obs.mgmt_per_poll_max, 0)}, mean ${num(obs.mgmt_per_poll_mean, 1)}${obs.mgmt_ratio_to_median != null ? ` (${num(obs.mgmt_ratio_to_median, 1)}× median)` : ""}`],
        ]} />
      </div>
      <p className="note">
        Window {fmtDateTimeSec(ev.window_start)} → {fmtDateTimeSec(ev.window_end)}
        {ev.duration_s != null ? ` (${ev.duration_s < 60 ? `${fmtNum(ev.duration_s, 1)} s` : fmtDuration(ev.duration_s)})` : ""}
        {" · rules: "}{ev.rules?.alerts ? "A (alerts)" : ""}{ev.rules?.alerts && ev.rules?.bursts ? " + " : ""}{ev.rules?.bursts ? "B (bursts)" : ""}
        {ev.ap?.trusted ? " · trusted AP" : ""}
        {ev.runs != null ? ` · ${ev.runs} run${ev.runs === 1 ? "" : "s"}, last ${fmtDateTime(ev.last_run_at)}` : ""}
      </p>
    </>
  );
}

function Evidence({ detection }) {
  const ev = detection.evidence ?? {};
  return (
    <div className="evidence">
      {detection.type === "deauth_flood" ? <DeauthFloodEvidence ev={ev} /> : null}
      <details>
        <summary className="muted">Raw evidence</summary>
        <pre>{JSON.stringify(ev, null, 2)}</pre>
      </details>
    </div>
  );
}

// --- page ---------------------------------------------------------------------

export default function Detections() {
  const [severity, setSeverity] = useState("all");
  const [state, setState] = useState("all");
  const [type, setType] = useState("all");

  const path = useMemo(() => {
    const q = new URLSearchParams();
    if (severity !== "all") q.set("severity", severity);
    const st = STATES.find((s) => s.id === state)?.query;
    if (st) q.set("acked", st);
    if (type !== "all") q.set("type", type);
    const qs = q.toString();
    return `/api/detections${qs ? `?${qs}` : ""}`;
  }, [severity, state, type]);
  const { data, error, loading } = useApi(path);

  const types = Object.keys(data?.by_type ?? {}).sort();

  return (
    <>
      <h1>Detections{data ? <small>{fmtInt(data.total)} total · {fmtInt(data.open)} open</small> : null}</h1>
      <p className="sub">
        Findings of the server-side batch detectors, run on demand over a time window
        (<code>docker compose run --rm detect …</code>). Click a row for the evidence.
        Severity: <b>low</b> = a brief burst Kismet called a flood, <b>medium</b> = several seconds
        or sustained burst activity, <b>high</b> = a sustained flood, <b>critical</b> = a sustained
        flood from the AP side on a trusted AP.
      </p>
      <div className="filters">
        <div className="chips" role="group" aria-label="Severity">
          <button type="button" className="chip" aria-pressed={severity === "all"}
                  onClick={() => setSeverity("all")}>All</button>
          {SEVERITIES.map((s) => (
            <button key={s} type="button" className="chip" aria-pressed={severity === s}
                    onClick={() => setSeverity(s)}>
              {s}{data ? <span className="muted"> {data.by_severity?.[s] ?? 0}</span> : null}
            </button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="State">
          {STATES.map((s) => (
            <button key={s.id} type="button" className="chip" aria-pressed={state === s.id}
                    onClick={() => setState(s.id)}>{s.label}</button>
          ))}
        </div>
        {types.length > 1 ? (
          <div className="chips" role="group" aria-label="Type">
            <button type="button" className="chip" aria-pressed={type === "all"}
                    onClick={() => setType("all")}>All types</button>
            {types.map((t) => (
              <button key={t} type="button" className="chip mono" aria-pressed={type === t}
                      onClick={() => setType(t)}>{t}</button>
            ))}
          </div>
        ) : null}
        {data ? <span className="count">{fmtInt(data.count)} shown{loading ? "…" : ""}</span> : null}
      </div>
      {error ? <p className="error">Failed to load: {error}</p> : null}
      {!data && !error ? <p className="muted">Loading…</p> : null}
      {data ? (
        <>
          <DataTable columns={COLUMNS} rows={data.detections} rowKey={(d) => d.id}
                     initialSort={{ key: "ts", dir: "desc" }}
                     renderDetail={(d) => <Evidence detection={d} />}
                     empty="No detections." />
          {data.total === 0 ? (
            <p className="muted">
              Nothing detected yet. Detectors run on demand; see <code>server/detection/README.md</code>.
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}
