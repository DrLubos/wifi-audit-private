"""Minimal read-only client for the Kismet REST API.

Only the handful of endpoints the collector needs, all verified against
Kismet 2025.09.0:

  GET  /system/status.json
  GET  /datasource/all_sources.json
  POST /devices/views/all/last-time/{ts}/devices.json   body {"fields": [...]}
  GET  /alerts/last-time/{ts}/alerts.json

Note: /alerts/alerts.json does not exist on this version (404).
"""

import json

import requests


class KismetError(Exception):
    """Kismet is unreachable, refused the request, or returned garbage."""


class KismetClient:
    def __init__(self, base_url, user, password, timeout=15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._s = requests.Session()
        self._s.auth = (user, password)
        self._s.headers["User-Agent"] = "wifi-sensor-collector"

    def close(self):
        self._s.close()

    # --- endpoints ------------------------------------------------------------

    def status(self):
        return self._get("/system/status.json")

    def datasources(self):
        return self._get("/datasource/all_sources.json")

    def devices_since(self, since_ts, fields):
        """Devices active after since_ts (unix seconds), reduced to fields."""
        path = "/devices/views/all/last-time/%d/devices.json" % int(since_ts)
        return self._post(path, {"fields": fields})

    def alerts_since(self, since_ts):
        return self._get("/alerts/last-time/%d/alerts.json" % int(since_ts))

    # --- transport ------------------------------------------------------------

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, body):
        return self._request("POST", path, data=json.dumps(body),
                             headers={"Content-Type": "application/json"})

    def _request(self, method, path, **kw):
        url = self.base_url + path
        try:
            r = self._s.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise KismetError("%s %s: %s" % (method, path, e)) from e
        if r.status_code != 200:
            raise KismetError("%s %s: HTTP %d" % (method, path, r.status_code))
        try:
            return r.json()
        except ValueError as e:
            raise KismetError("%s %s: invalid JSON (%s)" % (method, path, e)) from e
