// Coverage per day (Sensors board): one column per local day, the shares of
// the day stacked to 100 % (no data yet, outage, limited, successful, not
// installed - top to bottom), the whole column the hover / focus target, the
// tooltip beside it with the shares and the documented causes of that day;
// ←/→ move the focus. Day labels shrink to the day number, then every other
// day, when the plot is narrow.

import { useState } from "react";
import { tipStyle } from "../../charts/geometry.js";
import Tooltip from "../../charts/Tooltip.jsx";
import { useRoving } from "../../charts/useRoving.js";
import { useWidth } from "../../charts/useWidth.js";

export default function CoverageChart({ cols, height = 160 }) {
  const [active, setActive] = useState(null);
  const roving = useRoving(cols.length);
  const [ref, width] = useWidth();
  const per = width ? width / Math.max(1, cols.length) : Infinity;
  const label = (c, i) => {
    if (per >= 46) return c.label;
    if (per >= 22) return c.label.split(" ")[0];
    return i % 2 === (cols.length - 1) % 2 ? c.label.split(" ")[0] : "";
  };
  return (
    <>
      <div className="plot cov-plot" ref={ref} role="group" onMouseLeave={() => setActive(null)} style={{ height }}
           aria-label="Stacked columns of daily coverage; hover or focus a day for the shares">
        {cols.map((c, i) => (
          <div key={c.date} {...roving.props(i)} className={active === i ? "col on" : "col"} aria-label={c.aria}
               onMouseEnter={() => setActive(i)} onFocus={() => { setActive(i); roving.setFocus(i); }}
               onBlur={() => setActive(null)}>
            {c.segs.map((s, j) => (
              <div key={s.key} className={j === 0 ? "seg-top" : undefined}
                   style={{ height: `${(s.share * 100).toFixed(1)}%`, background: s.color }} />
            ))}
          </div>
        ))}
        {active != null && active < cols.length && (
          <Tooltip head={cols[active].head} rows={cols[active].rows} notes={cols[active].notes}
                   style={tipStyle(active, cols.length)} />
        )}
      </div>
      <div className="cov-labels" aria-hidden="true">
        {cols.map((c, i) => <span key={c.date}>{label(c, i)}</span>)}
      </div>
    </>
  );
}
