// Loading placeholder that keeps the layout's shape (no jump when data lands).
// Motion is a soft shimmer, off under prefers-reduced-motion.
export default function Skeleton({ height = "1em", width = "100%", label = null }) {
  return (
    <div className="skeleton" style={{ height, width }} role="status" aria-live="polite"
         aria-label={label ?? "Loading"}>
      {label ? <span className="skeleton-label">{label}</span> : null}
    </div>
  );
}
