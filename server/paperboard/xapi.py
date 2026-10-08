"""X (Twitter) integration — adapter boundary.

Every call into X goes through one interface, so an API migration only
touches this file. PAPERBOARD's auth does NOT go through X: the account
layer logs in by magic link. X enters at exactly two server-side points:

  * resolve_post(post_id)  — the syndication provider verifies a public,
    user-pasted post. Public read, no user OAuth, no user token.
  * follows_username(handle) — the eligibility follow-gate, best effort:
    True / False / None ("unknown"), never a guess.

SyndicationXClient is the currently-tested direction: public verification +
the public metrics it actually reports (never invented — absent metrics
stay None and are listed as missing). The older OAuth scan stack
(MockXClient / LiveXClient) stays intact for the legacy Connect-X routes
and for the scan pipeline; another provider can slot in behind the same
resolve/follows interface without touching accounts, scoring, or the routes.
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

    # -- submission interface (same shape SyndicationXClient returns) ------
    def resolve_post(self, post_id):
        """The pasted id is looked up in the scripted world only — the mock
        never invents a post it was not told about (unknown ids → None,
        honestly, never a synthesized pass)."""
        for p in self._posts:
            if p["id"] == post_id:
                return self._shape(p)
        return None

    def follows_username(self, handle):
        for u in self._users.values():
            if u["username"].lower() == handle.lower():
                return bool(u["follows_paper"])
        return False            # unknown handles in the script world: gate off

    def _shape(self, p):
        pm = p.get("public_metrics") or {}
        npm = p.get("non_public_metrics") or {}
        author = (self._users.get(p["author_id"]) or {}).get("username") or ""
        metrics = dict(likes=pm.get("like_count"), replies=pm.get("reply_count"),
                       reposts=pm.get("retweet_count"), quotes=pm.get("quote_count"),
                       impressions=npm.get("impression_count"))
        return dict(
            provider=self.mode, post_id=p["id"], author=author.lower(),
            author_id=p.get("author_id"), text=p.get("text") or "",
            url=f"https://x.com/{author.lower()}/status/{p['id']}",
            posted_at=p.get("created_at"), metrics=metrics,
            provided=[k for k, v in metrics.items() if v is not None],
        )

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

    # -- submission interface (the v2 app-token path, no user OAuth needed) --
    def resolve_post(self, post_id):
        rows, _meta = self.metrics_for([post_id])
        if not rows:
            return None
        p = rows[0]
        author = ""
        try:                                            # author byline is print-only
            author = (self.me(p["author_id"]) or {}).get("username") or ""
        except XError:
            pass
        metrics = dict(likes=p["public_metrics"]["like_count"],
                       replies=p["public_metrics"]["reply_count"],
                       reposts=p["public_metrics"]["retweet_count"],
                       quotes=p["public_metrics"]["quote_count"],
                       impressions=p["non_public_metrics"]["impression_count"])
        return dict(provider="v2", post_id=p["id"], author=author.lower(),
                    author_id=p.get("author_id"), text=p["text"],
                    url=p["url"], posted_at=p.get("created_at"),
                    metrics=metrics,
                    provided=[k for k, v in metrics.items() if v is not None])

    def follows_username(self, handle):
        acct = self.s.pb["eligibility"]["followAccount"]
        try:
            data = self._get(f"/users/by/username/{handle}/following",
                             dict(usernames=acct, max_results="1"))
        except XError:
            return None                                 # gate defers, never guesses
        return bool(data.get("data"))

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


class SyndicationXClient:
    """The syndication feed — public post verification with NO user OAuth and
    no per-user token. One unauthenticated GET per pasted post; metrics are
    whatever the feed honestly reports (likes, replies when present). What it
    does not report stays None and is listed as missing — the scorer must
    not pretend it exists. The follow-gate rides the app bearer when set."""

    mode = "live"
    SYND_URL = "https://cdn.syndication.twitter.com/tweet/f/json"

    def __init__(self, settings):
        self.s = settings

    def resolve_post(self, post_id):
        url = f"{self.SYND_URL}?id={post_id}&dnt=true"
        req = urllib.request.Request(url, headers={"User-Agent": "paperboard/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise XError(f"syndication {e.code}")
        except OSError as e:
            raise XError(f"syndication unreachable: {e}")
        except ValueError as e:      # live 2026 note: a deprecated CDN stub can
            raise XError(f"syndication unparseable: {e}")   # answer 200 + empty
        if not isinstance(payload, dict):
            raise XError("syndication unparseable: not an object")
        if not payload:
            return None                                  # gone-tweets arrive {}
        t = payload.get("tweet") or {}
        if not t or t.get("inaccessible"):
            return None
        pm = t.get("public_metrics") or t               # accept both feed shapes
        def metric(*names):
            for n in names:
                v = pm.get(n)
                if isinstance(v, int):
                    return v
            return None
        user = (t.get("user") or {})
        created = t.get("created_at")
        posted = None
        if isinstance(created, (int, float)):            # epoch ms on this feed
            posted = float(created) / 1000
        elif isinstance(created, str):
            try:
                posted = _iso_to_epoch(created)
            except (ValueError, TypeError):
                pass
        metrics = dict(likes=metric("favorite_count", "like_count"),
                       replies=metric("conversation_count", "reply_count"),
                       reposts=metric("retweet_count"),
                       quotes=metric("quote_count"),
                       impressions=metric("impression_count"))
        author = (user.get("screen_name") or user.get("username") or "").lower()
        pid = str(t.get("id_str") or t.get("id") or post_id)
        return dict(provider="syndication", post_id=pid, author=author,
                    author_id=str(user.get("id_str") or user.get("id") or "") or None,
                    text=t.get("text") or "",
                    url=f"https://x.com/{author}/status/{pid}",
                    posted_at=posted, metrics=metrics,
                    provided=[k for k, v in metrics.items() if v is not None])

    def follows_username(self, handle):
        """The gate rides the v2 app token (one small GET). No bearer, or a
        404 handle, or transport trouble — all answer None ('unknown'), which
        defers the gate to the scan instead of faking a verdict."""
        if not self.s.x_bearer:
            return None
        try:
            v2 = LiveXClient(self.s)
            return v2.follows_username(handle)
        except XError:
            return None

    # the Connect-X-era scan pipeline belongs to the v2 clients; on this
    # provider it degrades with a clear reason instead of an AttributeError
    def me(self, user_id):
        raise XError("scan pipeline needs the live v2 client")

    def follows_paper(self, user_id):
        raise XError("scan pipeline needs the live v2 client")

    def recent_posts(self, user_id, since, start_time=None, token=None,
                      max_results=50):
        raise XError("scan pipeline needs the live v2 client")

    def metrics_for(self, ids):
        raise XError("scan pipeline needs the live v2 client")


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
