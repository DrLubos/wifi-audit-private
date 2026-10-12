"""/api/v2/settings - the stored configuration (Settings and SettingsSensors
boards) and, from phase 5, its writes (PLAN.md 7.5; the write token,
deps.require_write; one transaction with audit_log rows and
accounts.write_version + 1, deps.write_tx).

Reads: networks, classification, detectors, sensors (capture
configuration versions, details, attached hardware, the history with its
data boundaries), hardware (the catalog).
Writes: the networks (the whole list from the Networks tab; one new network;
"Add to allowed"), the classification rules and the detector parameters -
each followed by refresh_events() for every sensor of the account -, a
sensor's capture configuration (a new version, not delivered: Q10), its
location / description, the hardware catalog, and a hardware change of a
sensor (a data boundary: RSSI baselines are learned inside one hardware
period, schema 11).
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
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
HARDWARE_FIELDS = ("kind", "name", "gain_dbi", "bands", "connector", "note")


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


def _hardware(conn, acc):
    return [dict(r, created_at=iso(r["created_at"])) for r in deps.run(conn, _Q, "hardware", account_id=acc["id"])]


def _hardware_text(item):
    if item is None:
        return "none recorded"
    gain = "" if item["gain_dbi"] is None else " (%g dBi)" % item["gain_dbi"]
    return item["name"] + gain


@router.get("/sensors")
def sensors(request: Request, acc=Depends(deps.get_account)):
    """Per sensor the applied (confirmed) configuration, a pending one (newer,
    not confirmed - delivery to the sensor does not exist yet, Q10), every
    version, its details and its capture hardware (current and history); the
    catalog; the documented changes and the hardware changes with their data
    boundaries."""
    def build(conn):
        versions = deps.run(conn, _Q, "sensor_config", account_id=acc["id"])
        history = deps.run(conn, _Q, "sensor_history", account_id=acc["id"])
        meta = {m["name"]: m for m in deps.run(conn, _Q, "sensor_meta", account_id=acc["id"])}
        catalog = _hardware(conn, acc)
        by_id = {h["id"]: h for h in catalog}
        attached = deps.run(conn, _Q, "sensor_hardware", account_id=acc["id"])
        out = []
        for s in acc["sensors"]:
            mine = [dict(v, created_at=iso(v["created_at"]), confirmed_at=iso(v["confirmed_at"]))
                    for v in versions if v["sensor"] == s["name"]]
            current = next((v for v in mine if v["confirmed_at"]), None)
            pending = mine[0] if mine and not mine[0]["confirmed_at"] else None
            hw = [{"valid_from": iso(x["valid_from"]), "adapter_id": x["adapter_id"], "antenna_id": x["antenna_id"],
                   "created_by": x["created_by"], "note": x["note"]}
                  for x in attached if x["sensor"] == s["name"]]
            m = meta[s["name"]]
            out.append({"name": s["name"], "current": current, "pending": pending, "versions": mine,
                        "meta": {"location": m["location"], "description": m["description"],
                                 "hardware": m["hardware"]},
                        "capture": hw[0] if hw else None, "capture_history": hw})
        plans = {s["current"]["channel_plan"] for s in out if s["current"]}
        changes = [dict(h, ts=iso(h["ts"]), kind="change") for h in history]
        for x in attached:
            changes.append({"sensor": x["sensor"], "ts": iso(x["valid_from"]), "boundary": True, "kind": "hardware",
                            "label": "Capture hardware: %s + %s" % (_hardware_text(by_id.get(x["adapter_id"])),
                                                                    _hardware_text(by_id.get(x["antenna_id"]))),
                            "ref": None})
        changes.sort(key=lambda h: h["ts"], reverse=True)
        return {"sensors": out, "mixed_plans": len(plans) > 1, "catalog": catalog, "history": changes,
                "plans": {"all_89": writes.ALL_89, "ht20_37": list(writes.HT20_37), "lock": list(writes.HT20_ALL)}}
    return _read(request, acc, build)


def _sensor_of(acc, name):
    s = next((x for x in acc["sensors"] if x["name"] == name), None)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown sensor %r" % name)
    return s


@router.post("/sensors", dependencies=[Depends(deps.require_write)])
def post_sensor_config(body: dict = Body(...), acc=Depends(deps.get_account)):
    """One new capture-configuration version for every selected sensor. Not
    delivered: there is no delivery to the sensor yet (Q10), so it stays
    pending until the sensor upload exists (11.1)."""
    names, config = deps.invalid(writes.parse_capture_config, body)
    chosen = [_sensor_of(acc, n) for n in names]
    out = []
    with deps.write_tx(acc) as (conn, audit):
        for s in chosen:
            version = deps.run(conn, _QW, "sensor_config_insert", sensor_id=s["id"], **config)[0]["version"]
            audit("sensor.config", "sensor:%s" % s["name"], None, dict(config, version=version))
            out.append({"name": s["name"], "version": version})
    return deps.written({"sensors": out, "delivered": False})


@router.put("/sensors/{name}", dependencies=[Depends(deps.require_write)])
def put_sensor_meta(name: str = Path(max_length=64), body: dict = Body(...), acc=Depends(deps.get_account)):
    """A sensor's location and description."""
    new = deps.invalid(writes.parse_sensor_meta, body)
    s = _sensor_of(acc, name)
    with deps.write_tx(acc) as (conn, audit):
        old = next(m for m in deps.run(conn, _Q, "sensor_meta", account_id=acc["id"]) if m["id"] == s["id"])
        old = {k: old[k] for k in new}
        if old != new:
            deps.run(conn, _QW, "sensor_meta_update", account_id=acc["id"], id=s["id"], **new)
            audit("sensor.meta", "sensor:%s" % name, old, new)
    return deps.written(dict(new, name=name))


@router.get("/hardware")
def hardware(request: Request, acc=Depends(deps.get_account)):
    return _read(request, acc, lambda conn: {"hardware": _hardware(conn, acc)})


def _stored_hw(h):
    return {k: h[k] for k in HARDWARE_FIELDS}


@router.post("/hardware", status_code=201, dependencies=[Depends(deps.require_write)])
def post_hardware(body: dict = Body(...), acc=Depends(deps.get_account)):
    """A new catalog row (an adapter or an antenna)."""
    h = deps.invalid(writes.parse_hardware, body)
    try:
        with deps.write_tx(acc) as (conn, audit):
            hid = deps.run(conn, _QW, "hardware_insert", account_id=acc["id"], **h)[0]["id"]
            audit("hardware.create", "hardware:%d" % hid, None, h)
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="the catalog has a %s %r already" % (h["kind"], h["name"])) from None
    with deps.connection() as conn:
        return deps.written({"item": next(x for x in _hardware(conn, acc) if x["id"] == hid)}, 201)


def _catalog_item(conn, acc, hid):
    item = next((x for x in _hardware(conn, acc) if x["id"] == hid), None)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown catalog id")
    return item


@router.put("/hardware/{hid}", dependencies=[Depends(deps.require_write)])
def put_hardware(hid: int = Path(ge=1, le=2 ** 31 - 1), body: dict = Body(...), acc=Depends(deps.get_account)):
    """A catalog row's name, gain, bands, connector or note (its kind stays)."""
    try:
        with deps.write_tx(acc) as (conn, audit):
            old = _catalog_item(conn, acc, hid)
            h = deps.invalid(writes.parse_hardware, body, kind_fixed=old["kind"])
            if _stored_hw(old) != h:
                deps.run(conn, _QW, "hardware_update", account_id=acc["id"], id=hid, **h)
                audit("hardware.update", "hardware:%d" % hid, _stored_hw(old), h)
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="the catalog has that name already") from None
    with deps.connection() as conn:
        return deps.written({"item": _catalog_item(conn, acc, hid)})


@router.delete("/hardware/{hid}", status_code=204, dependencies=[Depends(deps.require_write)])
def delete_hardware(hid: int = Path(ge=1, le=2 ** 31 - 1), acc=Depends(deps.get_account)):
    """An unused catalog row (one a sensor had attached stays: the history names it)."""
    with deps.write_tx(acc) as (conn, audit):
        old = _catalog_item(conn, acc, hid)
        if old["used"]:
            raise HTTPException(status_code=409, detail="in a sensor's hardware history - it stays")
        deps.run(conn, _QW, "hardware_delete", account_id=acc["id"], id=hid)
        audit("hardware.delete", "hardware:%d" % hid, _stored_hw(old), None)
    return deps.written(None, 204)


@router.post("/sensors/{name}/hardware", status_code=201, dependencies=[Depends(deps.require_write)])
def post_sensor_hardware(name: str = Path(max_length=64), body: dict = Body(...), acc=Depends(deps.get_account)):
    """A hardware change of a sensor from valid_from (default now): a data
    boundary. RSSI baselines are learned inside one hardware period
    (refresh_ap_baselines(), schema 11), so right after a change evil_twin (b)
    has no baseline until the new hardware has enough readings; the next
    import recomputes them."""
    s = _sensor_of(acc, name)
    with deps.write_tx(acc) as (conn, audit):
        hist = [x for x in deps.run(conn, _Q, "sensor_hardware", account_id=acc["id"]) if x["sensor"] == name]
        last = hist[0] if hist else None
        a = deps.invalid(writes.parse_attach, body, shape.utc_now(), last["valid_from"] if last else None)
        catalog = {h["id"]: h for h in _hardware(conn, acc)}
        if catalog.get(a["adapter_id"], {}).get("kind") != "adapter":
            raise HTTPException(status_code=422, detail="adapter_id: not an adapter of the catalog")
        if a["antenna_id"] is not None and catalog.get(a["antenna_id"], {}).get("kind") != "antenna":
            raise HTTPException(status_code=422, detail="antenna_id: not an antenna of the catalog")
        if last and (last["adapter_id"], last["antenna_id"]) == (a["adapter_id"], a["antenna_id"]):
            raise HTTPException(status_code=409, detail="that is the hardware attached now")
        deps.run(conn, _QW, "sensor_hardware_insert", sensor_id=s["id"], **a)
        audit("sensor.hardware", "sensor:%s" % name,
              None if last is None else {"adapter_id": last["adapter_id"], "antenna_id": last["antenna_id"],
                                         "valid_from": iso(last["valid_from"])},
              {"adapter_id": a["adapter_id"], "antenna_id": a["antenna_id"], "valid_from": iso(a["valid_from"])})
    return deps.written({"sensor": name, "valid_from": iso(a["valid_from"]), "adapter_id": a["adapter_id"],
                         "antenna_id": a["antenna_id"]}, 201)
