// Heatmap (PLAN.md 8.4, Networks board): rows of cells colored on the
// sequential blue ramp, hatched where there is no value (no sensor
// collecting, outside the period, load not recorded); the hovered or focused
// cell is outlined and its text shown in the hint line above (aria-live);
// keyboard: one cell in the tab order, arrow keys move in both directions.

import { useRef, useState } from "react";

export default function Heatmap({ rows, ariaLabel, hint, axis, compact }) {
  const [on, setOn] = useState(null);           // {r, c} under the pointer or focused
  const [focus, setFocus] = useState({ r: 0, c: 0 });
  const refs = useRef({});
  const rowH = compact ? 12 : 22;
  const move = (r, c) => {
    const rr = Math.max(0, Math.min(rows.length - 1, r));
    const cc = Math.max(0, Math.min((rows[rr]?.cells.length ?? 1) - 1, c));
    setFocus({ r: rr, c: cc });
    refs.current[`${rr}:${cc}`]?.focus();
  };
  const onKey = (e, r, c) => {
    const to = { ArrowLeft: [r, c - 1], ArrowRight: [r, c + 1], ArrowUp: [r - 1, c], ArrowDown: [r + 1, c],
                 Home: [r, 0], End: [r, 23] }[e.key];
    if (to) {
      e.preventDefault();
      move(...to);
    }
  };
  const current = on ? rows[on.r]?.cells[on.c]?.text : null;
  return (
    <>
      <div className="hint" aria-live="polite">{current ?? hint}</div>
      <div className="heat" role="group" aria-label={ariaLabel} onMouseLeave={() => setOn(null)}
           style={{ gap: compact ? 2 : 3 }}>
        {rows.map((row, r) => (
          <div className="heat-row" key={row.key ?? r}>
            <span className="heat-label">{row.label}</span>
            {row.cells.map((cell, c) => (
              <span key={c} ref={(el) => { refs.current[`${r}:${c}`] = el; }}
                    className={on && on.r === r && on.c === c ? "cell on" : "cell"}
                    tabIndex={focus.r === r && focus.c === c ? 0 : -1} aria-label={cell.text}
                    onMouseEnter={() => setOn({ r, c })} onFocus={() => { setOn({ r, c }); setFocus({ r, c }); }}
                    onBlur={() => setOn(null)} onKeyDown={(e) => onKey(e, r, c)}
                    style={{ height: rowH, background: cell.bg }} />
            ))}
          </div>
        ))}
        <div className="heat-axis" aria-hidden="true">{axis.map((t) => <span key={t}>{t}</span>)}</div>
      </div>
    </>
  );
}
