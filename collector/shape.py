"""Shape raw Kismet REST JSON into compact collector records.

The collector asks Kismet for a fixed list of fields (DEVICE_FIELDS) using the
field-simplification syntax of the devices endpoints, and turns each returned
device into one flat dict (see shape_device) that store.py knows how to write.

Kismet quirk handled here: in a field-filtered response a field that does not
exist on a device is serialised as the integer 0, never as null. A client has
no advertised SSID, so it comes back with "ssid": 0, "crypt_string": 0, ...
For signal fields 0 is likewise a "no data" sentinel - a real reading of 0 dBm
does not occur - so every non-counter value of 0 is mapped to None and only true
counters (packet/byte counts) keep a literal 0.

Field names were verified against Kismet 2025.09.0. There are no separate
wpa_version / pairwise-cipher / RSN fields in this version: the advertised
crypto is carried by crypt_string (human readable) and crypt_bitfield (64-bit).

Deliberately NOT requested (bystander PII, not needed for the RF analysis):
dot11.client.ipdata (DHCP/ARP-learned client IPs) and the WPS identity fields
(wps_serial_number, wps_model_name, wps_manuf, wps_device_name).
"""

import hashlib
import json

_BASE = "kismet.device.base."
_DOT11 = "dot11.device/dot11.device."
_ADV = _DOT11 + "last_beaconed_ssid_record/dot11.advertisedssid."
_SIG = _BASE + "signal/kismet.common.signal."

# Field list sent as {"fields": DEVICE_FIELDS} with every device poll.
# Entries are "path" (kept under that name) or ["path", "alias"]. Nested paths
# use "/". Everything under seenby[] is excluded on purpose: it embeds the whole
# datasource object and is by far the largest part of a device record.
DEVICE_FIELDS = [
    _BASE + "key",
    _BASE + "macaddr",
    _BASE + "type",
    _BASE + "manuf",
    _BASE + "channel",
    _BASE + "frequency",
    _BASE + "first_time",
    _BASE + "last_time",
    _BASE + "packets.total",
    _BASE + "packets.tx_total",
    _BASE + "packets.rx_total",
    _BASE + "packets.data",
    _BASE + "datasize",
    _BASE + "num_alerts",
    _BASE + "freq_khz_map",
    [_SIG + "last_signal", "sig_last"],
    [_SIG + "min_signal", "sig_min"],
    [_SIG + "max_signal", "sig_max"],
    # dot11 fields present on every 802.11 device (0 when not applicable)
    [_DOT11 + "last_bssid", "last_bssid"],
    [_DOT11 + "num_associated_clients", "n_clients"],
    [_DOT11 + "client_disconnects", "disconnects"],
    [_DOT11 + "beacon_fingerprint", "beacon_fp"],
    [_DOT11 + "bss_timestamp", "bss_ts"],
    [_DOT11 + "associated_client_map", "clients"],
    [_DOT11 + "probed_ssid_map", "probed"],
    # last beaconed SSID record (APs only; 0 for everything else)
    [_ADV + "ssid", "ssid"],
    [_ADV + "cloaked", "cloaked"],
    [_ADV + "crypt_string", "crypt"],
    [_ADV + "crypt_bitfield", "crypt_bits"],
    [_ADV + "wpa_mfp_supported", "mfp_sup"],
    [_ADV + "wpa_mfp_required", "mfp_req"],
    [_ADV + "channel", "adv_ch"],
    [_ADV + "ht_mode", "ht"],
    [_ADV + "beaconrate", "beacon_rate"],
    [_ADV + "dot11d_country", "country"],
    [_ADV + "ietag_checksum", "ie_sum"],
    [_ADV + "dot11e_qbss_stations", "qbss_stations"],
    [_ADV + "dot11e_channel_utilization_perc", "util_pct"],
]

_TYPE_MAP = {
    "Wi-Fi AP": "ap",
    "Wi-Fi Client": "client",
    "Wi-Fi Bridged": "bridged",
    "Wi-Fi Ad-Hoc": "adhoc",
    "Wi-Fi WDS": "wds",
    "Wi-Fi Device": "device",
}
_NULL_MAC = "00:00:00:00:00:00"


# --- value coercion: Kismet's 0-for-missing sentinel becomes None ------------

def _text(v):
    """String field, or None when absent. "" is kept: it is a real value for
    a cloaked SSID or a wildcard probe request."""
    return v if isinstance(v, str) else None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _counter(v):
    """Cumulative counter: 0 is a legitimate value."""
    n = _num(v)
    return int(n) if n is not None else None


def _positive(v):
    """Numeric field where 0 means unknown (frequency, timestamps, ids)."""
    n = _num(v)
    return n if n is not None and n != 0 else None


def _dbm(v):
    """Signal level in dBm. 0 is Kismet's blank value, never a reading."""
    n = _positive(v)
    return int(n) if n is not None else None


def _flag(v):
    n = _num(v)
    return 1 if n else 0


def _mac(v):
    s = _text(v)
    return s if s and s != _NULL_MAC else None


def _nonempty(v):
    s = _text(v)
    return s if s else None


def device_type(raw_type):
    t = _text(raw_type)
    if t is None:
        return "unknown"
    return _TYPE_MAP.get(t, t.lower().replace("wi-fi ", "").replace(" ", "_"))


# --- device records -----------------------------------------------------------

def shape_device(raw, ts):
    """Turn one field-filtered Kismet device into a compact record.

    Flat keys hold the per-poll time-series values; the "ap" and "cl"
    sub-dicts hold AP configuration and client behaviour and are present only
    when the device actually carries that data.
    """
    key = _nonempty(raw.get(_BASE + "key"))
    mac = _mac(raw.get(_BASE + "macaddr"))
    if key is None or mac is None:
        raise ValueError("device record without key/macaddr: %r" % (raw,))

    rec = {
        "ts": ts,
        "key": key,
        "mac": mac,
        "type": device_type(raw.get(_BASE + "type")),
        "manuf": _nonempty(raw.get(_BASE + "manuf")),
        "first": _positive(raw.get(_BASE + "first_time")),
        "last": _positive(raw.get(_BASE + "last_time")) or ts,
        "freq": _positive(raw.get(_BASE + "frequency")),
        "ch": _nonempty(raw.get(_BASE + "channel")),
        "rssi": _dbm(raw.get("sig_last")),
        "rssi_min": _dbm(raw.get("sig_min")),
        "rssi_max": _dbm(raw.get("sig_max")),
        "pk": _counter(raw.get(_BASE + "packets.total")),
        "tx": _counter(raw.get(_BASE + "packets.tx_total")),
        "rx": _counter(raw.get(_BASE + "packets.rx_total")),
        "data": _counter(raw.get(_BASE + "packets.data")),
        "bytes": _counter(raw.get(_BASE + "datasize")),
        "alerts": _counter(raw.get(_BASE + "num_alerts")),
        "freqs": _freq_map(raw.get(_BASE + "freq_khz_map")),
    }
    ap = _shape_ap(raw, rec["type"])
    if ap:
        rec["ap"] = ap
    cl = _shape_client(raw, mac)
    if cl:
        rec["cl"] = cl
    return rec


def _freq_map(v):
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, n in v.items():
        try:
            f = int(k)
        except (TypeError, ValueError):
            continue
        c = _counter(n)
        if c is not None:
            out[f] = c
    return out


def _shape_ap(raw, dtype):
    """AP block: present when the device has beaconed an SSID (or Kismet
    classifies it as an AP). Inside the block a 0 is a real value."""
    ssid = _text(raw.get("ssid"))
    if ssid is None and dtype != "ap":
        return None
    clients = raw.get("clients")
    return {
        "ssid": ssid,
        "cloaked": _flag(raw.get("cloaked")),
        "crypt": _nonempty(raw.get("crypt")),
        "crypt_bits": _counter(raw.get("crypt_bits")),
        "mfp": [_flag(raw.get("mfp_sup")), _flag(raw.get("mfp_req"))],
        "adv_ch": _nonempty(raw.get("adv_ch")),
        "ht": _nonempty(raw.get("ht")),
        "beacon_rate": _positive(raw.get("beacon_rate")),
        "country": _nonempty(raw.get("country")),
        "ie_sum": _positive(raw.get("ie_sum")),
        "beacon_fp": _positive(raw.get("beacon_fp")),
        "bss_ts": _positive(raw.get("bss_ts")),
        "qbss_stations": _counter(raw.get("qbss_stations")),
        "util_pct": _num(raw.get("util_pct")),
        "n_clients": _counter(raw.get("n_clients")),
        "disconnects": _counter(raw.get("disconnects")),
        "clients": sorted(clients) if isinstance(clients, dict) else [],
    }


def _shape_client(raw, own_mac):
    """Client block: the AP this device last talked to and the SSIDs it
    probed for. An AP's own last_bssid is itself and is not an association."""
    bssid = _mac(raw.get("last_bssid"))
    if bssid == own_mac:
        bssid = None
    probes = []
    for p in raw.get("probed") if isinstance(raw.get("probed"), list) else []:
        if not isinstance(p, dict):
            continue
        ssid = _text(p.get("dot11.probedssid.ssid"))
        first = _positive(p.get("dot11.probedssid.first_time"))
        last = _positive(p.get("dot11.probedssid.last_time"))
        if ssid is None or first is None or last is None:
            continue
        probes.append([ssid, int(first), int(last)])
    if bssid is None and not probes:
        return None
    return {"bssid": bssid, "probes": probes}


# --- alert records ------------------------------------------------------------

def shape_alert(raw):
    """Turn one Kismet alert (kismet.alert.*) into a flat dict.

    Field names come from the Kismet binary; no alert had fired when this was
    written, so the value types are handled leniently and the complete JSON is
    kept in "raw" until they are confirmed.
    """
    g = raw.get
    ts = _num(g("kismet.alert.timestamp"))
    header = _nonempty(g("kismet.alert.header")) or "UNKNOWN"
    rec = {
        "ts": float(ts) if ts is not None else None,
        "header": header,
        "class": _nonempty(g("kismet.alert.class")),
        "severity": _counter(g("kismet.alert.severity")),
        "source_mac": _mac(g("kismet.alert.source_mac")),
        "dest_mac": _mac(g("kismet.alert.dest_mac")),
        "transmitter_mac": _mac(g("kismet.alert.transmitter_mac")),
        "other_mac": _mac(g("kismet.alert.other_mac")),
        "channel": _channel_text(g("kismet.alert.channel")),
        "freq": _positive(g("kismet.alert.frequency")),
        "device_key": _nonempty(g("kismet.alert.device_key")),
        "text": _text(g("kismet.alert.text")),
        "raw": json.dumps(raw, separators=(",", ":"), sort_keys=True),
    }
    h = g("kismet.alert.hash")
    if isinstance(h, (int, str)) and not isinstance(h, bool) and h not in (0, ""):
        rec["hash"] = str(h)
    else:
        # No usable hash: derive one so the same alert is never stored twice.
        ident = "|".join(str(rec[k]) for k in (
            "ts", "header", "source_mac", "dest_mac", "transmitter_mac", "text"))
        rec["hash"] = "sha1:" + hashlib.sha1(ident.encode()).hexdigest()
    return rec


def _channel_text(v):
    if isinstance(v, str):
        return v or None
    n = _positive(v)
    return str(int(n)) if n is not None else None
