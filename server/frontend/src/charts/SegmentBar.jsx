// The 100 % bar of the security levels with its legend (Main board): hovering
// or focusing a segment highlights its legend row and the other way round.

import { useState } from "react";
import { fmtInt, fmtPct } from "../format.js";

export default function SegmentBar({ rows, ariaLabel }) {
  const [hl, setHl] = useState(null);
  const shown = rows.filter((r) => r.n > 0);
  const radius = (i) => {
    if (shown.length === 1) return "4px";
    if (i === 0) return "4px 0 0 4px";
    return i === shown.length - 1 ? "0 4px 4px 0" : "0";
  };
  return (
    <>
      <div className="segbar" role="group" aria-label={ariaLabel} onMouseLeave={() => setHl(null)}>
        {shown.map((r, i) => (
          <div key={r.key} tabIndex={0} aria-label={`${r.label}: ${fmtInt(r.n)} APs, ${fmtPct(r.share)}`}
               onMouseEnter={() => setHl(r.key)} onFocus={() => setHl(r.key)} onBlur={() => setHl(null)}
               style={{
                 width: `${(r.share * 100).toFixed(2)}%`, background: r.color, borderRadius: radius(i),
                 outline: hl === r.key ? "2px solid var(--text)" : "none",
               }} />
        ))}
      </div>
      <div className="seglegend">
        {rows.map((r) => (
          <span key={r.key} className={hl === r.key ? "on" : undefined}
                onMouseEnter={() => setHl(r.key)} onMouseLeave={() => setHl(null)}>
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="sw" style={{ background: r.color }} />{r.label}
            </span>
            <span>{fmtInt(r.n)} APs · {fmtPct(r.share)}</span>
          </span>
        ))}
      </div>
    </>
  );
}
