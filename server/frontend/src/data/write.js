// The write path (PLAN.md 7.5): until login exists every write needs the
// write token from the server's .env. The operator unlocks once per browser
// tab (sessionStorage - gone when the tab closes, never sent anywhere but
// this site's /api/v2), send() calls a write endpoint with Authorization:
// Bearer and, when it succeeds, marks every cached read stale and refetches
// the ones the page names (a write bumps the account's write_version, so
// every ETag changes). A 401 locks again. createWriteState() is the testable
// core (write.test.js); useWrite() the hook the shell and the pages use.

import { useSyncExternalStore } from "react";
import { store } from "./query.js";

const KEY = "wifi-audit.write-token";

export class WriteError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function detail(res) {
  try {
    const body = await res.json();
    const d = body?.detail;
    return typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export function createWriteState(storage, fetchImpl, cache) {
  let token = null;
  try {
    token = storage?.getItem(KEY) || null;
  } catch {
    token = null;
  }
  const listeners = new Set();
  let snapshot = Object.freeze({ unlocked: Boolean(token) });
  const emit = () => {
    snapshot = Object.freeze({ unlocked: Boolean(token) });
    for (const fn of listeners) fn();
  };
  const remember = (t) => {
    token = t;
    try {
      if (t) storage?.setItem(KEY, t); else storage?.removeItem(KEY);
    } catch {
      // private mode: unlocked for this page only
    }
    emit();
  };

  // POST /auth/check with the token: 204 unlocks, 401 / 403 throw
  async function unlock(candidate) {
    const t = (candidate ?? "").trim();
    if (!t) throw new WriteError(401, "enter the write token");
    const res = await fetchImpl("/api/v2/auth/check", { method: "POST", headers: { Authorization: `Bearer ${t}` } });
    if (res.status === 204) {
      remember(t);
      return true;
    }
    if (res.status === 403) throw new WriteError(403, "writes are disabled on the server (no WRITE_TOKEN in its .env)");
    throw new WriteError(res.status, res.status === 401 ? "wrong write token" : await detail(res));
  }

  // a write: JSON body, the token; afterwards every read is stale and the
  // keys starting with one of `refetch` are loaded again
  async function send(method, url, body, refetch = []) {
    if (!token) throw new WriteError(401, "locked - unlock with the write token first");
    const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetchImpl(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (res.status === 401) {
      remember(null);
      throw new WriteError(401, "the write token was refused - unlock again");
    }
    if (!res.ok) throw new WriteError(res.status, await detail(res));
    const data = res.status === 204 ? null : await res.json();
    cache?.markStale();
    for (const prefix of refetch) cache?.invalidate(prefix);
    return data;
  }

  // a GET behind the token (the label export): the blob and the file the api names
  async function download(url) {
    if (!token) throw new WriteError(401, "locked - unlock with the write token first");
    const res = await fetchImpl(url, { method: "GET", headers: { Authorization: `Bearer ${token}` } });
    if (res.status === 401) {
      remember(null);
      throw new WriteError(401, "the write token was refused - unlock again");
    }
    if (!res.ok) throw new WriteError(res.status, await detail(res));
    const m = /filename="([^"]+)"/.exec(res.headers?.get?.("Content-Disposition") ?? "");
    return { blob: await res.blob(), filename: m ? m[1] : "export.csv" };
  }

  return {
    get: () => snapshot,
    subscribe: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    unlock, send, download, lock: () => remember(null),
  };
}

function sessionStore() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export const writer = createWriteState(
  typeof window === "undefined" ? null : sessionStore(),
  (url, init) => fetch(url, { ...init, cache: "no-store" }),
  store,
);

// hand a downloaded blob to the browser as a file
export function saveFile({ blob, filename }) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// {unlocked, unlock(token), lock(), send(method, url, body, refetchPrefixes), download(url)}
export function useWrite() {
  const s = useSyncExternalStore(writer.subscribe, writer.get);
  return { unlocked: s.unlocked, unlock: writer.unlock, lock: writer.lock, send: writer.send, download: writer.download };
}
