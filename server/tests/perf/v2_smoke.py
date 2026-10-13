"""Smoke test of the /api/v2 endpoints, run INSIDE the api container after a
deploy (stdlib + the deployed app.v2.privacy; measure.sh MODE=v2 pipes it into
`docker compose exec -T api python - <BSSID,BSSID,...>`). Read-only GETs, and
writes only without (or with a wrong) token, which the api refuses before it
reads the body - nothing is written:

- every endpoint answers 200 with JSON (and the documented 404 / 422 cases);
- every write endpoint refuses a request without the token (401, or 403 when
  WRITE_TOKEN is not set);
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


def send(method, path, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=b"{}", method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


WRITES = (("POST", "/auth/check"), ("PUT", "/settings/networks"), ("POST", "/settings/networks"),
          ("POST", "/settings/networks/1/allowed"), ("PUT", "/settings/classification"),
          ("PUT", "/settings/detectors"), ("POST", "/settings/sensors"), ("PUT", "/settings/sensors/pi-fri"),
          ("POST", "/settings/hardware"), ("PUT", "/settings/hardware/1"), ("DELETE", "/settings/hardware/1"),
          ("POST", "/settings/sensors/pi-fri/hardware"), ("POST", "/events/1/status"), ("POST", "/events/status"),
          ("PUT", "/aps/EC:58:EA:55:88:FC/classification"), ("PUT", "/aps/EC:58:EA:55:88:FC/note"),
          ("POST", "/settings/sensors/new"), ("DELETE", "/settings/sensors/pi-fri?confirm=pi-fri"))


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
    # the feed's rows (single events and groups) hold every event exactly once
    ids, page, n = [], feed, 0
    while page and n < 50:
        ids += [i for r in page["items"] for i in ([m["id"] for m in r["group"]["members"]] if r.get("group")
                                                    else [r["id"]])]
        page = check("/events?limit=100&before=" + page["older"]) if page["older"] else None
        n += 1
    if feed and (len(ids) != len(set(ids)) or len(ids) != feed["counts"]["total"]):
        failures.append("events: %d events over the pages, %d distinct, %d counted"
                        % (len(ids), len(set(ids)), feed["counts"]["total"]))
        print("FAIL events rows do not hold every event once")
    check("/events?type=detection&limit=100")
    check("/events?severity=medium&status=open&limit=100")
    check("/events?severity=urgent", 422)
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
    for t in ("networks", "classification", "detectors", "sensors", "hardware"):
        check("/settings/%s" % t)
    for method, path in WRITES:
        for token in (None, "not-the-write-token-0000"):
            code = send(method, path, token)
            ok = code in (401, 403)
            print("%-4s %3d %s %s (%s token)" % ("ok" if ok else "FAIL", code, method, path, "a wrong" if token else "no"))
            if not ok:
                failures.append("%s %s accepted without the token (%d)" % (method, path, code))
    check("/overview/period?range=90d", 422)
    check("/overview/period?sensor=no-such-sensor", 422)
    print("%d failure(s)" % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
