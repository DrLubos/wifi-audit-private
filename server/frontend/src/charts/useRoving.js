// Roving keyboard focus over a row of n items (chart columns, sparkline
// bars): one item is in the tab order, ←/→ (Home/End) move focus along the
// row. Starts at the latest item.

import { useRef, useState } from "react";

export function useRoving(n) {
  const [focus, setFocus] = useState(null);
  const refs = useRef([]);
  const current = focus == null || focus >= n ? n - 1 : focus;
  const move = (i) => {
    const j = Math.max(0, Math.min(n - 1, i));
    setFocus(j);
    refs.current[j]?.focus();
  };
  const props = (i) => ({
    ref: (el) => { refs.current[i] = el; },
    tabIndex: i === current ? 0 : -1,
    onKeyDown: (e) => {
      const to = { ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: n - 1 }[e.key];
      if (to !== undefined) {
        e.preventDefault();
        move(to);
      }
    },
  });
  return { props, setFocus };
}
