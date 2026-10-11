import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <main className="content v2">
      <h1>Not found</h1>
      <span className="muted">Nothing here. <Link to="/">Overview</Link></span>
    </main>
  );
}
