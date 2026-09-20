import { useEffect, useState } from "react";
import { fetchJson } from "../api";

const INTERVAL_MS = 60_000;

// Small api/database indicator for the header; re-checks every minute.
export default function HealthBadge() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const check = () => fetchJson("/api/health")
      .then((h) => { if (!cancelled) { setHealth(h); setError(null); } })
      .catch((e) => { if (!cancelled) { setHealth(null); setError(e.message); } });
    check();
    const id = setInterval(check, INTERVAL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  let cls = "badge";
  let text = "checking api";
  if (error) {
    cls += " bad";
    text = `api unreachable (${error})`;
  } else if (health) {
    cls += " ok";
    text = `api ${health.status} · schema v${health.schema_version ?? "?"}`;
  }
  return (
    <span className={cls} title="GET /api/health">
      <span className="dot" aria-hidden="true" />
      {text}
    </span>
  );
}
