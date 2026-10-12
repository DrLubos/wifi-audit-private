// Channel occupancy (Spectrum board): one column per channel with its AP
// count on top, blue = 1 / 6 / 11 (2.4 GHz) or non-DFS (5 GHz), orange =
// overlapping / DFS; the tooltip beside the hovered or focused column (APs,
// average utilization, the overlap / DFS note); ←/→ move the focus.

import { useState } from "react";
import { tipStyle } from "../../charts/geometry.js";
import Tooltip from "../../charts/Tooltip.jsx";
import { useRoving } from "../../charts/useRoving.js";
import { fmtInt } from "../../format.js";

export default function ChannelColumns({ items, ariaLabel }) {
  const [active, setActive] = useState(null);
  const roving = useRoving(items.length);
  const a = active != null ? items[active] : null;
  return (
    <>
      <div className="plot occ" role="group" aria-label={ariaLabel} onMouseLeave={() => setActive(null)}>
        {items.map((c, i) => (
          <div key={c.ch} {...roving.props(i)} className={active === i ? "col on" : "col"} aria-label={c.aria}
               onMouseEnter={() => setActive(i)} onFocus={() => { setActive(i); roving.setFocus(i); }}
               onBlur={() => setActive(null)}>
            <span className="occ-n">{fmtInt(c.n)}</span>
            <div className="occ-bar" style={{ height: c.h, background: c.fill }} />
          </div>
        ))}
        {a && <Tooltip head={a.head} rows={a.rows} notes={a.notes} style={tipStyle(active, items.length)} />}
      </div>
      <div className="occ-x" aria-hidden="true">{items.map((c) => <span key={c.ch}>{c.ch}</span>)}</div>
    </>
  );
}
