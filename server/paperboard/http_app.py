"""HTTP surface: static site + JSON API on one port.

One dispatch() entry: static files or JSON envelopes. Every client input is
validated here (query params, JSON bodies, cookies, headers) — the DB stays
the authority, the client just asks.
"""

import json
import os
import re
import secrets
import time

from .settings import SITE_DIR
from .xapi import make_pkce, XError

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


class Router:
    """Routes /api/* + static serving of the site directory."""

    def __init__(self, service, settings, db, x):
        self.s = service
        self.settings = settings
        self.db = db
        self.x = x
        self.clock = time.time
        self._rl = {}

    # ------------------------------------------------------------ rate limits
    def _allow(self, key, limit, window):
        """In-process fixed-window limiter (dev-grade; swap for a real store
        at deploy time). Returns False when over budget."""
        bucket = int(self.clock() // window)
        store = self._rl.setdefault(key, {"b": bucket, "n": 0})
        if store["b"] != bucket:
            store["b"] = bucket
            store["n"] = 0
        store["n"] += 1
        return store["n"] <= limit

    # ------------------------------------------------------------------ static
    def _static(self, rel):
        full = os.path.normpath(os.path.join(SITE_DIR, rel))
        if not full.startswith(os.path.abspath(SITE_DIR)):
            return 403, {"error": "forbidden_path"}
        if not os.path.isfile(full):
            return 404, {"error": "not_found"}
        ext = os.path.splitext(full)[1].lower()
        ctype = _TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            return 200, fh.read(), ctype

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, method, raw_path, query, headers, cookies, body):
        """query: dict[str,str] · headers/cookies: dict[str,str] (lowercased keys)
        Returns (status, payload, headers_out). payload is dict → JSON, str/bytes → raw."""
        path = re.sub("/+", "/", "/" + raw_path)
        s, settings = self.s, self.settings
        now = self.clock()
        comp = s.active_competition()
        cid = comp["id"] if comp else None
        user = s.session_user(cookies.get("PBSD"))
        mode = "mock" if settings.mock else "live"

        if path.startswith("/api/"):
            ip = headers.get("x-forwarded-for") or headers.get("x-real-ip") or "?"
            if not self._allow(f"api:{ip}",
                               settings.pb["ratelimit"]["apiPerMinute"], 60):
                return 429, {"error": "rate_limited"}
            return self._api(method, path, query, headers, cookies, body,
                             user, comp, cid, now, mode)
        if path == "/":
            return self._static("index.html")
        return self._static(path.lstrip("/"))

    # ------------------------------------------------------------------ api
    def _api(self, method, path, query, headers, cookies, body, user, comp, cid, now, mode):
        s, settings, pb = self.s, self.settings, self.settings.pb
        rl = pb["ratelimit"]
        ok = lambda **kw: (200, dict(**kw), None)
        fail = lambda code, err: (code, {"error": err}, None)

        if method == "GET":
            if path == "/api/health":
                from . import __version__
                return ok(ok=True, version=__version__, now=now, mode=mode,
                          modeReason=settings.mock_reason, scheduler=settings.scheduler)
            if path == "/api/config":
                return 200, settings.public_view(), None
            if path == "/api/competitions":
                rows = [dict(r) for r in
                        s.db.conn.execute("SELECT * FROM competitions ORDER BY id")]
                for r in rows:
                    r["state"] = s.competition_state(r["id"])
                return ok(competitions=rows)
            if path == "/api/leaderboard":
                if cid is None:
                    return fail(404, "no_active_competition")
                view = s.leaderboard(cid)
                if not view:
                    return fail(503, "leaderboard_not_built")
                try:
                    limit = int(query.get("rows") or 0)
                except ValueError:
                    return fail(400, "bad_rows_param")
                rows = view["rows"][:limit] if limit > 0 else view["rows"]
                return ok(comp=comp["slug"], state=s.competition_state(cid),
                          generatedAt=view["generated_at"],
                          nextRefreshAt=view["next_refresh_at"],
                          refreshMinutes=pb["refreshMinutes"],
                          totalPoints=view["total_points"],
                          participants=view["participants"], rows=rows, mode=mode)
            if path == "/api/me":
                if not user:
                    return ok(connected=False)
                if cid is None:
                    return ok(connected=True, competition_state="none")
                return 200, s.me_view(user, cid), None
            if path == "/api/me/posts":
                if not user:
                    return fail(401, "not_connected")
                if cid is None:
                    return fail(404, "no_active_competition")
                return ok(posts=s.posts_view(user, cid), mode=mode)
            return fail(404, "unknown_api_route")

        if method == "POST":
            if path == "/api/auth/x/start":
                if settings.mock:
                    return ok(mock=True, url="/api/auth/x/mock-login")
                verifier, challenge = make_pkce()
                state = secrets.token_hex(16)
                s.db.conn.execute("INSERT OR REPLACE INTO oauth_states VALUES(?,?,?)",
                                  (state, verifier, now))
                s.db.conn.commit()
                try:
                    url = self.x.authorize_url(state, challenge)
                except XError as e:
                    return fail(503, f"oauth_unconfigured: {e}")
                return ok(url=url, state=state)
            if path == "/api/auth/x/callback":
                code = query.get("code")
                state = query.get("state")
                if not code or not state:
                    return fail(400, "missing_code_or_state")
                row = s.db.conn.execute(
                    "SELECT code_verifier FROM oauth_states WHERE state=?",
                    (state,)).fetchone()
                if not row:
                    return fail(400, "state_mismatch_or_expired")
                try:
                    tok = self.x.exchange_code(code, row["code_verifier"])
                except XError as e:
                    return fail(502, f"x_exchange_failed: {e}")
                uid = s.upsert_user(tok["user"])
                token = s.new_session(uid)
                s.db.conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
                s.db.conn.commit()
                return ok(redirect="/#board", session=token,
                          user=dict(handle=tok["user"]["username"],
                                    name=tok["user"].get("name")))
            if path == "/api/auth/x/mock-login":
                uid = s.upsert_user(dict(
                    id="mock-user-0001", username="degenledger",
                    name="Degen Ledger", profile_image_url=""))
                return ok(redirect="/#board", session=s.new_session(uid))
            if path == "/api/auth/x/logout":
                if cookies.get("PBSD"):
                    s.db.conn.execute("DELETE FROM sessions WHERE token=?",
                                      (cookies["PBSD"],))
                    s.db.conn.commit()
                return ok(loggedOut=True)
            if path == "/api/me/wallet":
                if not user:
                    return fail(401, "not_connected")
                addr, err = s.set_wallet(user["id"], (body or {}).get("wallet"))
                if err:
                    return fail(422, err)
                return ok(wallet=addr)
            if path == "/api/me/scan":
                if not user:
                    return fail(401, "not_connected")
                if cid is None:
                    return fail(404, "no_active_competition")
                if not self._allow(f"scan:{user['x_user_id']}",
                                   rl["scanPerTenMinutes"], 600):
                    return fail(429, "scan_rate_limited")
                res = s.scan_user(user, cid)
                if not res.get("ok"):
                    return fail(502, res.get("reason", "scan_failed"))
                s.refresh_standings(cid, movement_from_prev=False)
                return 200, dict(scan=res, **s.me_view(user, cid)), None
            if path == "/api/admin/refresh":
                tok = headers.get("x-paper-token") or ""
                if not settings.admin_token and not settings.mock:
                    return fail(503, "admin_token_not_configured")
                want = settings.admin_token or "dev"
                if tok != want:
                    return fail(401, "bad_admin_token")
                if cid is None:
                    return fail(404, "no_active_competition")
                return ok(snapshot=s.refresh_standings(cid),
                          competition=comp["slug"])
            return fail(404, "unknown_api_route")

        return fail(405, "method_not_allowed")
