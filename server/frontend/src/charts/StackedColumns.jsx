// Stacked columns (PLAN.md 8.4, Main board "Access points over time"): series
// stacked bottom-up in the given order with 2 px gaps, the whole column is
// the hover / focus target with a light wash, the tooltip sits beside the
// column (never over it) and lists every series value-first plus the total
// and the notes; a column with no sensor collecting is hatched (never drawn
// as zero); an event dot on top; ←/→ move the focus; x labels thinned to
// the measured plot width (narrow screens).

import { useEffect, useRef, useState } from "react";
import { fmtInt } from "../format.js";
import { niceMax, pct, thinLabels, tipStyle } from "./geometry.js";
import Tooltip from "./Tooltip.jsx";
import { useRoving } from "./useRoving.js";

// Two charts that share their x axis (Networks board) pass the hovered
// bucket in and out (active / onActive) and a common y maximum (top); only
// the chart under the pointer shows its tooltip (tip).
export default function StackedColumns({
  series, on, labels, xlabels, notes = [], events = [], height = 200, ariaLabel, columnAria,
  active: activeIn, onActive, tip = true, top: topIn, totalLabel = "total",
}) {
  const n = labels.length;
  const [own, setOwn] = useState(null);
  const active = onActive ? activeIn : own;
  const setActive = onActive ?? setOwn;
  const roving = useRoving(n);
  const plot = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!plot.current || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver((entries) => setWidth(entries[0].contentRect.width));
    ro.observe(plot.current);
    return () => ro.disconnect();
  }, []);
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const top = topIn ?? niceMax(Math.max(0, ...totals));
  const topDown = [...series].reverse();

  const enter = (i) => setActive(i);
  const tipRows = (i) => (on[i]
    ? [...topDown.map((s) => ({ c: s.color, v: fmtInt(s.values[i] ?? 0), l: s.label })),
       { c: "transparent", v: fmtInt(totals[i]), l: totalLabel }]
    : []);

  return (
    <>
      <div className="chart">
        <div className="yax" aria-hidden="true" style={{ height }}>
          <span>{top}</span><span>{top / 2}</span><span>0</span>
        </div>
        <div className="plot" ref={plot} role="group" aria-label={ariaLabel} onMouseLeave={() => setActive(null)}
             style={{ height }}>
          {labels.map((label, i) => {
            const segs = topDown.filter((s) => (s.values[i] || 0) > 0);
            return (
              <div key={i} {...roving.props(i)} className={active === i ? "col on" : "col"}
                   aria-label={columnAria(i)} onMouseEnter={() => enter(i)}
                   onFocus={() => { enter(i); roving.setFocus(i); }} onBlur={() => setActive(null)}>
                {events[i] > 0 && <span className="evdot" />}
                {!on[i]
                  ? <div className="gap" />
                  : segs.map((s, j) => (
                    <div key={s.key} className={j === 0 ? "seg-top" : undefined}
                         style={{ height: pct(s.values[i], top), background: s.color }} />
                  ))}
              </div>
            );
          })}
          {tip && active != null && active < n && (
            <Tooltip head={labels[active]} rows={tipRows(active)} notes={notes[active] ?? []}
                     style={tipStyle(active, n)} />
          )}
        </div>
      </div>
      <div className="xax" aria-hidden="true">
        {thinLabels(xlabels, width).map((x) => (
          <span key={x.left} style={x.alignRight ? { right: 0 } : { left: x.left }}>{x.t}</span>
        ))}
      </div>
    </>
  );
}
