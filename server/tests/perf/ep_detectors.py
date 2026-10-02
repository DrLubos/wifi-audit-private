"""Print EXPLAIN (ANALYZE, BUFFERS) statements for the detectors' time-window
scans of observations, with the SQL taken from the detector modules
themselves (stdlib only; the modules are imported, not run):

  deauth_flood EVENTS_SQL   - every AP's polls in the window, per-AP window functions
  evil_twin READINGS_SQL    - the baselined APs' valid readings in the window, in
                              (device_key, ts) order (streamed by the detector)

  python3 tests/perf/ep_detectors.py [FROM] [TO] | (cat tests/perf/common.sql -) | psql ...

Default window: 24 h, 2026-09-25 00:00 .. 2026-09-26 00:00 UTC (the detectors'
default span). Each statement runs twice: the second is the warm one.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import deauth_flood, evil_twin  # noqa: E402


def bind(sql, params):
    def sub(m):
        return params[m.group(1)]
    return re.sub(r"%\((\w+)\)s", sub, sql)


def main(argv):
    frm = argv[0] if argv else "2026-09-25 00:00:00+00"
    to = argv[1] if len(argv) > 1 else "2026-09-26 00:00:00+00"
    p = {"sid": ":sid", "scan_from": "timestamptz '%s'" % frm, "scan_to": "timestamptz '%s'" % to,
         "max_gap_s": "120"}
    for name, sql in (("deauth_flood EVENTS_SQL", deauth_flood.EVENTS_SQL),
                      ("evil_twin READINGS_SQL", evil_twin.READINGS_SQL)):
        stmt = bind(sql.strip(), p)
        for run in ("first", "warm"):
            print("\\echo == %s, %s .. %s (%s run)" % (name, frm, to, run))
            print("EXPLAIN (ANALYZE, BUFFERS)\n%s;" % stmt)


if __name__ == "__main__":
    main(sys.argv[1:])
