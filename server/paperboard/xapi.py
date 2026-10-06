"""X (Twitter) integration — adapter boundary.

Every call into X goes through one interface (XClient), so an API migration
only touches this file. Live mode talks to api.x.com/2; mock mode returns a
deterministic demo feed and is always flagged so nobody mistakes it for prod.

OAuth: X OAuth 2.0 (web login) with PKCE. The client secret stays server-side;
the frontend only sees a redirect URL.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime as _dt

AUTHORIZE_URL = "https://x.com/i/oauth/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API_BASE = "https://api.x.com/2"

USER_FIELDS = "id,username,name,profile_image_url"
TWEET_FIELDS = "id,text,created_at,public_metrics,author_id"
NON_PUBLIC = "impression_count"


def b64url(raw):
    return base64.b64encode(raw).decode().rstrip("=").replace("+", "-").replace("/", "_")


class XError(RuntimeError):
    pass


class MockXClient:
    """Deterministic demo feed. Never reports itself as production data."""

    mode = "mock"

    def __init__(self, settings, clock=None):
        self.clock = clock or time.time
        self.paper_username = settings.pb["eligibility"]["followAccount"]
        # tiny scripted world — clearly fake handles so nobody is fooled
        self._users = {
            "mock-user-0001": dict(id="mock-user-0001", username="degenledger",
                                   name="Degen Ledger", follows_paper=True),
            "mock-user-0002": dict(id="mock-user-0002", username="chartwitch",
                                   name="Chart Witch", follows_paper=True),
            "mock-user-0003": dict(id="mock-user-0003", username="candlegremlin",
                                   name="Candle Gremlin", follows_paper=True),
            "mock-user-0004": dict(id="mock-user-0004", username="memecoinmom",
                                   name="Memecoin Mom", follows_paper=True),
            "mock-user-0005": dict(id="mock-user-0005", username="bidetbear",
                                   name="Bidet Bear", follows_paper=False),
        }
        ca = settings.raw["brand"]["contractAddress"]
        now = self.clock()
        day = 86400

        def post(pid, uid, text, ago, m):
            return dict(
                id=pid, author_id=uid, text=text,
                url=f"https://x.com/i/web/status/{pid}",
                created_at=now - ago,
                public_metrics=dict(retweet_count=m[0], reply_count=m[1],
                                    like_count=m[2], quote_count=m[3]),
                non_public_metrics=dict(impression_count=m[4]),
            )

        self._posts = [
            post("m1", "mock-user-0001", f"stacking {ca} while the chart sleeps", 2 * day, (3, 1, 12, 0, 640)),
            post("m2", "mock-user-0001", "$paper front page energy today", 5 * day, (8, 4, 30, 2, 2400)),
            post("m3", "mock-user-0002", "watching @paperusdc 👀", 1 * day, (12, 6, 40, 3, 5100)),
            post("m4", "mock-user-0002", f"{ca} is the internet's financial paper", 9 * day, (5, 2, 18, 1, 1100)),
            post("m5", "mock-user-0003", "$PAPER? no. $paper.", 3 * day, (2, 0, 9, 0, 300)),
            post("m6", "mock-user-0004", "paper posted. points earned.", 6 * day, (20, 9, 55, 6, 9000)),
            post("m7", "mock-user-0004", f"CA {ca} framed on my wall", 12 * day, (30, 15, 90, 9, 24000)),
            post("m8", "mock-user-0005", "@paperusdc (don't follow me, just admire)", 4 * day, (4, 1, 11, 0, 820)),
            post("m9", "mock-user-0003", f"$paper {ca} @paperusdc triple threat", 7 * day, (6, 3, 22, 1, 1600)),
            post("m10", "mock-user-0002", "$paper print the front page", 8 * day, (9, 5, 26, 2, 3300)),
            post("m11", "mock-user-0004", f"{ca} runs on attention", 0.5 * day, (11, 4, 33, 2, 2100)),
            post("m12", "mock-user-0001", "@paperusdc posting from the front page", 7.5 * day, (7, 2, 19, 1, 1450)),
        ]

    def me(self, user_id):
        u = self._users.get(user_id)
        if not u:
            raise XError("unknown mock user")
        return dict(id=u["id"], username=u["username"], name=u["name"],
                    profile_image_url="")

    def follows_paper(self, user_id):
        u = self._users.get(user_id)
        if not u:
            raise XError("unknown mock user")
        return bool(u["follows_paper"])

    def recent_posts(self, user_id, since, start_time=None, token=None, max_results=50):
        floor = start_time if start_time is not None else since
        out = [p for p in self._posts
               if p["author_id"] == user_id and p["created_at"] >= floor]
        out.sort(key=lambda p: p["created_at"], reverse=True)
        out = out[:max_results]
        return out, dict(returned=len(out), more=False, next_token=None)

    def metrics_for(self, ids):
        ids = set(ids)
        out = [p for p in self._posts if p["id"] in ids]
        return out, dict(returned=len(out), more=False, next_token=None)

    def exchange_code(self, code, verifier):
        uid = "mock-user-0001"
        return dict(
            access_token="mock-token-" + secrets.token_hex(6),
            user=dict(id=uid, username=self._users[uid]["username"],
                      name=self._users[uid]["name"], profile_image_url=""),
        )

    def authorize_url(self, state, code_challenge):
        return None  # mock flow goes straight through /api/auth/x/mock-login


class LiveXClient:
    """X API v2 + OAuth 2.0 (PKCE). One place for every endpoint shape."""

    mode = "live"

    def __init__(self, settings):
        self.s = settings
        if not settings.x_bearer:
            raise XError("X_API_BEARER missing — live mode cannot read v2 endpoints")

    def _get(self, path, params=None):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.s.x_bearer}",
            "User-Agent": "paperboard/0.2",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:400]
            print(f"[xapi] GET {path} failed {e.code}")
            raise XError(f"x api {e.code}: {body}")
        d = payload.get("data")
        n = len(d) if isinstance(d, list) else (1 if d else 0)
        print(f"[xapi] GET {path} -> {n} resource(s)")
        return payload

    def me(self, user_id):
        d = self._get(f"/users/{user_id}", dict(user_fields=USER_FIELDS)).get("data") or {}
        return dict(id=d.get("id"), username=d.get("username"), name=d.get("name"),
                    profile_image_url=d.get("profile_image_url", ""))

    def follows_paper(self, user_id):
        # v2 follow lookup: returns which of the requested usernames the author follows
        acct = self.s.pb["eligibility"]["followAccount"]
        data = self._get(f"/users/{user_id}/following",
                         dict(usernames=acct, max_results="1"))
        return bool(data.get("data"))

    def recent_posts(self, user_id, since, start_time=None, token=None, max_results=50):
        params = [("tweet_fields", TWEET_FIELDS), ("non_public_metrics", NON_PUBLIC),
                  ("max_results", str(max_results))]
        if start_time is not None:                      # server-side window (P1)
            params.append(("start_time", _iso_utc(start_time)))
        if token:
            params.append(("pagination_token", token))
        data = self._get(f"/users/{user_id}/tweets", params)
        meta = data.get("meta") or {}
        rows = [_map_post(d) for d in data.get("data") or []]
        out = [p for p in rows if p.get("created_at") and p["created_at"] >= since]
        return out, dict(returned=int(meta.get("result") or len(rows)),
                         more=bool(meta.get("next")),
                         next_token=meta.get("next"))

    def metrics_for(self, ids):
        ids = list(ids)[:100]                           # v2 /tweets ids cap
        params = [("ids", i) for i in ids] + [("tweet_fields", TWEET_FIELDS),
                                              ("non_public_metrics", NON_PUBLIC)]
        data = self._get("/tweets", params)
        rows = [_map_post(d) for d in data.get("data") or []]
        return rows, dict(returned=len(rows), more=False, next_token=None)

    def authorize_url(self, state, code_challenge):
        q = urllib.parse.urlencode(dict(
            response_type="code", client_id=self.s.x_client_id,
            redirect_uri=self.s.x_redirect_uri,
            scope="users.read tweet.read follows.read offline.access",
            state=state, code_challenge=code_challenge,
            code_challenge_method="S256",
        ))
        return f"{AUTHORIZE_URL}?{q}"

    def exchange_code(self, code, verifier):
        body = urllib.parse.urlencode(dict(
            grant_type="authorization_code", code=code,
            client_id=self.s.x_client_id, client_secret=self.s.x_client_secret,
            redirect_uri=self.s.x_redirect_uri, code_verifier=verifier,
        )).encode()
        basic = base64.b64encode(
            f"{self.s.x_client_id}:{self.s.x_client_secret}".encode()).decode()
        req = urllib.request.Request(TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                tok = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise XError(f"x token {e.code}: {e.read().decode(errors='replace')[:300]}")
        me_url = API_BASE + "/users/me?user_fields=" + urllib.parse.quote(USER_FIELDS)
        req2 = urllib.request.Request(me_url,
                                       headers={"Authorization": f"Bearer {tok['access_token']}"})
        with urllib.request.urlopen(req2, timeout=10) as r2:
            me = json.loads(r2.read())["data"]
        return dict(access_token=tok["access_token"],
                    user=dict(id=me["id"], username=me["username"], name=me.get("name"),
                              profile_image_url=me.get("profile_image_url", "")))


def _iso_to_epoch(ts):
    return _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _iso_utc(t):
    from datetime import timezone
    return _dt.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _map_post(d):
    """one shape for a tweet object, from either endpoint."""
    pm = d.get("public_metrics") or {}
    npm = d.get("non_public_metrics") or {}
    return dict(
        id=d.get("id"),
        author_id=d.get("author_id"),
        text=d.get("text") or "",
        url=f"https://x.com/i/web/status/{d.get('id')}",
        created_at=_iso_to_epoch(d["created_at"]) if d.get("created_at") else None,
        public_metrics=dict(retweet_count=pm.get("retweet_count", 0),
                            reply_count=pm.get("reply_count", 0),
                            like_count=pm.get("like_count", 0),
                            quote_count=pm.get("quote_count", 0)),
        non_public_metrics=dict(impression_count=npm.get("impression_count", 0)),
    )


def make_pkce():
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge
