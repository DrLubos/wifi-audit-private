// What the form tabs share: a draft that follows the stored data until it is
// edited (useDraft), and the bar with Discard / Save and the result of the
// last save (SaveBar). Saving needs the write token (useWrite).

import { useEffect, useRef, useState } from "react";
import { useWrite } from "../../data/write.js";
import { sameBody } from "./settings.js";

// draft = toDraft(data) until edited; a refetch replaces an unedited draft
export function useDraft(data, toDraft, toBody) {
  const [draft, setDraft] = useState(null);
  const from = useRef(null);
  const saved = data ? toDraft(data) : null;
  const dirty = Boolean(draft && saved && !sameBody(toBody(draft), toBody(saved)));
  useEffect(() => {
    if (!data) return;
    if (draft === null || from.current === null || (data !== from.current && !dirty)) {
      setDraft(toDraft(data));
      from.current = data;
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  const reset = () => {
    setDraft(toDraft(data));
    from.current = data;
  };
  return { draft, setDraft, dirty, reset };
}

// save(): a promise from w.send(...); the bar shows its outcome
export function useSave() {
  const w = useWrite();
  const [state, setState] = useState({ busy: false, ok: null, error: null });
  const run = async (fn, okText) => {
    setState({ busy: true, ok: null, error: null });
    try {
      const out = await fn(w);
      setState({ busy: false, ok: typeof okText === "function" ? okText(out) : okText, error: null });
      return out;
    } catch (e) {
      setState({ busy: false, ok: null, error: e.message });
      return null;
    }
  };
  return { w, state, run, clear: () => setState({ busy: false, ok: null, error: null }) };
}

export function SaveBar({ label, dirty, problem, save, onSave, onDiscard }) {
  const locked = !save.w.unlocked;
  const why = locked ? "Unlock with the write token above first" : problem || (!dirty ? "Nothing changed" : undefined);
  return (
    <div className="save-bar">
      <span className="save-msg" role="status">
        {save.state.error && <span className="bad-text">{save.state.error}</span>}
        {!save.state.error && save.state.ok && <span className="ok-text">{save.state.ok}</span>}
        {!save.state.error && !save.state.ok && problem && dirty && <span className="bad-text">{problem}</span>}
        {!save.state.error && !save.state.ok && !problem && dirty && <span className="muted">Unsaved changes</span>}
      </span>
      <button type="button" className="btn2 btn-lg" disabled={!dirty || save.state.busy} onClick={onDiscard}>
        Discard changes
      </button>
      <button type="button" className="btn" disabled={Boolean(why) || save.state.busy} title={why} onClick={onSave}>
        {save.state.busy ? "Saving…" : label}
      </button>
    </div>
  );
}
