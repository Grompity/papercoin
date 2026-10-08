#!/usr/bin/env python3
"""PAPERBOARD server entry.

  python3 server/run.py            # serve site + api + scheduler (mock or live)
  python3 server/run.py --once     # run one standings refresh, then exit (cron mode)

Mock mode boots a small deterministic demo world so the frontend can show a
populated front page without X credentials. It is always flagged (mode:"mock").
"""

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, __file__.rpartition("/")[0])  # server/

from paperboard import db as dbmod, http_app, service as svc  # noqa: E402
from paperboard.settings import Settings                      # noqa: E402
from paperboard.xapi import (MockXClient, SyndicationXClient, FXTwitterXClient,
                             OembedXClient, FallbackXClient)             # noqa: E402


def build():
    settings = Settings()
    database = dbmod.DB(settings.db_path)
    # live mode runs the zero-cost stack (2026-10): FxEmbed first for the
    # engagement truth, official oEmbed behind it for presence-only proof —
    # no X Connect, no per-user OAuth, no paid API, no wallet ceremony.
    # (SyndicationXClient stays as the recorded historic route.)
    x = MockXClient(settings) if settings.mock else \
        FallbackXClient(FXTwitterXClient(settings), OembedXClient(settings))
    svc_ = svc.Service(database, x, settings)
    cid = svc_.ensure_competition()
    router = http_app.Router(svc_, settings, database, x)
    return settings, database, x, svc_, router, cid


def seed_mock_world(svc_, cid):
    """Idempotent demo seeding: known mock users, scanned + ranked.
    The demo scan stays clickable because mock scans never spend X budget."""
    already = svc_.db.conn.execute(
        "SELECT 1 FROM users WHERE x_user_id LIKE 'mock-%' LIMIT 1").fetchone()
    if not already:
        for uid in ("mock-user-0001", "mock-user-0002", "mock-user-0003",
                    "mock-user-0004", "mock-user-0005"):
            u = svc_.x.me(uid)
            row_id = svc_.upsert_user(u)
            user_row = dict(svc_.db.conn.execute(
                "SELECT * FROM users WHERE id=?", (row_id,)).fetchone())
            svc_.scan_user(user_row, cid)
        svc_.refresh_standings(cid)
    svc_.db.conn.execute(
        "UPDATE users SET last_scan_at=NULL WHERE x_user_id LIKE 'mock-%'")
    svc_.db.conn.commit()


class Handler(BaseHTTPRequestHandler):
    server_version = "Paper/0.2 "
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[pb]", fmt % args)

    def _go(self, method):
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        raw = self.headers.get("Content-Length")
        body = None
        if raw and int(raw) > 0 and int(raw) < 10000:
            data = self.rfile.read(int(raw))
            try:
                body = json.loads(data)
            except ValueError:
                body = {}
        headers = {k.lower(): v for k, v in self.headers.items()}
        cookies = {}
        for part in (headers.get("cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()

        out = self.server.router.dispatch(method, parsed.path, query, headers,
                                           cookies, body)
        status, payload, extra = out if len(out) == 3 else (out[0], out[1], None)
        router = self.server.router
        self.send_response(status)
        self.send_header("Cache-Control", "no-cache")
        if isinstance(payload, dict):
            flags = "Path=/; HttpOnly; SameSite=Lax"
            if getattr(router.settings, "secure_cookies", False):
                flags += "; Secure"
            if payload.get("session"):
                days = int(router.s.acfg.get("sessionDays", 30))
                self.send_header("Set-Cookie",
                                 f"PBSD={payload['session']}; {flags};"
                                 f" Max-Age={days * 86400}")
            elif payload.get("loggedOut"):
                self.send_header("Set-Cookie", "PBSD=; Path=/; Max-Age=0")
        if isinstance(extra, dict):            # route-level headers (Location, cookies…)
            for hk, hv in extra.items():
                self.send_header(hk, hv)
        if isinstance(payload, (dict, list)):
            blob = json.dumps(payload).encode()
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            blob = payload if isinstance(payload, bytes) else str(payload).encode()
            ctype = extra if isinstance(extra, str) else "text/plain; charset=utf-8"
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(blob)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_HEAD(self):
        self._go("HEAD")


class Scheduler(threading.Thread):
    daemon = True

    def __init__(self, svc_, cid, settings):
        super().__init__()
        self.svc, self.cid, self.st = svc_, cid, settings
        self.stop = threading.Event()

    def next_at(self):
        row = self.svc.db.conn.execute(
            "SELECT generated_at FROM snapshots WHERE competition_id=?"
            " ORDER BY id DESC LIMIT 1", (self.cid,)).fetchone()
        base = row["generated_at"] if row else time.time() - 999
        return base + self.st.pb["refreshMinutes"] * 60

    def run(self):
        while not self.stop.wait(5):
            try:
                if time.time() >= self.next_at():
                    self.svc.refresh_standings(self.cid)
            except Exception as e:                      # never die silently
                print("[scheduler] refresh failed:", e)


def main():
    once = "--once" in sys.argv
    settings, database, x, svc_, router, cid = build()
    if settings.mock:
        seed_mock_world(svc_, cid)
        if not svc_.db.conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone():
            svc_.refresh_standings(cid)

    if once:
        print("[once] standings refreshed:", svc_.refresh_standings(cid))
        return

    print(f"[paperboard] :{settings.port}  mode={settings.mock and 'mock' or 'live'}"
          f"  ({settings.mock_reason})")
    httpd = ThreadingHTTPServer(("0.0.0.0", settings.port), Handler)
    httpd.router = router
    if settings.scheduler and svc_.active_competition():
        sch = Scheduler(svc_, cid, settings)
        sch.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        database.close()


if __name__ == "__main__":
    main()
