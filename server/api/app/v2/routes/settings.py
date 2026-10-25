"""/api/v2/settings - the stored configuration (Settings and SettingsSensors
boards) and, from phase 5, its writes (PLAN.md 7.5; the write token,
deps.require_write; one transaction with audit_log rows and
accounts.write_version + 1, deps.write_tx).

Reads: networks, classification, detectors, sensors (capture
configuration versions, details, the adapter, the history with its data
boundaries).
Writes: the networks (the whole list from the Networks tab; one new network;
"Add to allowed"), the classification rules and the detector parameters -
each followed by refresh_events() for every sensor of the account -, a
sensor's capture configuration (a new version, not delivered: Q10), its
name / location / description (add, edit, rename, guarded delete), and a
hardware change of a sensor (a dated history entry that is a data boundary:
RSSI baselines are learned inside one hardware period, schema 11 / 13).
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from psycopg import errors

from .. import deps, queries, shape, writes
from ..periods import iso

router = APIRouter(prefix="/settings", tags=["v2 settings"])

_Q = queries.load("settings")
_QW = queries.load("writes")

CLASSIFICATION = ("ignore_random_bssid", "suspicious_unallowed", "managed_by_vendor", "ignore_weaker_dbm",
                  "mobile_vendors")
DETECTORS = ("deauth_min_bursts", "deauth_window_s", "evil_twin_k", "evil_twin_min_db", "evil_twin_guard_s",
             "sensor_offline_min")
NETWORK_FIELDS = ("ssid", "kind", "expected_security", "require_mfp", "allowed_bssids", "allowed_ouis", "note")


def _read(request, acc, build):
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        body = build(conn)
    return deps.respond(body, tag)


def _networks(conn, acc):
    rows = deps.run(conn, _Q, "networks", account_id=acc["id"])
    return [dict(r, created_at=iso(r["created_at"]), updated_at=iso(r["updated_at"])) for r in rows]


def _counts(refreshed):
    return {name: dict(r) for name, r in refreshed.items()}


@router.get("/networks")
def networks(request: Request, acc=Depends(deps.get_account)):
    def build(conn):
        vendors = {r["oui"]: r["manuf"] for r in deps.run(conn, _Q, "oui_vendors", account_id=acc["id"])}
        return {"networks": _networks(conn, acc), "vendors": vendors}
    return _read(request, acc, build)


def _network_params(acc, n):
    return {"account_id": acc["id"], "ssid": n["ssid"], "kind": n["kind"],
            "expected_security": n["expected_security"], "require_mfp": n["require_mfp"],
            "allowed_bssids": n["allowed_bssids"], "allowed_ouis": [o + ":00:00:00" for o in n["allowed_ouis"]],
            "note": n["note"]}


def _stored(n):
    return {k: n[k] for k in NETWORK_FIELDS}


@router.put("/networks", dependencies=[Depends(deps.require_write)])
def put_networks(body: dict = Body(...), acc=Depends(deps.get_account)):
    """The whole list of the Networks tab: an id updates that network, none
    adds one, a stored network left out is removed. Then the events are
    re-derived (the classification and the rule event read the networks)."""
    rows = deps.invalid(writes.parse_networks, body)
    try:
        with deps.write_tx(acc) as (conn, audit):
            old = {r["id"]: r for r in _networks(conn, acc)}
            unknown = [n["id"] for n in rows if n["id"] is not None and n["id"] not in old]
            if unknown:
                raise HTTPException(status_code=404, detail="unknown network id(s) %s" % unknown)
            keep = {n["id"] for n in rows if n["id"] is not None}
            for nid in sorted(set(old) - keep):            # deletes first: a name can move to a new row
                deps.run(conn, _QW, "network_delete", account_id=acc["id"], id=nid)
                audit("network.delete", "network:%d" % nid, _stored(old[nid]), None)
            for n in rows:
                if n["id"] is None:
                    continue
                if _stored(old[n["id"]]) != _stored(n):
                    deps.run(conn, _QW, "network_update", id=n["id"], **_network_params(acc, n))
                    audit("network.update", "network:%d" % n["id"], _stored(old[n["id"]]), _stored(n))
            for n in rows:
                if n["id"] is None:
                    nid = deps.run(conn, _QW, "network_insert", **_network_params(acc, n))[0]["id"]
                    audit("network.create", "network:%d" % nid, None, _stored(n))
            refreshed = deps.refresh_events_all(conn, acc) if audit.rows else {}
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="an SSID can be one network only") from None
    with deps.connection() as conn:
        return deps.written({"networks": _networks(conn, acc), "refreshed": _counts(refreshed)})


@router.post("/networks", status_code=201, dependencies=[Depends(deps.require_write)])
def post_network(body: dict = Body(...), acc=Depends(deps.get_account)):
    """One new network (the "Managed" / "Friendly" action of an AP)."""
    n = deps.invalid(writes.parse_network, body)
    try:
        with deps.write_tx(acc) as (conn, audit):
            nid = deps.run(conn, _QW, "network_insert", **_network_params(acc, n))[0]["id"]
            audit("network.create", "network:%d" % nid, None, _stored(n))
            refreshed = deps.refresh_events_all(conn, acc)
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="%r is a network already" % n["ssid"]) from None
    with deps.connection() as conn:
        net = next(x for x in _networks(conn, acc) if x["id"] == nid)
    return deps.written({"network": net, "refreshed": _counts(refreshed)}, 201)


@router.post("/networks/{network_id}/allowed", dependencies=[Depends(deps.require_write)])
def post_allowed(network_id: int = Path(ge=1, le=2 ** 63 - 1), body: dict = Body(...),
                 acc=Depends(deps.get_account)):
    """"Add to allowed BSSIDs" (or an OUI) of a network."""
    what, value = deps.invalid(writes.parse_allowed, body)
    with deps.write_tx(acc) as (conn, audit):
        old = next((x for x in _networks(conn, acc) if x["id"] == network_id), None)
        if old is None:
            raise HTTPException(status_code=404, detail="unknown network")
        stmt = "network_allow_bssid" if what == "bssid" else "network_allow_oui"
        deps.run(conn, _QW, stmt, account_id=acc["id"], id=network_id,
                 value=value if what == "bssid" else value + ":00:00:00")
        audit("network.allow", "network:%d" % network_id, {what: None}, {what: value})
        refreshed = deps.refresh_events_all(conn, acc)
    with deps.connection() as conn:
        net = next(x for x in _networks(conn, acc) if x["id"] == network_id)
    return deps.written({"network": net, "refreshed": _counts(refreshed)})


def _account_settings(conn, acc, keys):
    st = deps.run(conn, _Q, "account_settings", account_id=acc["id"])[0]
    return dict({k: st[k] for k in keys}, updated_at=iso(st["updated_at"]))


def _put_settings(acc, body, parse, keys, stmt, action):
    new = deps.invalid(parse, body)
    with deps.write_tx(acc) as (conn, audit):
        old = _account_settings(conn, acc, keys)
        changed = {k: new[k] for k in keys if old[k] != new[k]}
        refreshed = {}
        if changed:
            deps.run(conn, _QW, stmt, account_id=acc["id"], **new)
            audit(action, "account:%d" % acc["id"], {k: old[k] for k in changed}, changed)
            refreshed = deps.refresh_events_all(conn, acc)
    with deps.connection() as conn:
        return deps.written(dict(_account_settings(conn, acc, keys), refreshed=_counts(refreshed)))


@router.get("/classification")
def classification(request: Request, acc=Depends(deps.get_account)):
    def build(conn):
        out = _account_settings(conn, acc, CLASSIFICATION)
        out["affected"] = deps.run(conn, _Q, "classification_affected", account_id=acc["id"])[0]
        return out
    return _read(request, acc, build)


@router.put("/classification", dependencies=[Depends(deps.require_write)])
def put_classification(body: dict = Body(...), acc=Depends(deps.get_account)):
    """The classification rules; the events are re-derived (the rule event)."""
    return _put_settings(acc, body, writes.parse_classification, CLASSIFICATION, "classification_update",
                         "settings.classification")


@router.get("/detectors")
def detectors(request: Request, acc=Depends(deps.get_account)):
    return _read(request, acc, lambda conn: _account_settings(conn, acc, DETECTORS))


@router.put("/detectors", dependencies=[Depends(deps.require_write)])
def put_detectors(body: dict = Body(...), acc=Depends(deps.get_account)):
    """The detector parameters. The sensor-offline minutes apply at once (the
    sensor events are re-derived); the batch detectors still take their
    command-line options (they read these in a later step)."""
    return _put_settings(acc, body, writes.parse_detectors, DETECTORS, "detectors_update", "settings.detectors")


@router.get("/sensors")
def sensors(request: Request, acc=Depends(deps.get_account)):
    """Per sensor the applied (confirmed) configuration, a pending one (newer,
    not confirmed - delivery to the sensor does not exist yet, Q10), every
    version, its details and its adapter (read-only: seeded now, reported by
    the sensor in the live-upload phase); the documented changes, the
    hardware changes entered by hand among them, with their data
    boundaries."""
    def build(conn):
        versions = deps.run(conn, _Q, "sensor_config", account_id=acc["id"])
        history = deps.run(conn, _Q, "sensor_history", account_id=acc["id"])
        meta = {m["name"]: m for m in deps.run(conn, _Q, "sensor_meta", account_id=acc["id"])}
        adapters = {a["sensor"]: a for a in deps.run(conn, _Q, "sensor_adapters", account_id=acc["id"])}
        delivery = {d["sensor"]: d for d in deps.run(conn, _Q, "sensor_delivery", account_id=acc["id"])}
        out = []
        for s in acc["sensors"]:
            mine = [dict(v, created_at=iso(v["created_at"]), confirmed_at=iso(v["confirmed_at"]),
                         delivered_at=iso(v["delivered_at"]), refused_at=iso(v["refused_at"]))
                    for v in versions if v["sensor"] == s["name"]]
            current = next((v for v in mine if v["confirmed_at"]), None)
            pending = mine[0] if mine and not mine[0]["confirmed_at"] else None
            m = meta[s["name"]]
            ad = adapters.get(s["name"])
            dl = delivery.get(s["name"]) or {}
            attempt = dl.get("attempt")
            out.append({"name": s["name"], "current": current, "pending": pending, "versions": mine,
                        "delivery": {"accepting": dl.get("accepting"), "capabilities_at": iso(dl.get("capabilities_at")),
                                     "attempt": None if attempt is None else dict(
                                         attempt, reported_at=iso(shape.parse_ts(attempt["reported_at"])))},
                        "meta": {"location": m["location"], "description": m["description"],
                                 "hardware": m["hardware"], "has_polls": s["first_poll"] is not None},
                        "adapter": None if ad is None else dict(
                            {k: ad[k] for k in ("name", "usb_id", "chipset", "driver", "interface", "bands", "reported_by")},
                            # the adapter's MAC stays in the database: no MAC but an account AP's
                            # BSSID in a response (the privacy scan), the page only needs to know it
                            mac_known=ad["mac"] is not None)})
        plans = {s["current"]["channel_plan"] for s in out if s["current"]}
        changes = [dict(h, ts=iso(h["ts"])) for h in history]
        return {"sensors": out, "mixed_plans": len(plans) > 1, "history": changes,
                "plans": {"all_89": writes.ALL_89, "ht20_37": list(writes.HT20_37), "lock": list(writes.HT20_ALL)}}
    return _read(request, acc, build)


def _sensor_of(acc, name):
    s = next((x for x in acc["sensors"] if x["name"] == name), None)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown sensor %r" % name)
    return s


@router.post("/sensors", dependencies=[Depends(deps.require_write)])
def post_sensor_config(body: dict = Body(...), acc=Depends(deps.get_account)):
    """One new capture-configuration version for every selected sensor
    (docs/config-delivery.md): checked against what each sensor reported it
    can tune (capabilities), stored with its hash, and delivered in the
    responses to the sensor's uploads once it accepts configuration
    (CONFIG_APPLY=1). retry: true re-offers a refused configuration."""
    names, config = deps.invalid(writes.parse_capture_config, body)
    chosen = [_sensor_of(acc, n) for n in names]
    sha = writes.config_sha256(config)
    out = []
    with deps.write_tx(acc) as (conn, audit):
        for s in chosen:
            caps = deps.run(conn, _Q, "sensor_capabilities", sensor_id=s["id"])
            caps = caps[0]["capabilities"] if caps else None
            deps.invalid(lambda _: writes.check_capture_config(config["channels"], config["dwell_s"],
                                                                config["poll_s"], caps), None)
            version = deps.run(conn, _QW, "sensor_config_insert", sensor_id=s["id"], sha256=sha,
                               **config)[0]["version"]
            audit("sensor.config", "sensor:%s" % s["name"], None, dict(config, version=version, sha256=sha))
            out.append({"name": s["name"], "version": version})
    return deps.written({"sensors": out, "delivered": False, "sha256": sha})


def _sensor_data(conn, sensor_id):
    """The tables that hold a row of the sensor (its configuration aside)."""
    return [r["table_name"] for r in deps.run(conn, _Q, "sensor_data_tables", sensor_id=sensor_id) if r["has_rows"]]


def _sensor_row(conn, acc, name):
    s = _sensor_of(acc, name)
    m = next(x for x in deps.run(conn, _Q, "sensor_meta", account_id=acc["id"]) if x["id"] == s["id"])
    return s, m


@router.post("/sensors/new", status_code=201, dependencies=[Depends(deps.require_write)])
def post_sensor(body: dict = Body(...), acc=Depends(deps.get_account)):
    """A new, empty sensor (name, location, description): it shows up in the
    selectors and lists with their empty states; data arrives with an import
    under that name (seed/import_snapshot.py --sensor NAME)."""
    new = deps.invalid(writes.parse_sensor_meta, body, True)
    try:
        with deps.write_tx(acc) as (conn, audit):
            sid = deps.run(conn, _QW, "sensor_insert", account_id=acc["id"], tz=acc["tz"], **new)[0]["id"]
            audit("sensor.add", "sensor:%s" % new["name"], None, dict(new, id=sid))
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="a sensor named %r exists already" % new["name"]) from None
    return deps.written(dict(new, id=sid), 201)


@router.put("/sensors/{name}", dependencies=[Depends(deps.require_write)])
def put_sensor_meta(name: str = Path(max_length=64), body: dict = Body(...), acc=Depends(deps.get_account)):
    """A sensor's name, location and description. A rename changes only the
    label (sensor_id is the key); the importer files data under the name, so
    its --sensor option must follow (the response says so)."""
    new = deps.invalid(writes.parse_sensor_meta, body)
    try:
        with deps.write_tx(acc) as (conn, audit):
            s, m = _sensor_row(conn, acc, name)
            old = {"name": m["name"], "location": m["location"], "description": m["description"]}
            new = dict(new, name=new.get("name", name))
            if old != new:
                deps.run(conn, _QW, "sensor_update", account_id=acc["id"], id=s["id"], **new)
                if new["name"] != name:
                    audit("sensor.rename", "sensor:%s" % new["name"], {"id": s["id"], "name": name},
                          {"id": s["id"], "name": new["name"]})
                if (old["location"], old["description"]) != (new["location"], new["description"]):
                    audit("sensor.meta", "sensor:%s" % new["name"],
                          {k: old[k] for k in ("location", "description")},
                          {k: new[k] for k in ("location", "description")})
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="a sensor named %r exists already" % new["name"]) from None
    return deps.written(dict(new, renamed=new["name"] != name))


@router.delete("/sensors/{name}", status_code=204, dependencies=[Depends(deps.require_write)])
def delete_sensor(name: str = Path(max_length=64), confirm: str = Query("", max_length=64),
                  acc=Depends(deps.get_account)):
    """Delete a sensor - never one with data, never unconfirmed: the sensor's
    name must be typed (?confirm=NAME) and no poll, device, observation,
    alert, event or rollup row may belong to it (409 names the tables); only
    its configuration (adapter record, capture configurations, history, and
    its ingest tokens - revoked by the deletion, live upload amendment 4) goes
    with it, and the audit row keeps what was there (the token ids)."""
    s = _sensor_of(acc, name)
    if confirm != name:
        raise HTTPException(status_code=422, detail="type the sensor's name to confirm the deletion")
    with deps.write_tx(acc) as (conn, audit):
        m = next(x for x in deps.run(conn, _Q, "sensor_meta", account_id=acc["id"]) if x["id"] == s["id"])
        data = _sensor_data(conn, s["id"])
        if data:
            raise HTTPException(status_code=409, detail="%s has data (%s) - a sensor with data is never deleted"
                                % (name, ", ".join(data)))
        gone = deps.run(conn, _QW, "sensor_delete_config", id=s["id"])[0]
        deps.run(conn, _QW, "sensor_delete", account_id=acc["id"], id=s["id"])
        audit("sensor.delete", "sensor:%s" % name,
              {"id": s["id"], "name": name, "location": m["location"], "description": m["description"],
               "configuration_removed": dict(gone)}, None)
    return deps.written(None, 204)


@router.post("/sensors/{name}/hardware-change", status_code=201, dependencies=[Depends(deps.require_write)])
def post_hardware_change(name: str = Path(max_length=64), body: dict = Body(...), acc=Depends(deps.get_account)):
    """A hardware change of a sensor, entered by hand ({valid_from?, note?}: the
    antenna swapped, the sensor moved - anything that shifts every RSSI): a
    dated entry of the configuration history that is a data boundary. RSSI
    baselines are learned inside one hardware period (refresh_ap_baselines(),
    schema 11), so right after one evil twin (b) has no baseline until the new
    hardware has enough readings; the next import recomputes them."""
    s = _sensor_of(acc, name)
    a = deps.invalid(writes.parse_hardware_change, body, shape.utc_now())
    label = writes.hardware_change_label(a["note"])
    try:
        with deps.write_tx(acc) as (conn, audit):
            deps.run(conn, _QW, "hardware_change_insert", sensor_id=s["id"], valid_from=a["valid_from"], label=label)
            audit("sensor.hardware_change", "sensor:%s" % name, None,
                  {"id": s["id"], "valid_from": iso(a["valid_from"]), "note": a["note"]})
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="%s has a hardware change at that very time already" % name) from None
    return deps.written({"sensor": name, "valid_from": iso(a["valid_from"]), "label": label}, 201)
