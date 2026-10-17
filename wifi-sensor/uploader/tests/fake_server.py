"""A stand-in for the server's ingest (/heartbeat, /batch) for the uploader's
tests: HTTPS with a test CA, the server's own payload validation
(server/api/app/ingest/validate.py), an in-memory "database" keyed like the
real one (polls by seq, observations by AP and ts - nothing twice), and
failures on demand: 'status:NNN' answers NNN without storing, 'drop' stores
the batch and then closes the connection without an answer (a lost
acknowledgement), 'slow:S' sleeps S seconds first. Standard library only;
the certificates are made with the openssl command line (tests skip without it).
"""

import gzip
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def load_validate():
    path = os.path.join(HERE, "..", "..", "..", "server", "api", "app", "ingest", "validate.py")
    spec = importlib.util.spec_from_file_location("server_validate_fake", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_certs(d):
    """(ca.pem, server cert, server key) for localhost / 127.0.0.1, signed by a
    test CA; a second, self-signed server cert for the refusal test."""
    def run(*args):
        subprocess.run(["openssl"] + list(args), cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=wifi-audit test CA")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost")
    with open(os.path.join(d, "ext.cnf"), "w") as f:
        f.write("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
        "-days", "2", "-extfile", "ext.cnf")
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "self.key", "-out", "self.pem", "-days", "2",
        "-subj", "/CN=localhost")
    return tuple(os.path.join(d, n) for n in ("ca.pem", "srv.pem", "srv.key", "self.pem", "self.key"))


class Fake:
    def __init__(self, token, resume=None):
        self.token = token
        self.resume = resume
        self.validate = load_validate()
        self.polls, self.obs, self.uploads, self.gaps, self.boots = {}, set(), {}, [], {}
        self.heartbeats = []
        self.failures = []           # consumed one per /batch request
        self.requests = 0
        self.lock = threading.Lock()

    def store(self, b):
        cols = self.validate.columns
        for row in b["tables"].get("polls", []):
            p = dict(zip(cols("polls"), row))
            self.polls.setdefault(p["seq"], p)
        for row in b["tables"].get("observations", []):
            o = dict(zip(cols("observations"), row))
            self.obs.add((o["device_key"], o["ts"]))
        key = (b["buffer_id"], b["batch_seq"])
        replay = key in self.uploads
        self.uploads[key] = self.uploads.get(key, -1) + 1
        self.gaps += [g for g in b["gaps"] if g not in self.gaps]
        for boot in b["boots"]:
            self.boots[boot["boot_id"]] = boot
        return replay


def serve(fake, certfile, keyfile):
    """(server, base url); the server runs in a daemon thread."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("Authorization") != "Bearer " + fake.token:
                return self.answer(401, {"detail": "ingest token required"})
            body = fake.validate.decode_body(raw, self.headers.get("Content-Encoding"))
            now = time.time()
            if self.path.endswith("/heartbeat"):
                fake.heartbeats.append(fake.validate.heartbeat(body["heartbeat"]))
                return self.answer(200, {"server_ts": now, "resume": fake.resume, "last_batch_seq": None})
            with fake.lock:
                fake.requests += 1
                action = fake.failures.pop(0) if fake.failures else None
            if action and action.startswith("status:"):
                return self.answer(int(action.split(":")[1]), {"detail": "injected"})
            if action and action.startswith("slow:"):
                time.sleep(float(action.split(":")[1]))
            try:
                b = fake.validate.batch(body, now)
            except fake.validate.Invalid as e:
                return self.answer(422, {"detail": str(e)})
            with fake.lock:
                replay = fake.store(b)
            if action == "drop":            # stored, but the acknowledgement never arrives
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            return self.answer(200, {"batch_seq": b["batch_seq"], "replay": replay, "inserted": {}, "ms": 1,
                                     "server_ts": now})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "https://localhost:%d/api/ingest/v1" % srv.server_address[1]
