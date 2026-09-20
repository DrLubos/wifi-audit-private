import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { fmtDateTime, fmtDbm, fmtInt, fmtNum } from "../format";

// RSSI over time for one AP: the bucketed median as a 2 px line, the bucket
// min-max as a wash of the same hue, and - when the AP has a baseline - its
// median as a dashed neutral line with a +-3 robust-sigma band behind the data.
// One y axis (dBm). Crosshair readout above the plot lists every value at the
// hovered bucket; a table view keeps the numbers reachable without hovering.
//
// data: { t: [unix s], median: [], min: [], max: [], n: [] }
// baseline: { median, robust_sd, sd } | null

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const hiddenSeries = (label) => ({ label, width: 0, stroke: "transparent", points: { show: false } });

export default function RssiChart({ data, baseline, loading = false, height = 320 }) {
  const wrapRef = useRef(null);
  const [cursor, setCursor] = useState(null);
  const [themeTick, setThemeTick] = useState(0);

  // Re-create the plot when the OS theme flips: the colors are read from CSS once.
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setThemeTick((n) => n + 1);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const hasData = Boolean(data && data.t && data.t.length);
  const hasBaseline = Boolean(baseline && baseline.median != null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || !hasData) return undefined;

    const blue = cssVar("--series-1");
    const blueFill = cssVar("--series-1-fill");
    const gray = cssVar("--baseline");
    const grayFill = cssVar("--baseline-fill");
    const grid = cssVar("--grid");
    const ink = cssVar("--text-2");
    const surface = cssVar("--surface");
    const font = `12px ${cssVar("--font") || "system-ui"}`;

    const n = data.t.length;
    const columns = [data.t];
    const series = [{}];
    const bands = [];

    // Baseline first so the data draws over it.
    let blMedian = null;
    if (hasBaseline) {
      blMedian = baseline.median;
      const sigma = baseline.robust_sd ?? 0;
      columns.push(Array(n).fill(blMedian + 3 * sigma),
                   Array(n).fill(blMedian - 3 * sigma),
                   Array(n).fill(blMedian));
      series.push(hiddenSeries("baseline +3 sigma"),
                  hiddenSeries("baseline -3 sigma"),
                  { label: "baseline", stroke: gray, width: 1, dash: [6, 4], points: { show: false } });
      bands.push({ series: [1, 2], fill: grayFill });
    }
    const maxIdx = columns.length;
    columns.push(data.max, data.min, data.median);
    series.push(hiddenSeries("max"), hiddenSeries("min"),
                { label: "median", stroke: blue, width: 2, points: { show: false } });
    bands.push({ series: [maxIdx, maxIdx + 1], fill: blueFill });

    const axisBase = {
      stroke: ink,
      font,
      grid: { stroke: grid, width: 1 },
      ticks: { stroke: grid, width: 1 },
    };

    const opts = {
      width: el.clientWidth || 600,
      height,
      legend: { show: false },
      cursor: {
        x: true,
        y: false,
        points: { size: 9, width: 2, stroke: blue, fill: surface },
      },
      scales: {
        x: { time: true },
        y: { range: (_u, min, max) => [Math.floor(min - 2), Math.ceil(max + 2)] },
      },
      axes: [
        { ...axisBase },
        { ...axisBase, size: 56, label: "RSSI (dBm)", labelFont: font, labelSize: 18 },
      ],
      series,
      bands,
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx;
          if (i == null || i < 0 || i >= n) {
            setCursor(null);
          } else {
            setCursor({ t: data.t[i], median: data.median[i], min: data.min[i],
                        max: data.max[i], n: data.n[i] });
          }
        }],
        // Direct label for the one reference the reader must find: the baseline.
        draw: hasBaseline ? [(u) => {
          const ctx = u.ctx;
          const y = u.valToPos(blMedian, "y", true);
          const x = u.bbox.left + u.bbox.width - 6 * devicePixelRatio;
          ctx.save();
          ctx.font = `${12 * devicePixelRatio}px ${cssVar("--font") || "system-ui"}`;
          ctx.fillStyle = ink;
          ctx.textAlign = "right";
          ctx.textBaseline = "bottom";
          ctx.fillText(`baseline ${fmtNum(blMedian, 0)} dBm`, x, y - 3 * devicePixelRatio);
          ctx.restore();
        }] : [],
      },
    };

    const plot = new uPlot(opts, columns, el);
    const ro = new ResizeObserver(() => {
      if (el.clientWidth) plot.setSize({ width: el.clientWidth, height });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      plot.destroy();
      setCursor(null);
    };
  }, [data, baseline, hasData, hasBaseline, height, themeTick]);

  if (!hasData) {
    return <p className="muted">No RSSI readings for this access point in the selected range.</p>;
  }

  // Values lead, labels follow. Without a hover the last bucket is shown.
  const last = data.t.length - 1;
  const r = cursor ?? { t: data.t[last], median: data.median[last], min: data.min[last],
                        max: data.max[last], n: data.n[last] };

  return (
    <div>
      <div className="readout" aria-live="polite">
        <span>{fmtDateTime(r.t)}{cursor ? "" : " (latest bucket)"}</span>
        <span><span className="key" /> <b>{fmtDbm(r.median)}</b> median</span>
        <span><span className="key band" /> <b>{fmtDbm(r.min)}</b> .. <b>{fmtDbm(r.max)}</b> min-max</span>
        <span><b>{fmtInt(r.n)}</b> readings</span>
        {hasBaseline ? (
          <span><span className="key base" /> <b>{fmtDbm(baseline.median)}</b> baseline
            {baseline.robust_sd != null ? ` ± ${fmtNum(3 * baseline.robust_sd, 1)} dB (3 robust sigma)` : ""}
          </span>
        ) : null}
      </div>
      <div ref={wrapRef} className={`chart ${loading ? "loading" : ""}`} />
      <details className="table-view">
        <summary>Show as table ({fmtInt(data.t.length)} buckets)</summary>
        <div className="table-wrap">
          <table className="small-table">
            <thead>
              <tr><th>Bucket start</th><th className="num">Median</th><th className="num">Min</th>
                  <th className="num">Max</th><th className="num">Readings</th></tr>
            </thead>
            <tbody>
              {data.t.map((t, i) => (
                <tr key={t}>
                  <td>{fmtDateTime(t)}</td>
                  <td className="num">{fmtNum(data.median[i], 1)}</td>
                  <td className="num">{data.min[i]}</td>
                  <td className="num">{data.max[i]}</td>
                  <td className="num">{fmtInt(data.n[i])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
