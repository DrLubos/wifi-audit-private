// A modal dialog on the native <dialog> element: showModal() gives the focus
// trap, the backdrop, Esc to close and the page behind it inert; the title
// labels it; a click on the backdrop closes it. Rendered only while open
// (the parent mounts it), so its form state starts fresh every time.

import { useEffect, useRef } from "react";

export default function Modal({ title, onClose, children, wide = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (el && !el.open) el.showModal();
    return () => { if (el?.open) el.close(); };
  }, []);
  return (
    <dialog ref={ref} className={wide ? "modal wide" : "modal"} aria-labelledby="modal-title"
            onCancel={(e) => { e.preventDefault(); onClose(); }}
            onMouseDown={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="modal-head">
        <h2 id="modal-title">{title}</h2>
        <button type="button" className="btn2 modal-x" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}
