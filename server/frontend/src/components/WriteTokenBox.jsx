// The write-token box of the Settings board: locked - the token field and
// Unlock (POST /api/v2/auth/check); unlocked - Lock. The token is kept for
// this browser tab only (data/write.js).

import { useState } from "react";
import { useWrite } from "../data/write.js";

export default function WriteTokenBox() {
  const w = useWrite();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const unlock = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await w.unlock(token);
      setToken("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  if (w.unlocked) {
    return (
      <section className="token-box unlocked" aria-labelledby="tok-h">
        <div className="token-text">
          <h2 id="tok-h">Changes are unlocked in this browser tab</h2>
          <span>The write token stays in this tab until you lock it or close the tab. Every change is logged.</span>
        </div>
        <button type="button" className="btn2 token-btn" onClick={w.lock}>Lock</button>
      </section>
    );
  }
  return (
    <section className="token-box" aria-labelledby="tok-h">
      <div className="token-text">
        <h2 id="tok-h">Changes are protected by a write token</h2>
        <span>
          Reading is public for now. Changing settings, labels and classification requires the write token from the
          server’s .env file until login exists.
        </span>
      </div>
      <form className="token-form" onSubmit={unlock}>
        <label className="fl">Write token
          <input type="password" className="in" autoComplete="current-password" value={token}
                 onChange={(e) => setToken(e.target.value)} aria-invalid={Boolean(error)} />
        </label>
        <button type="submit" className="btn" disabled={busy}>{busy ? "Checking…" : "Unlock"}</button>
        {error && <span className="token-error" role="alert">{error}</span>}
      </form>
    </section>
  );
}
