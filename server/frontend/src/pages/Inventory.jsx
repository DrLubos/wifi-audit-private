import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApi } from "../api";
import DataTable from "../components/DataTable";
import { fmtDateTime, fmtInt, fmtNum } from "../format";

const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "baseline", label: "With baseline", test: (a) => a.baseline_n_obs != null },
  { id: "nobaseline", label: "No baseline", test: (a) => a.baseline_n_obs == null },
  { id: "hidden", label: "Hidden SSID", test: (a) => a.hidden },
  { id: "random", label: "Randomised BSSID", test: (a) => a.random_bssid },
  { id: "trusted", label: "Trusted", test: (a) => a.trusted },
];

const COLUMNS = [
  { key: "ssid", label: "SSID",
    render: (a) => (a.hidden ? <span className="muted">&lt;hidden&gt;</span> : a.ssid),
    sortValue: (a) => (a.hidden ? null : a.ssid) },
  { key: "bssid", label: "BSSID", className: "mono" },
  { key: "manuf", label: "Manufacturer",
    render: (a) => <>{a.manuf ?? "-"} <span className="muted mono">{a.oui}</span></> },
  { key: "crypt", label: "Encryption" },
  { key: "adv_channel", label: "Ch",
    render: (a) => <>{a.adv_channel ?? "-"}{a.ht_mode ? <span className="muted"> {a.ht_mode}</span> : null}</>,
    sortValue: (a) => (a.adv_channel ? Number(a.adv_channel) || a.adv_channel : null) },
  { key: "rssi_median", label: "RSSI median", align: "right",
    render: (a) => (a.rssi_median == null ? "-" : `${fmtNum(a.rssi_median, 0)} dBm`) },
  { key: "rssi_robust_sd", label: "Robust sd", align: "right",
    render: (a) => (a.rssi_robust_sd == null ? "-" : `${fmtNum(a.rssi_robust_sd, 1)} dB`) },
  { key: "baseline_n_obs", label: "Baseline", align: "right",
    render: (a) => (a.baseline_n_obs == null ? <span className="muted">none</span>
      : `${fmtInt(a.baseline_n_obs)} / ${a.baseline_n_days} d`) },
  { key: "trusted", label: "Trusted", render: (a) => (a.trusted ? "yes" : "-") },
  { key: "last_seen", label: "Last seen", render: (a) => fmtDateTime(a.last_seen) },
];

export default function Inventory() {
  const { data, error } = useApi("/api/aps");
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");

  const rows = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const test = FILTERS.find((f) => f.id === filter)?.test ?? (() => true);
    return data.aps.filter((a) => test(a) && (!q
      || (a.ssid ?? "").toLowerCase().includes(q)
      || a.bssid.toLowerCase().includes(q)
      || (a.manuf ?? "").toLowerCase().includes(q)));
  }, [data, query, filter]);

  return (
    <>
      <h1>Access points{data ? <small>{fmtInt(data.count)} seen by the sensor</small> : null}</h1>
      <p className="sub">
        <b>Baseline</b> = enough readings for an RSSI baseline (≥ 200 on ≥ 2 days).{" "}
        <b>Trusted</b> = operator-approved identity (none yet). Click a row for the RSSI timeline.
      </p>
      <div className="filters">
        <input type="search" placeholder="Search SSID, BSSID, manufacturer"
               value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search" />
        <div className="chips" role="group" aria-label="Filter">
          {FILTERS.map((f) => (
            <button key={f.id} type="button" className="chip" aria-pressed={filter === f.id}
                    onClick={() => setFilter(f.id)}>{f.label}</button>
          ))}
        </div>
        {data ? <span className="count">{fmtInt(rows.length)} shown</span> : null}
      </div>
      {error ? <p className="error">Failed to load: {error}</p> : null}
      {!data && !error ? <p className="muted">Loading…</p> : null}
      {data ? (
        <DataTable columns={COLUMNS} rows={rows} rowKey={(a) => a.device_key}
                   initialSort={{ key: "last_seen", dir: "desc" }}
                   onRowClick={(a) => navigate(`/aps/${a.device_key}`)}
                   empty="No access point matches." />
      ) : null}
    </>
  );
}
