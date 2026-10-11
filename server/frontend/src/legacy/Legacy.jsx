// A page of the previous dashboard inside the new shell, with its own scoped
// stylesheet, until its replacement lands (PLAN.md 8.1; removed in phase 5).

import "./legacy.css";

export default function Legacy({ children }) {
  return (
    <div className="legacy">
      <main>{children}</main>
    </div>
  );
}
