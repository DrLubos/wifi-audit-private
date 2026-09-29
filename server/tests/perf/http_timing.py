"""End-to-end request timing against the api, run INSIDE the api container
(stdlib only; measure.sh pipes it into `docker compose exec -T api python -`).

  python - cold PATH...        one request per path (the caller made the cache cold)
  python - warm N PATH...      one warm-up, then N requests per path: p50 / p95 / max

p95 is the nearest-rank value (ceil(0.95 N)-th of the sorted times).
"""

import math
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def once(path):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=300) as r:
            size, code = len(r.read()), r.status
    except urllib.error.HTTPError as e:
        size, code = len(e.read()), e.code
    return (time.perf_counter() - t) * 1000.0, size, code


def main(argv):
    mode = argv[0]
    if mode == "cold":
        for p in argv[1:]:
            ms, size, code = once(p)
            print("http cold  %-48s %8.0f ms  %9d B  HTTP %d" % (p, ms, size, code))
        return
    n = int(argv[1])
    for p in argv[2:]:
        once(p)
        runs = [once(p) for _ in range(n)]
        xs = sorted(r[0] for r in runs)
        p95 = xs[max(0, math.ceil(0.95 * n) - 1)]
        codes = sorted({r[2] for r in runs})
        print("http warm  %-48s p50 %6.0f  p95 %6.0f  max %6.0f ms  %9d B  HTTP %s  (n=%d)"
              % (p, xs[n // 2], p95, xs[-1], runs[-1][1], ",".join(map(str, codes)), n))


if __name__ == "__main__":
    main(sys.argv[1:])
