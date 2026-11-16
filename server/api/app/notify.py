"""E-mail notifications (2026-11-16): the refresher mails the developer when a
sensor stops uploading or capturing, when a disk runs low, and
ops/backup_nightly.sh when the nightly backup fails. Standard library only
(smtplib; tests/test_notify.py).

Configuration from the environment (server/.env, passed by compose to the
refresher); without NOTIFY_SMTP_HOST and NOTIFY_TO nothing is sent:

  NOTIFY_SMTP_HOST      SMTP server, e.g. smtp.gmail.com
  NOTIFY_SMTP_PORT      587 (STARTTLS, default) or 465 (TLS from the start).
                        Google Cloud blocks outgoing port 25.
  NOTIFY_SMTP_USER      login (a Gmail address with an app password, ...)
  NOTIFY_SMTP_PASSWORD
  NOTIFY_FROM           sender; default NOTIFY_SMTP_USER
  NOTIFY_TO             recipient(s), comma-separated
  NOTIFY_OFFLINE_MIN    no contact / no capture for this long is an alarm (15)
  NOTIFY_DISK_MIN_GB    free space below this on the box or the sensor (2)

A message names the sensor, times and sizes only - never an SSID or a MAC
(the privacy rule). One mail when a condition starts and one when it ends;
a condition that is on when the refresher starts is mailed again (a
restart must not hide an outage). At most MAX_PER_DAY mails a day.

    python -m app.notify --subject "..." --body "..."     # one mail (backup_nightly.sh)
    python -m app.notify --test                           # a test mail
"""

import argparse
import logging
import os
import smtplib
import socket
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

log = logging.getLogger("notify")

MAX_PER_DAY = 30
PENDING_MAX = 20
SMTP_TIMEOUT_S = 20
GB = 1024 ** 3


def config(env=None):
    """The SMTP configuration, or None when notifications are off."""
    env = os.environ if env is None else env
    host, to = (env.get("NOTIFY_SMTP_HOST") or "").strip(), (env.get("NOTIFY_TO") or "").strip()
    if not host or not to:
        return None
    user = (env.get("NOTIFY_SMTP_USER") or "").strip()
    try:
        port = int(env.get("NOTIFY_SMTP_PORT") or 587)
        offline = float(env.get("NOTIFY_OFFLINE_MIN") or 15)
        disk = float(env.get("NOTIFY_DISK_MIN_GB") or 2)
    except ValueError as e:
        raise ValueError("notify: a NOTIFY_* number is invalid: %s" % e)
    return {"host": host, "port": port, "user": user, "password": env.get("NOTIFY_SMTP_PASSWORD") or "",
            "from": (env.get("NOTIFY_FROM") or user).strip(),
            "to": [a.strip() for a in to.split(",") if a.strip()],
            "offline_min": offline, "disk_min_gb": disk}


def message(cfg, subject, body):
    m = EmailMessage()
    m["Subject"] = "[wifi-audit] " + subject
    m["From"] = cfg["from"] or cfg["to"][0]
    m["To"] = ", ".join(cfg["to"])
    m.set_content(body + "\n\n-- \nwifi-audit server (%s)\n" % socket.gethostname())
    return m


def send(cfg, subject, body, smtp=None):
    """Send one mail; raises on failure (the caller keeps it for a retry)."""
    m = message(cfg, subject, body)
    ctx = ssl.create_default_context()
    if smtp is None:
        smtp = smtplib.SMTP_SSL if cfg["port"] == 465 else smtplib.SMTP
    kw = {"context": ctx} if cfg["port"] == 465 else {}
    with smtp(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_S, **kw) as s:
        if cfg["port"] != 465:
            s.starttls(context=ctx)
        if cfg["user"]:
            s.login(cfg["user"], cfg["password"])
        s.send_message(m)


def _local(ts, tz):
    if ts is None:
        return "never"
    return "%s (%s UTC)" % (ts.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M"), ts.strftime("%H:%M"))


def _ago(now, ts):
    s = int((now - ts).total_seconds())
    return "%d min" % (s // 60) if s < 7200 else "%.1f h" % (s / 3600) if s < 172800 else "%.1f days" % (s / 86400)


def conditions(sensors, box_free_bytes, now, cfg, tz="UTC"):
    """{key: (subject, body)} of every alarm that is on now.

    sensors: rows with name, last_contact_at, last_capture_at,
    disk_free_bytes, discarding - the sensors that ever uploaded."""
    out = {}
    limit = timedelta(minutes=cfg["offline_min"])
    for s in sensors:
        name, contact, capture = s["name"], s["last_contact_at"], s["last_capture_at"]
        if contact is None or now - contact > limit:
            out["contact:" + name] = (
                "sensor %s: no contact" % name,
                "The sensor %s has not contacted the server since %s (%s ago).\n"
                "Its buffer keeps 14 days; check the Pi's power and network, then the uploader "
                "(systemctl status wifi-sensor-uploader)." % (name, _local(contact, tz), _ago(now, contact) if contact else "-"))
        elif not s.get("discarding") and (capture is None or now - capture > limit):
            # contact without capture: the adapter or Kismet (the 2026-10-06 adapter loss)
            out["capture:" + name] = (
                "sensor %s: uploading, but not capturing" % name,
                "The sensor %s uploads, but its last collecting poll was %s (%s ago).\n"
                "Check the capture adapter and Kismet (journalctl -u kismet, the capture watchdog)."
                % (name, _local(capture, tz), _ago(now, capture) if capture else "-"))
        free = s.get("disk_free_bytes")
        if free is not None and free < cfg["disk_min_gb"] * GB:
            out["disk:" + name] = ("sensor %s: disk almost full" % name,
                                   "The sensor %s has %.1f GB free (alarm below %.1f GB)."
                                   % (name, free / GB, cfg["disk_min_gb"]))
    if box_free_bytes is not None and box_free_bytes < cfg["disk_min_gb"] * GB:
        out["disk:box"] = ("server disk almost full",
                           "The server box has %.1f GB free (alarm below %.1f GB). Database growth and "
                           "the retention: docs/disk-retention.md." % (box_free_bytes / GB, cfg["disk_min_gb"]))
    return out


class Tracker:
    """Turns the conditions of each check into mails: one when a condition
    starts, one when it ends; failed sends are retried, at most
    MAX_PER_DAY mails a day."""

    def __init__(self):
        self.active = {}         # key -> (subject, since)
        self.pending = []        # (subject, body) not sent yet
        self.sent = []           # send times of the last day

    def update(self, now_conditions, now):
        for key, (subject, body) in now_conditions.items():
            if key not in self.active:
                self.active[key] = (subject, now)
                self.pending.append((subject, body))
        for key in [k for k in self.active if k not in now_conditions]:
            subject, since = self.active.pop(key)
            self.pending.append(("resolved - " + subject,
                                 "Resolved: %s (it lasted %s)." % (subject, _ago(now, since))))
        del self.pending[:-PENDING_MAX]

    def flush(self, now, sender):
        """Send what is pending through sender(subject, body); returns the number sent."""
        self.sent = [t for t in self.sent if now - t < timedelta(days=1)]
        n = 0
        while self.pending and len(self.sent) < MAX_PER_DAY:
            subject, body = self.pending[0]
            try:
                sender(subject, body)
            except (OSError, smtplib.SMTPException) as e:
                log.warning("notify: mail not sent (retried next round): %s", e)
                break
            self.pending.pop(0)
            self.sent.append(now)
            n += 1
        return n


_SENSORS = (
    "SELECT s.name, a.tz, st.last_contact_at, ss.last_capture_at, "
    "       (st.heartbeat ->> 'disk_free_bytes')::bigint AS disk_free_bytes, "
    "       discard_active(s.id) IS NOT NULL AS discarding "
    "FROM sensor_status st JOIN sensors s ON s.id = st.sensor_id JOIN accounts a ON a.id = s.account_id "
    "LEFT JOIN sensor_summary ss ON ss.sensor_id = s.id ORDER BY s.id")


def box_free(path="/"):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


class Notifier:
    """The refresher's notification step: one check per round, never raises."""

    def __init__(self, cfg):
        self.cfg, self.tracker = cfg, Tracker()

    def check(self, conn, now=None):
        if self.cfg is None:
            return
        now = now or datetime.now(timezone.utc)
        try:
            rows = conn.execute(_SENSORS, prepare=False).fetchall()
            tz = rows[0]["tz"] if rows else "UTC"
            self.tracker.update(conditions(rows, box_free(), now, self.cfg, tz), now)
            n = self.tracker.flush(now, lambda subject, body: send(self.cfg, subject, body))
            if n:
                log.info("notify: %d mail(s) sent, %d condition(s) on", n, len(self.tracker.active))
        except Exception as e:                      # a notification must never stop a refresh
            log.warning("notify: check failed: %s", e)


def main(argv=None):
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="python -m app.notify", description="Send one notification mail.")
    ap.add_argument("--subject")
    ap.add_argument("--body", default="")
    ap.add_argument("--test", action="store_true", help="send a test mail")
    args = ap.parse_args(argv)
    cfg = config()
    if cfg is None:
        print("notify: off (NOTIFY_SMTP_HOST / NOTIFY_TO not set) - nothing sent", file=sys.stderr)
        return 0
    if args.test:
        args.subject, args.body = "test", "A test mail from the wifi-audit server: notifications work."
    if not args.subject:
        ap.error("--subject (or --test) is required")
    send(cfg, args.subject, args.body)
    print("notify: sent to %s" % ", ".join(cfg["to"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
