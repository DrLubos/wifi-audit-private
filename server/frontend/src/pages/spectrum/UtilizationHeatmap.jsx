// "Channel utilization (QBSS)" (Spectrum board): the airtime the APs report
// busy in their BSS Load element, per channel of the band - the average day
// (24 local hours over the period) or the timeline (24 x 1 h, 168 x 1 h,
// 180 x 4 h). Hatched = no sensor collecting or no AP reported (the load
// history starts with collector buffer v7). The board's fixed scale (10-65 %).

import Heatmap from "../../charts/Heatmap.jsx";
import { RAMP_COLORS, rampLabels } from "../../charts/ramp.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { timelineLabel, UTIL_RAMP, utilCellText, utilColor, utilSub, utilTicks } from "./spectrum.js";

const HINT = "Hover a cell: channel, time, utilization and the number of APs reporting on the channel.";

export default function UtilizationHeatmap({ sensor, range, band, view, setView, tz, now }) {
  const [key, url] = ep.spectrumUtilization(sensor, range, band, view);
  const q = useQuery(key, url);
  const d = q.data;
  // the response's own view and range: while the next one loads the previous
  // response is shown (stale), and a day row has no timeline cells
  const shown = d ? { view: d.view, range: d.filters?.range ?? range } : { view, range };
  const rows = d ? d.rows.map((row) => ({
    key: row.channel,
    label: `channel ${row.channel}`,
    cells: row.cells.map((cell) => ({ bg: utilColor(cell), text: utilCellText(shown.view, row.channel, cell, tz, now) })),
  })) : [];
  const ticks = d ? utilTicks(shown.view, shown.range, d.rows[0]?.cells ?? [], tz) : [];
  const dense = shown.view === "timeline" && shown.range !== "24h";
  const legend = rampLabels(UTIL_RAMP, " %");
  return (
    <section className="card panel" aria-labelledby="ut-h" style={{ gap: 12 }}>
      <div className="card-head" style={{ alignItems: "flex-end" }}>
        <div className="card-title">
          <h2 id="ut-h">Channel utilization (QBSS)</h2>
          <span className="muted small">{utilSub(view, range)}</span>
        </div>
        <div role="group" aria-label="View" className="seg">
          <button type="button" aria-pressed={view === "day"} onClick={() => setView("day")}>Average day</button>
          <button type="button" aria-pressed={view === "timeline"} onClick={() => setView("timeline")}>
            {timelineLabel(range)}
          </button>
        </div>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && <Skeleton height={260} />}
      {d && rows.length === 0 && (
        <p className="muted small" style={{ margin: 0 }}>No AP on {band === "5" ? "5" : "2.4"} GHz reported its utilization in this period.</p>
      )}
      {d && rows.length > 0 && (
        <div className={dense ? "scroll-x" : undefined}>
          <div className={[q.isPrevious ? "stale" : "", dense ? "wide" : ""].join(" ").trim() || undefined}>
            <Heatmap rows={rows} hint={HINT} ticks={ticks} dense={dense}
                     ariaLabel={`Heatmap of utilization of ${rows.length} channels; hover a cell for the value`} />
          </div>
        </div>
      )}
      <div className="heat-foot">
        <span className="muted tiny" style={{ flex: "1 1 420px" }}>
          Only APs that broadcast the QBSS (BSS Load) element; each cell is the mean of the APs reporting on the
          channel. Hatched = outside the period, no sensor collecting or no AP reported utilization.
        </span>
        <span className="ramp" aria-label={`Color scale, utilization: ${legend.join(", ")}`}>
          {legend.map((t, i) => (
            <span className="step" key={t}><span>{t}</span><i style={{ background: RAMP_COLORS[i] }} /></span>
          ))}
        </span>
      </div>
    </section>
  );
}
