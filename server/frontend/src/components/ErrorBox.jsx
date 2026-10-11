// A card's own error: the rest of the page keeps working.
export default function ErrorBox({ error }) {
  return <div className="error-box" role="alert">Could not load: {error}</div>;
}
