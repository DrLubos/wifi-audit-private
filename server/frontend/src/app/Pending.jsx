// A page of the new dashboard that is not built yet (phase 4 builds them one
// by one, PLAN.md section 10), with the previous dashboard's page if any.

import { Link } from "react-router-dom";

export default function Pending({ title, phase, legacy }) {
  return (
    <main className="content v2">
      <h1>{title}</h1>
      <section className="card notice">
        <span>This page of the new dashboard comes with {phase}.</span>
        {legacy && (
          <span className="muted">
            Until then: <Link to={legacy.to}>{legacy.label}</Link> of the previous dashboard.
          </span>
        )}
      </section>
    </main>
  );
}
