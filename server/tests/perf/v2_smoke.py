"""Smoke test of the /api/v2 endpoints, run INSIDE the api container after a
deploy (stdlib + the deployed app.v2.privacy; measure.sh MODE=v2 pipes it into
`docker compose exec -T api python - <BSSID,BSSID,...>`). Read-only GETs:

- every endpoint answers 200 with JSON (and the documented 404 / 422 cases);
- the ETag round trip: the same request with If-None-Match is a 304;
- the privacy scan: every MAC in every response is a BSSID of the account's
  APs (the comma-separated argument; measure.sh reads it as claude_ro).

Exit status 1 on any failure.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/app")
from app.v2 import privacy  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v2"


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def main(argv):
    bssids = set(argv[0].split(",")) if argv else set()
    failures = []

    def check(path, want=200):
        code, headers, body = get(path)
        ok = code == want
        note = ""
        if code == 200:
            data = json.loads(body)
            foreign = privacy.foreign_macs(data, bssids)
            if foreign:
                ok = False
                note = "foreign MACs %s" % sorted(foreign)[:3]
            tag = headers.get("ETag")
            if tag:
                c304, _, _ = get(path, {"If-None-Match": tag})
                if c304 != 304:
                    ok = False
                    note += " If-None-Match -> %d" % c304
            else:
                ok = False
                note += " no ETag"
        print("%-4s %3d %8d B  %s %s" % ("ok" if ok else "FAIL", code, len(body), path, note))
        if not ok:
            failures.append(path)
        return json.loads(body) if code == 200 else None

    for r in ("24h", "7d", "30d"):
        check("/overview/period?range=%s" % r)
        check("/networks?range=%s" % r)
        check("/networks/timeseries?range=%s" % r)
        check("/aps?range=%s&hide_hotspots=false&page_size=100" % r)
        check("/spectrum?range=%s&band=2.4" % r)
        check("/spectrum?range=%s&band=5" % r)
        check("/spectrum/utilization?range=%s&band=2.4&view=timeline" % r)
    check("/sensors")
    check("/overview/status")
    check("/clients/heatmap?range=7d&view=days")
    check("/clients/heatmap?range=30d&view=week")
    check("/clients/heatmap?range=24h&view=week", 422)
    check("/spectrum/utilization?range=7d&band=5&view=day")
    feed = check("/events?limit=100")
    if feed and feed["older"]:
        older = check("/events?limit=100&before=" + feed["older"])
        if older and older["newer"]:
            back = check("/events?limit=100&after=" + older["newer"])
            if back and [i["id"] for i in back["items"]] != [i["id"] for i in feed["items"]]:
                failures.append("events: paging back did not return the first page")
                print("FAIL events paging back")
    check("/events?type=detection&limit=100")
    check("/events?status=closed", 422)
    check("/events?before=garbage", 422)
    for item in (feed or {}).get("items", [])[:5]:
        check("/events/%d" % item["id"])
    for d in range(1, 9):
        check("/events/%d" % d)                      # the detector rows (client MACs in their evidence)
    check("/events/999999999", 404)
    aps = check("/aps?range=30d&sort=-clients&page_size=5")
    for a in (aps or {}).get("items", []):
        b = urllib.parse.quote(a["bssid"])
        check("/aps/%s" % b)
        check("/aps/%s/rssi?days=7" % b)
        check("/aps/%s/load?view=util&range=7d" % b)
        check("/aps/%s/load?view=clients_week&weeks=4" % b)
    check("/aps/00:00:5E:00:53:00", 404)
    check("/aps/not-a-mac", 422)
    sensors = check("/sensors")
    for s in (sensors or {}).get("sensors", []):
        check("/sensors/%s/coverage?days=14" % urllib.parse.quote(s["name"]))
        check("/sensors/%s/outages" % urllib.parse.quote(s["name"]))
    check("/sensors/no-such-sensor/outages", 404)
    for t in ("networks", "classification", "detectors", "sensors"):
        check("/settings/%s" % t)
    check("/overview/period?range=90d", 422)
    check("/overview/period?sensor=no-such-sensor", 422)
    print("%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
