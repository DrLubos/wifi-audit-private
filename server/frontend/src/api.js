import { useEffect, useState } from "react";

// Fetch a JSON endpoint; the api returns {"detail": ...} on errors.
export async function fetchJson(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail = body?.detail ?? (body?.database ? `database ${body.database}` : null);
    throw new Error(detail ? `${res.status}: ${detail}` : `HTTP ${res.status}`);
  }
  return body;
}

// { data, error, loading } for one GET. On a path change the previous data is
// kept while the new request runs (charts hold their frame instead of flashing).
export function useApi(path) {
  const [state, setState] = useState({ data: null, error: null, loading: Boolean(path) });

  useEffect(() => {
    if (!path) return undefined;
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    fetchJson(path)
      .then((data) => { if (!cancelled) setState({ data, error: null, loading: false }); })
      .catch((e) => { if (!cancelled) setState({ data: null, error: e.message, loading: false }); });
    return () => { cancelled = true; };
  }, [path]);

  return state;
}
