// "Signal strength by sensor" (ApDetail board): the hourly RSSI medians of
// each sensor over 7 days (D8; floor readings are censored, never a level -
// an hour with floors only is a gap), the baseline band of each sensor
// (median ± 2 robust σ), a crosshair at the hovered or focused hour with
// the tooltip beside it; ←/→ move the focus hour by hour.

import { useState } from "react";
import { thinLabels, tipStyle } from "../../charts/geometry.js";
import Tooltip from "../../charts/Tooltip.jsx";
import { useRoving } from "../../charts/useRoving.js";
import { useWidth } from "../../charts/useWidth.js";
import { fmtDay, fmtTime } from "../../format.js";
import { bandRect, bandSpan, rssiLines, rssiScale, rssiTicks, rssiTip } from "./aps.js";

const H = 220;
const dbm = (v) => String(Math.round(v)).replace("-", "−");

export default function RssiChart({ d, colors, tz }) {
  const n = d.t.length;
  const [active, setActive] = useState(null);
  const roving = useRoving(n);
  const [plot, width] = useWidth();
  const sc = rssiScale(d.series, d.baseline);
  const ticks = rssiTicks(d.t, tz);
  const tip = active != null ? rssiTip(active, d, colors, tz) : null;
  const aria = (i) => {
    const t = d.t[i] * 1000;
    const vals = Object.entries(d.series).map(([s, v]) => `${s} ${v[i] == null ? "no reading" : `${dbm(v[i])} dBm`}`);
    return `${fmtDay(t, tz)}, ${fmtTime(t, tz)}: ${vals.join(", ")}`;
  };
  return (
    <>
      <div className="chart">
        <div className="yax" aria-hidden="true" style={{ height: H, width: 54, paddingBottom: 0 }}>
          {sc.ticks.map((t, k) => <span key={t}>{k === 0 ? `${dbm(t)} dBm` : dbm(t)}</span>)}
        </div>
        <div className="rssi-plot" ref={plot} style={{ height: H }}>
          <svg aria-hidden="true" viewBox={`0 0 1000 ${H}`} preserveAspectRatio="none">
            {Object.entries(d.baseline ?? {}).map(([s, b]) => {
              const r = bandRect(b, sc.top, sc.bottom, H);
              const span = bandSpan(b, d.t, d.bucket_s);
              return <rect key={s} x={span.x} width={span.width} y={r.y} height={r.height}
                           style={{ fill: colors[s], fillOpacity: 0.16 }} />;
            })}
            {sc.ticks.slice(1, -1).map((t) => {
              const y = ((sc.top - t) / (sc.top - sc.bottom)) * H;
              return <line key={t} x1="0" x2="1000" y1={y} y2={y} className="grid" vectorEffect="non-scaling-stroke" />;
            })}
            {Object.entries(d.series).map(([s, vals]) => rssiLines(vals, sc.top, sc.bottom, H).map((pts, k) => (
              <polyline key={`${s}-${k}`} points={pts} fill="none" style={{ stroke: colors[s] }} strokeWidth="2"
                        vectorEffect="non-scaling-stroke" />
            )))}
          </svg>
          <div className="rssi-hits" role="group" onMouseLeave={() => setActive(null)}
               aria-label="RSSI of this AP at each sensor over 7 days; hover or focus to read the values at a point in time">
            {d.t.map((_, i) => (
              <div key={i} {...roving.props(i)} className="hit" aria-label={aria(i)} onMouseEnter={() => setActive(i)}
                   onFocus={() => { setActive(i); roving.setFocus(i); }} onBlur={() => setActive(null)}>
                <span className={active === i ? "x on" : "x"} />
              </div>
            ))}
            {tip && <Tooltip head={tip.head} rows={tip.rows} notes={tip.notes} style={tipStyle(active, n)} />}
          </div>
        </div>
      </div>
      <div className="xax" aria-hidden="true" style={{ marginLeft: 64 }}>
        {thinLabels(ticks, width).map((x) => (
          <span key={x.left} style={x.alignRight ? { right: 0 } : { left: x.left }}>{x.t}</span>
        ))}
      </div>
    </>
  );
}
