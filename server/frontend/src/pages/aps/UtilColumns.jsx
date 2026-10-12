// "Utilization of channel N – last 7 days" (ApDetail board): the AP's own
// BSS Load reports per 6-hour window, the column = the average, the
// tooltip adds the hourly peak; hatched = no sensor collecting.

import { useState } from "react";
import { thinLabels, tipStyle } from "../../charts/geometry.js";
import Tooltip from "../../charts/Tooltip.jsx";
import { useRoving } from "../../charts/useRoving.js";
import { useWidth } from "../../charts/useWidth.js";

const pctText = (v) => (v == null ? "–" : `${Math.round(v)} %`);

export default function UtilColumns({ avg, peak, on, labels, xlabels }) {
  const n = labels.length;
  const [active, setActive] = useState(null);
  const roving = useRoving(n);
  const [plot, width] = useWidth();
  const top = Math.max(1, ...peak.filter((v) => v != null));
  return (
    <>
      <div className="plot" ref={plot} role="group" onMouseLeave={() => setActive(null)} style={{ height: 160 }}
           aria-label="Columns of channel utilization per 6-hour window; hover or focus for the average and the peak">
        {labels.map((l, i) => (
          <div key={i} {...roving.props(i)} className={active === i ? "col on" : "col"}
               aria-label={on[i] ? `${l}: average ${pctText(avg[i])}, peak ${pctText(peak[i])}` : `${l}: no sensor collecting`}
               onMouseEnter={() => setActive(i)} onFocus={() => { setActive(i); roving.setFocus(i); }}
               onBlur={() => setActive(null)}>
            {!on[i] ? <div className="gap" />
              : avg[i] != null && <div className="seg-top" style={{ height: `${((avg[i] / top) * 100).toFixed(1)}%`,
                                                                  background: "var(--c1)", minHeight: 2 }} />}
          </div>
        ))}
        {active != null && (
          <Tooltip head={labels[active]} style={tipStyle(active, n)}
                   rows={on[active] && avg[active] != null
                     ? [{ c: "var(--c1)", v: pctText(avg[active]), l: "average" },
                        { c: "transparent", v: pctText(peak[active]), l: "hourly peak" }] : []}
                   notes={!on[active] ? ["No sensor was collecting in this window"]
                     : avg[active] == null ? ["No utilization reported in this window"] : []} />
        )}
      </div>
      <div className="xax" aria-hidden="true" style={{ marginLeft: 0 }}>
        {thinLabels(xlabels, width).map((x) => (
          <span key={x.left} style={x.alignRight ? { right: 0 } : { left: x.left }}>{x.t}</span>
        ))}
      </div>
    </>
  );
}
