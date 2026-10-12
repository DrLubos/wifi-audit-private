// "Clients on managed networks – when is it busy" (Networks board): day by
// day (rows = local days of the period, 24 hourly cells) or the average week
// (rows Mon-Sun, each cell the mean of the period's matching weekday-hours;
// not for 24h). Hatched = outside the period, no sensor collecting, or the
// client counts were not recorded. One color scale for every period, view
// and sensor filter: thresholds from the account's 30-day peak (the api's
// ramp), shown in the legend.

import Heatmap from "../../charts/Heatmap.jsx";
import { RAMP_COLORS as RAMP, rampIndex, rampLabels } from "../../charts/ramp.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { fmtInt } from "../../format.js";
import { cellText } from "./networks.js";
const AXIS = ["00:00", "06:00", "12:00", "18:00", "23:00"];

export default function ClientsHeatmap({ sensor, range, view, setView }) {
  const v = range === "24h" ? "days" : view;
  const [key, url] = ep.clientsHeatmap(sensor, range, v);
  const q = useQuery(key, url);
  const d = q.data;
  const shownView = d?.view ?? v;          // the previous response is shown while the next loads
  const rows = d ? d.rows.map((row, r) => ({
    key: row.date ?? row.label ?? r,
    label: row.label,
    cells: row.cells.map((cell, h) => ({
      bg: cell.state === "ok" ? RAMP[rampIndex(cell.value, d.ramp)] : "var(--hatch)",
      text: cellText(shownView, row, h, cell),
    })),
  })) : [];
  const sub = (shownView === "days" ? "Sum of clients on all managed networks per hour"
    : "Average for each weekday and hour over the period")
    + (d?.peak?.value != null ? ` · peak ${fmtInt(d.peak.value)} (${d.peak.label})` : "");
  const hint = range === "24h"
    ? "Hover a cell for the hour and the number of clients per network. Average week needs a period of at least 7 days."
    : "Hover a cell for the hour and the number of clients per network.";
  return (
    <section className="card panel" aria-labelledby="cli-h" style={{ gap: 12 }}>
      <div className="card-head" style={{ alignItems: "flex-end" }}>
        <div className="card-title">
          <h2 id="cli-h">Clients on managed networks – when is it busy</h2>
          <span className="muted small">{d ? sub : "…"}</span>
        </div>
        <div role="group" aria-label="View" className="seg">
          <button type="button" aria-pressed={v === "days"} onClick={() => setView("days")}>Day by day</button>
          <button type="button" aria-pressed={v === "week"} aria-disabled={range === "24h"}
                  title={range === "24h" ? "needs a period of at least 7 days" : undefined}
                  onClick={() => { if (range !== "24h") setView("week"); }}>Average week</button>
        </div>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={220} />}
      {d && (
        <div className={q.isPrevious ? "stale" : undefined}>
          <Heatmap rows={rows} hint={hint} axis={AXIS} compact={rows.length > 10}
                   ariaLabel="Heatmap of clients on managed networks by day and hour; hover a cell for the value" />
        </div>
      )}
      <div className="heat-foot">
        <span className="muted tiny" style={{ flex: "1 1 420px" }}>
          Clients = stations the AP reports in its BSS Load element; for APs without it, clients seen by the sensor.
          Not unique people: one person often carries several devices. Hatched = outside the period, no sensor
          collecting or the client counts were not recorded.
          {d?.scale?.peak_30d != null && ` Colors: one scale for every period, up to the busiest hour of the last 30 days (${fmtInt(d.scale.peak_30d)} clients).`}
        </span>
        {d && (
          <span className="ramp" aria-label={`Color scale, clients per hour: ${rampLabels(d.ramp).join(", ")}`}>
            {rampLabels(d.ramp).map((t, i) => (
              <span className="step" key={t}><span>{t}</span><i style={{ background: RAMP[i] }} /></span>
            ))}
          </span>
        )}
      </div>
    </section>
  );
}
