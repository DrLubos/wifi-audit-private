// Stat tile: label, value, optional hint. Values wear text tokens, never a series color.
export default function StatTile({ label, value, hint }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value ?? "-"}</div>
      {hint ? <div className="tile-hint">{hint}</div> : null}
    </div>
  );
}
