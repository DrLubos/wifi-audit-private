import { useEffect, useState } from "react";

// Milestone 3, step 1: an empty app that proves the stack - it only shows
// what /api/health says. Dashboard pages come next.
export default function App() {
  const [health, setHealth] = useState(null); // null while loading
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health")
      .then(async (res) => {
        const body = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error(body?.database ? `${res.status}: database ${body.database}` : `HTTP ${res.status}`);
        }
        return body;
      })
      .then((body) => { if (!cancelled) setHealth(body); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  let status;
  if (error) {
    status = <p className="status bad">api unreachable ({error})</p>;
  } else if (health === null) {
    status = <p className="status">checking api…</p>;
  } else {
    status = (
      <p className="status ok">
        api: {health.status} · database: {health.database} · schema v{health.schema_version ?? "?"}
      </p>
    );
  }

  return (
    <main>
      <h1>wifi-audit</h1>
      <p>Passive Wi-Fi monitoring - server dashboard (empty for now).</p>
      {status}
    </main>
  );
}
