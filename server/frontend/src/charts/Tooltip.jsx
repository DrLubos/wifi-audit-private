// The boards' tooltip: a head line, rows "value first, then label" with a
// short colored key, notes below a hairline. Placed by the caller (beside a
// column, above a sparkline).
export default function Tooltip({ head, rows = [], notes = [], style, className = "" }) {
  return (
    <div className={`tip ${className}`} role="tooltip" style={style}>
      <div className="th">{head}</div>
      {rows.map((r) => (
        <div className="tr" key={r.l}>
          <i style={{ background: r.c }} />
          <b>{r.v}</b>
          <span>{r.l}</span>
        </div>
      ))}
      {notes.map((n, i) => <div className="tn" key={i}>{n}</div>)}
    </div>
  );
}
