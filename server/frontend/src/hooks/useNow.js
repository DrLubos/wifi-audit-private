// The current time, re-rendering every intervalMs ("12 s ago", the end of
// the period label).

import { useEffect, useState } from "react";

export function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
