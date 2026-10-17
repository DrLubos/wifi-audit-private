"""Rate limits of the ingest (docs/live-upload.md 9.2), in the api process:
Caddy has no rate limiter without a custom build. One uvicorn worker, so
in-memory state is the whole truth; the routes run in a thread pool, hence
the lock. Times are seconds of any monotonic clock (passed in, for tests).

- Buckets: a token bucket per key (a sensor and a route) - steady state is
  one batch per poll (2 a minute), a catch-up one every ~2 s.
- FailWindow: failed authentications per client address; too many in a
  window block that address for a while (log noise, not security - the
  tokens are unguessable).

Standard library only (tests/test_ingest_logic.py).
"""

import threading


class Buckets:
    def __init__(self, per_minute, burst):
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self._state = {}
        self._lock = threading.Lock()

    def allow(self, key, now):
        """(allowed, seconds until a request would be allowed)."""
        with self._lock:
            tokens, last = self._state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._state[key] = (tokens - 1.0, now)
                return True, 0.0
            self._state[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate


class FailWindow:
    def __init__(self, limit=20, window_s=60, block_s=600):
        self.limit, self.window_s, self.block_s = limit, window_s, block_s
        self._fails = {}
        self._blocked = {}
        self._lock = threading.Lock()

    def failed(self, key, now):
        with self._lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self.window_s] + [now]
            self._fails[key] = recent
            if len(recent) >= self.limit:
                self._blocked[key] = now + self.block_s
                self._fails.pop(key, None)
            if len(self._fails) > 10000:             # never grow without bound
                self._fails.clear()

    def blocked(self, key, now):
        """Seconds the key stays blocked (0 = not blocked)."""
        with self._lock:
            until = self._blocked.get(key)
            if until is None:
                return 0
            if now >= until:
                del self._blocked[key]
                return 0
            return until - now
