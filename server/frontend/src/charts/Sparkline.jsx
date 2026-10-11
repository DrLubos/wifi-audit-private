// The small columns of a period tile (Main board): one bar per bucket, grey
// 3 px where no sensor was collecting, the hovered bar darker, a tooltip
// above with the bucket and its value; ←/→ move the focus.

import { useState } from "react";
import { fmtInt } from "../format.js";
import Tooltip from "./Tooltip.jsx";
import { useRoving } from "./useRoving.js";

export default function Sparkline({ values, on, labels, unit, ariaLabel, gapText = () => "no sensor collecting" }) {
  const n = values.length;
  const [hv, setHv] = useState(null);
  const roving = useRoving(n);
  const m = Math.max(1, ...values.filter((v) => v != null));
  const has = (i) => on[i] && values[i] != null;
  return (
    <div className="spark" role="group" aria-label={ariaLabel} onMouseLeave={() => setHv(null)}>
      {values.map((v, i) => (
        <span key={i} {...roving.props(i)}
              aria-label={`${labels[i]}: ${has(i) ? `${fmtInt(v)} ${unit}` : gapText(i)}`}
              onMouseEnter={() => setHv(i)} onFocus={() => { setHv(i); roving.setFocus(i); }}
              onBlur={() => setHv(null)}>
          <i style={{
            height: has(i) ? `${Math.max(4, Math.round((v / m) * 100))}%` : "3px",
            background: !has(i) ? "var(--spark-off)" : hv === i ? "var(--spark-on)" : "var(--spark)",
          }} />
        </span>
      ))}
      {hv !== null && hv < n && (
        <Tooltip className="above" head={labels[hv]}
                 rows={[{ c: "var(--spark)", v: has(hv) ? fmtInt(values[hv]) : "–", l: has(hv) ? unit : gapText(hv) }]} />
      )}
    </div>
  );
}
