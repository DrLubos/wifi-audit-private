// GET a JSON endpoint with the ETag of the copy already held: a 304 keeps
// it (PLAN.md 8.3). cache "no-store": the browser's own HTTP cache would
// answer the If-None-Match itself; the query cache is the only cache here.
// Errors carry the api's detail ({"detail": ...}, a string or FastAPI's
// validation list).

export async function getJson(url, etag) {
  const headers = { Accept: "application/json" };
  if (etag) headers["If-None-Match"] = etag;
  const res = await fetch(url, { headers, cache: "no-store" });
  if (res.status === 304) return { status: 304 };
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const d = body?.detail ?? (body?.database ? `database ${body.database}` : null);
    const text = d == null ? "" : typeof d === "string" ? d : JSON.stringify(d);
    throw new Error(text ? `${res.status}: ${text}` : `HTTP ${res.status}`);
  }
  return { status: 200, data: body, etag: res.headers.get("ETag") };
}
