// A loading placeholder of the card's size (cards load in parallel; a slow
// card never blocks the others).
export default function Skeleton({ height = 20, width = "100%", style }) {
  return <div className="skeleton" aria-hidden="true" style={{ height, width, ...style }} />;
}
