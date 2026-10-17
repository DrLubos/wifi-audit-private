"""HTTPS to the server's ingest (server/docs/live-upload.md 5).

gzip-JSON POSTs with the sensor's bearer token. TLS is always verified - the
stdlib default context checks the certificate chain and the host name, and
there is no switch to turn that off (amendment 2); `cafile` exists only for
the tests' local CA. https only: a plain http URL is refused before anything
is sent. The token is never logged (describe_token()).

Standard library only (tests/test_client.py).
"""

import gzip
import json
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class Refused(Exception):
    """A configuration the uploader will not use (e.g. not https)."""


def describe_token(token):
    parts = (token or "").split(".")
    return "token %s" % parts[1] if len(parts) == 3 and parts[1].isdigit() else "a malformed token"


class Client:
    def __init__(self, base_url, token, timeout=30, cafile=None):
        if urlsplit(base_url).scheme != "https":
            raise Refused("INGEST_URL must be https (got %r)" % base_url.split("://")[0])
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=cafile)

    def post(self, path, payload):
        """(status, body dict or None, rtt_ms, mono_mid). status 0 = no HTTP
        answer (network, TLS, timeout); body carries the error text then."""
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip",
            "Authorization": "Bearer " + self.token, "User-Agent": "wifi-sensor-uploader/1"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            t1 = time.monotonic()
            reason = getattr(e, "reason", e)
            return 0, {"detail": "%s: %s" % (type(reason).__name__, reason)}, int((t1 - t0) * 1000), (t0 + t1) / 2
        t1 = time.monotonic()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        return status, body, int((t1 - t0) * 1000), (t0 + t1) / 2

    @staticmethod
    def size(payload):
        return len(gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
