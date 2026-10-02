#!/usr/bin/env python3
"""Compare two API snapshots (api_snapshot.sh) field by field, e.g. the answers
of schema 5 (raw observations) and schema 6 (read tables) for the same data.

  python3 tests/perf/compare_api.py tests/perf/out/schema5 tests/perf/out/schema6
  python3 tests/perf/compare_api.py OLD NEW --ignore baseline.main_freq_khz,observations

--ignore: checks (by label) whose difference is intended, reported as ignored.

Prints one line per check (before / after / verdict) and exits 1 on any
difference. Floats compare with a tolerance of 0.01 (real columns are float4:
2.4 comes back as 2.4000000953674316 from Python and as 2.4 from PostgreSQL's
JSON). Keys a schema added (e.g. p10/p90, ap_observations) are listed, not
compared; keys it removed are differences. Standard library only.
"""

import json
import math
import os
import sys
from datetime import datetime

TOL = 0.01


def load(d, name):
    with open(os.path.join(d, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def norm(v):
    """ISO timestamps -> aware datetimes, so '...Z' and '...+00:00' compare equal."""
    if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and v[10] in "T ":
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return v


def same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, abs_tol=TOL)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return norm(a) == norm(b)


class Report:
    def __init__(self, ignore=()):
        self.failed = 0
        self.ignore = tuple(ignore)

    def check(self, label, a, b):
        if label.split(" ")[0] in self.ignore:
            print("  %-52s (ignored: an intended change)" % label[:52])
            return
        ok = same(a, b)
        self.failed += not ok
        show = lambda v: (json.dumps(v, ensure_ascii=False)[:70])
        print("  %-52s %-26s %-26s %s" % (label[:52], show(a)[:26], show(b)[:26], "ok" if ok else "DIFF"))

    def section(self, title):
        print("\n== " + title)
        print("  %-52s %-26s %-26s %s" % ("check", "before", "after", ""))

    def keys(self, label, a, b):
        added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
        if added:
            print("  %s: new keys %s" % (label, ", ".join(added)))
        for k in removed:
            self.check("%s.%s (removed)" % (label, k), a[k], None)


def series_check(r, label, a, b):
    """Columnar RSSI series: same buckets, same values bucket by bucket."""
    r.check(label + ": buckets", len(a["t"]), len(b["t"]))
    r.check(label + ": bucket times", a["t"], b["t"])
    for k in ("n", "n_floor", "median", "min", "max"):
        r.check(label + ": " + k + " (all buckets)", a[k], b[k])
    r.check(label + ": sum n", sum(x or 0 for x in a["n"]), sum(x or 0 for x in b["n"]))
    r.check(label + ": sum n_floor", sum(x or 0 for x in a["n_floor"]), sum(x or 0 for x in b["n_floor"]))
    r.check(label + ": baseline", a["baseline"], b["baseline"])


def main(argv):
    before, after = argv[0], argv[1]
    ignore = []
    if "--ignore" in argv:
        ignore = argv[argv.index("--ignore") + 1].split(",")
    keys = sorted(f[3:-5] for f in os.listdir(before) if f.startswith("ap_") and f.endswith(".json"))
    r = Report(ignore)

    for k in keys:
        a, b = load(before, "ap_" + k), load(after, "ap_" + k)
        r.section("AP %s (%s)" % (k, a["ap"].get("ssid") or a["ap"].get("name_seen") or "hidden"))
        for f in ("n_obs", "n_rssi", "n_floor", "first_obs", "last_obs"):
            r.check("observations." + f, a["observations"][f], b["observations"][f])
        r.check("config changes (events)", a["config_history"]["total"], b["config_history"]["total"])
        r.check("config history raw rows", a["config_history"]["raw_rows"], b["config_history"]["raw_rows"])
        ca = [(x["ts"], sorted((c["field"], c["old"], c["new"]) for c in x["changes"]))
              for x in a["config_history"]["rows"]]
        cb = [(x["ts"], sorted((c["field"], c["old"], c["new"]) for c in x["changes"]))
              for x in b["config_history"]["rows"]]
        r.check("config change rows (ts + fields)", ca, cb)
        r.keys("ap", a["ap"], b["ap"])
        for f in sorted(set(a["ap"]) & set(b["ap"])):
            r.check("ap." + f, a["ap"][f], b["ap"][f])
        r.check("baseline present", a["baseline"] is not None, b["baseline"] is not None)
        if a["baseline"] and b["baseline"]:
            for f in sorted(a["baseline"]):
                r.check("baseline." + f, a["baseline"][f], b["baseline"].get(f))
        series_check(r, "rssi hourly", load(before, "rssi_hourly_" + k), load(after, "rssi_hourly_" + k))
        series_check(r, "rssi 15 min, 48 h", load(before, "rssi_raw_" + k), load(after, "rssi_raw_" + k))

    a, b = load(before, "overview"), load(after, "overview")
    r.section("overview")
    r.keys("overview", a, b)
    for f in ("capture", "polls", "devices", "observations", "alerts", "baselines", "detections"):
        r.check(f, a[f], b[f])

    a, b = load(before, "aps"), load(after, "aps")
    r.section("AP list")
    r.check("count", a["count"], b["count"])
    ia, ib = {x["device_key"]: x for x in a["aps"]}, {x["device_key"]: x for x in b["aps"]}
    r.check("same device keys", sorted(ia), sorted(ib))
    diff = [(k, f) for k in ia if k in ib for f in ia[k] if not same(ia[k][f], ib[k].get(f))]
    r.check("fields differing (of %d x %d)" % (len(ia), len(next(iter(ia.values()), {}))),
            0, len(diff))
    for k, f in diff[:10]:
        r.check("  %s.%s" % (k[-12:], f), ia[k][f], ib[k].get(f))
    r.check("order (last_seen desc)", [x["last_seen"] for x in a["aps"]], [x["last_seen"] for x in b["aps"]])

    a, b = load(before, "detections"), load(after, "detections")
    r.section("detections list")
    for f in ("total", "open", "by_severity", "by_type", "count"):
        r.check(f, a[f], b[f])
    strip = lambda rows: [{k: v for k, v in x.items() if k != "evidence"} for x in rows]
    r.check("rows without evidence", strip(a["detections"]), strip(b["detections"]))

    print("\n%s: %d difference(s)" % ("FAIL" if r.failed else "PASS", r.failed))
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
