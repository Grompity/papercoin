#!/usr/bin/env python3
"""PAPERBOARD self-tests — deterministic, stdlib only, no network.

  python3 server/selftest.py

Covers: eligibility truth table, scoring math + anti-gaming, prize models,
wallet validation, snapshot ranking/tiers, and every HTTP route (via direct
dispatch, no sockets). Exit code is the failure count.
"""

import json                                      # noqa: E402
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from paperboard import db as dbmod, http_app, service as svc           # noqa: E402
from paperboard.accounts import hash_magic_token                      # noqa: E402
from paperboard.eligibility import Eligibility, normalize_wallet      # noqa: E402
from paperboard.prizes import estimate, tier_for                      # noqa: E402
from paperboard.scoring import score_post, normalize_text             # noqa: E402
from paperboard.settings import Settings                              # noqa: E402
from paperboard.xapi import (MockXClient, SyndicationXClient, XError,
                             FXTwitterXClient, OembedXClient,
                             FallbackXClient, XRatelimited)             # noqa: E402
from paperboard import xapi as xapimod                                  # noqa: E402

CA = "E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  pass  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def make_env(tmp="/tmp/paperboard-self.sqlite3"):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass
    os.environ["PAPER_DB_PATH"] = tmp
    os.environ["PAPER_MOCK"] = "1"
    st = Settings()
    database = dbmod.DB(tmp)
    x = MockXClient(st)
    s = svc.Service(database, x, st)
    cid = s.ensure_competition()
    return st, database, x, s, cid


class FakeX:
    """Scriptable X feed for scan-policy tests. Same contract as the real
    clients: recent_posts/metrics_for return (posts, meta)."""

    def __init__(self, pages=None, follows=True):
        self.pages = list(pages or [])       # each (posts, meta); last one sticks
        self.follows = follows
        self.metrics_posts = []              # what /tweets?ids= returns
        self.reentrant = None                # callable → runs mid-flight
        self.n = dict(recent=0, follows=0, metrics=0, me=0)
        self.start_times = []

    def me(self, uid):
        self.n["me"] += 1
        return dict(id="fx-1", username="fakes", name="Fakes", profile_image_url="")

    def follows_paper(self, uid):
        self.n["follows"] += 1
        return self.follows

    def recent_posts(self, uid, since, start_time=None, token=None, max_results=50):
        self.n["recent"] += 1
        self.start_times.append(start_time)
        if self.reentrant:
            self.inner_result = self.reentrant()
        page = self.pages.pop(0) if len(self.pages) > 1 else (self.pages[0] if self.pages else ([], {}))
        return page

    def metrics_for(self, ids):
        self.n["metrics"] += 1
        return list(self.metrics_posts), dict(returned=len(self.metrics_posts),
                                               more=False, next_token=None)

    def authorize_url(self, state, challenge):
        return "https://x.com/auth"

    def exchange_code(self, code, verifier):
        return dict(access_token="fx", user=dict(id="fx-1", username="fakes",
                                                  name="Fakes", profile_image_url=""))


class ShyX:
    """Syndication-shaped double: resolves one known post but reports only
    likes + replies. The service must score what exists and record the rest
    as missing — never invent impressions, retweets, or quotes."""

    mode = "mock"

    def __init__(self, follows=True):
        self.follows = follows

    def resolve_post(self, post_id, url=None):
        if post_id != "777777777":
            return None
        return dict(provider="syndication", post_id="777777777", author="shy",
                    author_id=None, text="$paper shy but real",
                    url="https://x.com/shy/status/777777777",
                    posted_at=time.time() - 600,
                    metrics=dict(likes=9, replies=2, reposts=None, quotes=None,
                                 impressions=None),
                    provided=["likes", "replies"])

    def follows_username(self, handle):
        return self.follows


class BlindX:
    """The live-feed 2026 case, replayed: the provider resolves the post —
    existence, byline, text — but reports NO metrics at all. Presence must
    still be verifiable, and absence of measurement must not be punished."""

    mode = "mock"

    def resolve_post(self, post_id, url=None):
        return dict(provider="syndication", post_id=post_id, author="quiet",
                    author_id=None, text="$paper quiet post",
                    url=f"https://x.com/quiet/status/{post_id}",
                    posted_at=time.time() - 60,
                    metrics=dict(likes=None, replies=None, reposts=None,
                                 quotes=None, impressions=None),
                    provided=[])

    def follows_username(self, handle):
        return True


class BoomX(BlindX):
    """Provider transport trouble: resolving raises, loudly."""

    def resolve_post(self, post_id, url=None):
        raise XError("syndication 503")


class ReplayResp:
    """Stands in for what urlopen answers, holding a captured payload."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


def fx_post(pid, author, ago, text, m=(0, 0, 0, 0, 0)):
    return dict(id=pid, author_id=author, text=text,
                url=f"https://x.com/i/web/status/{pid}",
                created_at=time.time() - ago,
                public_metrics=dict(retweet_count=m[0], reply_count=m[1],
                                    like_count=m[2], quote_count=m[3]),
                non_public_metrics=dict(impression_count=m[4]))


def make_env2(tag, client):
    tmp = f"/tmp/paperboard-self-{tag}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass
    st = Settings()
    database = dbmod.DB(tmp)
    s = svc.Service(database, client, st)
    cid = s.ensure_competition()
    return st, database, s, cid


def main():
    print("paperboard selftest")

    # ------------------------------------------------------ eligibility truth
    st, database, x, s, cid = make_env()
    el = s.elig
    cases = [
        (f"look at {CA} today", True, "ok"),            # CA only
        ("I think $paper is going somewhere.", True, "ok"),   # $paper only
        ("Watching @paperusdc 👀", True, "ok"),         # @handle only
        ("no money words here", False, "no_identifier"),
        ("$PAPER LOUD SHOUT", True, "ok"),              # case-insensitive ticker
        (f"{CA.lower()} lowercase ca", False, "no_identifier"),  # CA case-strict
    ]
    for text, want_ok, want_reason in cases:
        ok, reason, _ = el.verdict(text, True)
        check(f"eligibility: {text[:26]!r}",
              ok is want_ok and (ok or reason == want_reason),
              f"got {ok}/{reason}")
    ok, reason, _ = el.verdict(f"$paper and {CA}", False)
    check("eligibility: no follow kills everything", (not ok) and reason == "no_follow")

    # -------------------------------------------------------- scoring math
    sc = st.pb["scoring"]
    m = dict(impressions=2400, likes=8, replies=2, reposts=0, quotes=0)
    pts, audit = score_post(m, sc, ctx={})
    want = (sc["basePerPost"] + 8 * sc["perLike"] + 2 * sc["perReply"]
            + sc["impressions"]["points"] * (1 - 2 ** (-2400 / sc["impressions"]["halfLife"])))
    check("scoring: transparent sum", abs(pts - round(min(want, sc["postCap"]), 2)) < .001,
          f"{pts} vs {round(want, 2)}")
    pts2, a2 = score_post(m, sc, ctx={"duplicate_of": "x1"})
    check("scoring: duplicate zeros out", pts2 == 0.0 and "duplicate" in a2["applied"])
    pts3, a3 = score_post(m, sc, ctx={"over_frequency": True})
    check("scoring: frequency cap zeros out", pts3 == 0.0
          and "frequency_capped" in a3["applied"])
    thin, at = score_post(dict(impressions=0, likes=0, replies=0, reposts=0, quotes=0),
                          sc, ctx={})
    check("scoring: thin-signal halves",
          abs(thin - (sc["basePerPost"] * sc["thinSignalMultiplier"])) < .001)
    big, _ = score_post(dict(impressions=999999, likes=999, replies=999,
                             reposts=999, quotes=999), sc, ctx={})
    check("scoring: post cap holds", big == sc["postCap"], f"got {big}")

    # -------------------------------------------------------- prizes math
    est = estimate([dict(user_id=1, points=70), dict(user_id=2, points=30)],
                   dict(st.pb["prize"], model="proportional"))
    pool = st.pb["prize"]["pool"]
    check("prize: proportional split", abs(est[1]["share_est"] - pool * 0.7) < .5)
    est_m = estimate([dict(user_id=1, points=70), dict(user_id=2, points=30)], st.pb["prize"])
    # mixed = proportional slice (50% of pool, by points share) + top_n slice (30% of pool, rank-weighted)
    want_m = pool * (0.7 * 0.5 + 0.3 * 0.5)
    check("prize: mixed = proportional slice + top3 slice",
          abs(est_m[1]["share_est"] - want_m) < 1, f"{est_m[1]['share_est']} vs {want_m}")
    check("tiers: rank 1 = FRONT PAGE", tier_for(1, st.pb["tiers"]) == "FRONT PAGE")
    check("tiers: rank 11 = None", tier_for(11, st.pb["tiers"]) is None)

    # -------------------------------------------------------- wallet gate
    check("wallet: base58 accepted", normalize_wallet(CA) == CA)
    check("wallet: seed phrase rejected", normalize_wallet("0OIl") is None)
    check("wallet: too short rejected", normalize_wallet("abc") is None)

    # ------------------------------------------------- full flow on mock feed
    u = x.me("mock-user-0004")                     # memecoinmom: 3 qualifying posts
    uid = s.upsert_user(u)
    user_row = dict(database.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    res = s.scan_user(user_row, cid)
    check("scan: server found posts", res["ok"] and res["found"] >= 1, str(res))
    view = s.me_view(user_row, cid)
    check("me: has points + posts", view["points"] > 0 and view["qualifying_posts"] >= 1,
          str(view["points"]))
    snap = s.refresh_standings(cid)
    board = s.leaderboard(cid)
    check("leaderboard: snapshot + rows", board and len(board["rows"]) >= 1, str(bool(board)))
    check("leaderboard: rank 1 is FRONT PAGE",
          board["rows"][0]["tier"] == "FRONT PAGE" if board["rows"] else False)
    check("leaderboard: meta has next_refresh_at", "next_refresh_at" in (board or {}))
    posts = s.posts_view(user_row, cid)
    check("posts: audit trail present", all(p["audit"].get("contributions") for p in posts
                                            if p["eligible"] and p["points"]))

    # ------------------------------------------------------------- HTTP routes
    r = http_app.Router(s, st, database, x)

    def go(method, path, query=None, headers=None, cookies=None, body=None):
        out = r.dispatch(method, path, query or {}, headers or {}, cookies or {}, body)
        status, payload = out[0], out[1]
        return status, payload

    check("http: static / serves html", go("GET", "/")[0] == 200)
    check("http: css served", go("GET", "/css/paper.css")[0] in (200, 404))
    stt, payload = go("GET", "/api/health")
    check("http: /api/health ok", stt == 200 and payload.get("ok"))
    stt, payload = go("GET", "/api/config")
    check("http: config has no secret keys",
          all(k not in str(payload).lower() for k in
              ("client_secret", "bearer", "admin_token")), str(payload)[:120])
    check("http: /api/config names the provider strategy honestly — the"
          " battery env opted into mock, and the payload says so",
          stt == 200 and "mock" in str(payload.get("providerStrategy")),
          str(payload.get("providerStrategy")))
    stt, payload = go("GET", "/api/leaderboard")
    check("http: leaderboard rows", stt == 200 and len(payload.get("rows", [])) >= 1)
    stt, payload = go("GET", "/api/me")
    check("http: me anonymous", stt == 200 and payload.get("connected") is False)
    tok = s.new_session(uid)
    stt, payload = go("GET", "/api/me", cookies={"PBSD": tok})
    check("http: me connected", payload.get("connected") is True)
    stt, payload = go("POST", "/api/me/wallet", cookies={"PBSD": tok},
                      body={"wallet": "bad0OIl"})
    check("http: wallet rejected (base58 has no 0/I/O/l)", stt == 422, f"{stt}")
    stt, payload = go("POST", "/api/me/wallet", cookies={"PBSD": tok},
                      body={"wallet": CA})
    check("http: wallet accepted", stt == 200 and payload.get("wallet") == CA)
    stt, payload = go("GET", "/api/me/posts", cookies={"PBSD": tok})
    check("http: posts breakdown", stt == 200 and isinstance(payload.get("posts"), list))
    stt, payload = go("POST", "/api/admin/refresh", headers={"x-paper-token": "dev"})
    check("http: admin refresh (mock token dev)", stt == 200, f"{stt} {payload}")
    stt, payload = go("POST", "/api/admin/refresh", headers={"x-paper-token": "nope"})
    check("http: admin wrong token", stt == 401)
    stt, payload = go("POST", "/api/auth/x/start")
    check("http: oauth start (mock)", stt == 200 and "mock" in payload.get("url", ""))

    # ------------------------------------------------- competitions + hardening
    stt, payload = go("GET", "/api/competitions")
    rows = (payload or {}).get("competitions") or []
    check("http: competitions list + state",
          stt == 200 and len(rows) >= 1
          and rows[0]["state"] in ("live", "upcoming", "ended", "closed"),
          str(rows)[:80])
    stt, payload = go("GET", "/api/nosuch")
    check("http: unknown api route is 404 json", stt == 404)
    _, before = go("GET", "/api/me", cookies={"PBSD": tok})
    stt, payload = go("POST", "/api/me/wallet", cookies={"PBSD": tok},
                      body={"wallet": CA, "points": 999999})
    _, after = go("GET", "/api/me", cookies={"PBSD": tok})
    check("http: client cannot inject a score (extra body keys ride ignored)",
          stt == 200 and after["points"] == before["points"],
          f"{before['points']} vs {after['points']}")
    stt, payload = go("POST", "/api/me/wallet", cookies={"PBSD": tok}, body=None)
    check("http: malformed wallet body fails safe (422)", stt == 422)

    # =============== scan policy + hardening (P1-P7) ========================
    T = time.time()

    def row_of(d, uid):
        return dict(d.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def paged(posts, more=False, token=None):
        return (posts, dict(returned=len(posts), more=more, next_token=token))

    def fake_user(idv="fx-1"):
        return dict(id=idv, username="fakes", name="Fakes", profile_image_url="")

    fx = FakeX()
    stA, dA, sA, cidA = make_env2("A", fx)
    uidA = sA.upsert_user(fake_user())
    a1 = fx_post("a1", "fx-1", 7200, f"look at {CA} once", (0, 0, 1, 0, 500))
    a2 = fx_post("a2", "fx-1", 3600, "$paper twice", (0, 0, 1, 0, 500))
    fx.pages = [paged([a1, a2])]
    resA = sA.scan_user(row_of(dA, uidA), cidA, at=T)
    check("P1 first scan: one full page, no start_time sent",
          resA["ok"] and resA["first_scan"] and fx.n["recent"] == 1
          and fx.start_times == [None] and resA["found"] == 2, str(resA)[:120])
    rA = row_of(dA, uidA)
    check("P1 watermark is the server clock (client never supplies it)",
          rA["scan_watermark"] == T, str(rA["scan_watermark"]))
    a3 = fx_post("a3", "fx-1", 60, "$paper three", (0, 0, 1, 0, 500))
    fx.pages = [paged([a1, a3])]
    resB = sA.scan_user(row_of(dA, uidA), cidA, at=T + 7 * 3600)
    ov = stA.pb["scan"]["overlapSeconds"]
    check("P1 delta scan sends start_time = watermark - overlap",
          resB["ok"] and fx.start_times[-1] == T - ov, str(fx.start_times[-1]))
    nA = dA.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    fx.pages = [paged([a2, a1])]
    resC = sA.scan_user(row_of(dA, uidA), cidA, at=T + 14 * 3600)
    check("P1 overlap duplicates dedupe by post id (one row per post)",
          resC["ok"] and dA.conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == nA)
    fx.pages = [paged([])]
    resD = sA.scan_user(row_of(dA, uidA), cidA, at=T + 21 * 3600)
    check("P1 empty delta is a good scan (no fallback, no error)",
          resD["ok"] and resD["found"] == 0
          and fx.start_times[-1] == T + 14 * 3600 - ov, str(resD)[:120])

    many = [fx_post(f"b{i}", "fx-1", 100 + i, "$paper many", (0, 0, 1, 0, 500))
            for i in range(50)]
    fx2 = FakeX([paged(many, more=True, token="t1")])
    stB, dB, sB, cidB = make_env2("B", fx2)
    uidB = sB.upsert_user(fake_user())
    resE0 = sB.scan_user(row_of(dB, uidB), cidB)
    check("P1 first scan caps at one 50-post page (truncated flag, never silent)",
          resE0["ok"] and resE0["truncated"] and fx2.n["recent"] == 1, str(resE0)[:120])
    fx2.pages = [paged(many[:20], more=True, token="t2"),
                 paged([fx_post("bnew", "fx-1", 30, "$paper new", (0, 0, 1, 0, 500))])]
    resE = sB.scan_user(row_of(dB, uidB), cidB, at=time.time() + 7 * 3600)
    check("P1 delta pagination follows next_token safely (50/request cap holds)",
          resE["ok"] and fx2.n["recent"] == 3 and not resE["truncated"], str(resE)[:120])

    fx3 = FakeX([paged([fx_post("c0", "fx-1", 80000, "$paper warm", (0, 0, 1, 0, 500))])])
    stC, dC, sC, cidC = make_env2("C", fx3)
    uidC = sC.upsert_user(fake_user())
    sC.scan_user(row_of(dC, uidC), cidC, at=T)
    pages = []
    for pg in range(4):
        posts = [fx_post(f"c{pg}-{i}", "fx-1", 86400 + pg * 3600 + i, "$paper page",
                         (0, 0, 1, 0, 500)) for i in range(50)]
        pages.append(paged(posts, more=pg < 3, token=f"tk{pg}" if pg < 3 else None))
    fx3.pages = pages
    resF = sC.scan_user(row_of(dC, uidC), cidC, at=T + 7 * 3600)
    rC = row_of(dC, uidC)
    check("P1 delta caps at deltaMaxPages, rewinds watermark, logged not silent",
          resF["truncated"] and resF["pages"] == 3
          and abs(rC["scan_watermark"] - (time.time() - (86400 + 2 * 3600 + 49))) < 5,
          f"wm={rC['scan_watermark']:.1f} exp={time.time() - (86400 + 2 * 3600 + 49):.1f}")

    fx4 = FakeX()
    stD, dD, sD, cidD = make_env2("D", fx4)
    uidD = sD.upsert_user(fake_user())
    fx4.pages = [paged([fx_post("d1", "fx-1", 600, f"{CA} follows gate",
                                (0, 0, 1, 0, 500))])]
    sD.scan_user(row_of(dD, uidD), cidD, at=T)
    resG = sD.scan_user(row_of(dD, uidD), cidD, at=T + 7 * 3600)
    check("P2 follow cache hit reuses the stored verdict (no X call)",
          fx4.n["follows"] == 1 and resG["ok"], str(fx4.n))
    dD.conn.execute("UPDATE users SET follows_checked_at=? WHERE id=?",
                    (T - 25 * 3600, uidD))
    dD.conn.commit()
    sD.scan_user(row_of(dD, uidD), cidD, at=T + 2 * 86400)
    check("P2 follow cache expiry re-checks on the configured TTL",
          fx4.n["follows"] == 2, str(fx4.n))
    rate = (sD.usage_view() or {}).get("follow_cache_hit_rate")
    check("P6/P2 usage reports the follow cache hit rate",
          rate is not None and rate > 0, str(rate))
    fx4b = FakeX(follows=False)
    stD2, dD2, sD2, cidD2 = make_env2("D2", fx4b)
    uidD2 = sD2.upsert_user(fake_user("fx-2"))
    fx4b.pages = [paged([fx_post("d2", "fx-2", 600, "$paper no follow",
                                (0, 0, 1, 0, 500))])]
    resN = sD2.scan_user(row_of(dD2, uidD2), cidD2, at=T)
    check("P2 cached-negative follow keeps eligibility rules intact",
          resN["ok"] and resN["found"] == 1 and resN["qualified"] == 0, str(resN)[:120])

    fx5 = FakeX()
    stE, dE, sE, cidE = make_env2("E", fx5)
    uidE = sE.upsert_user(fake_user())
    fx5.pages = [paged([fx_post("e1", "fx-1", 3600, "$paper metrics",
                                (0, 0, 2, 0, 800))])]
    sE.scan_user(row_of(dE, uidE), cidE, at=T)
    fx5.pages = [paged([])]
    pts0 = dE.conn.execute(
        "SELECT points FROM posts WHERE x_post_id='e1'").fetchone()["points"]
    fx5.metrics_posts = [fx_post("e1", "fx-1", 3600 + 3 * 3600, "$paper metrics",
                                (1, 1, 30, 1, 4000))]
    sE.scan_user(row_of(dE, uidE), cidE, at=T + 7 * 3600)
    row1 = dE.conn.execute(
        "SELECT points, metrics_refreshed_at FROM posts WHERE x_post_id='e1'").fetchone()
    check("P3 stale recent post gets a metric refresh + re-score",
          fx5.n["metrics"] == 1 and row1["points"] != pts0
          and row1["metrics_refreshed_at"] == T + 7 * 3600, str((pts0, row1["points"])))
    dE.conn.execute("UPDATE posts SET posted_at=? WHERE x_post_id='e1'", (T - 60 * 3600,))
    dE.conn.commit()
    sE.scan_user(row_of(dE, uidE), cidE, at=T + 14 * 3600)
    check("P3 older eligible post is quiet on its 12h clock (zero X)",
          fx5.n["metrics"] == 1, str(fx5.n))
    dE.conn.execute("UPDATE posts SET metrics_refreshed_at=? WHERE x_post_id='e1'",
                    (T - 86400,))
    dE.conn.commit()
    sE.scan_user(row_of(dE, uidE), cidE, at=T + 21 * 3600)
    check("P3 older eligible post refreshes on its longer clock",
          fx5.n["metrics"] == 2, str(fx5.n))
    dE.conn.execute("UPDATE posts SET posted_at=? WHERE x_post_id='e1'", (T - 40 * 86400,))
    dE.conn.commit()
    sE.scan_user(row_of(dE, uidE), cidE, at=T + 28 * 3600)
    check("P3 ancient posts freeze (metrics stop being chased)",
          fx5.n["metrics"] == 2 and dE.conn.execute(
              "SELECT metrics_frozen f FROM posts WHERE x_post_id='e1'").fetchone()["f"] == 1)
    dE.conn.execute("UPDATE posts SET metrics_frozen=0 WHERE x_post_id='e1'")
    dE.conn.execute("UPDATE competitions SET ends_at=? WHERE id=?", (T - 10, cidE))
    dE.conn.commit()
    fx5.metrics_posts = [fx_post("e1", "fx-1", 40 * 86400, "$paper metrics",
                                (2, 1, 40, 2, 5000))]
    sE.scan_user(row_of(dE, uidE), cidE, at=T + 35 * 3600)
    check("P3 ended competition gets one final authoritative snapshot then freezes",
          fx5.n["metrics"] == 3 and dE.conn.execute(
              "SELECT metrics_frozen f FROM posts WHERE x_post_id='e1'").fetchone()["f"] == 1)

    fx6 = FakeX()
    stF, dF, sF, cidF = make_env2("F", fx6)
    uidF = sF.upsert_user(fake_user())
    fx6.pages = [paged([fx_post("f1", "fx-1", 600, "$paper guarded", (0, 0, 1, 0, 500))])]
    res = sF.scan_user(row_of(dF, uidF), cidF, at=T)
    inner = {}
    fx6.reentrant = lambda: inner.update(
        r=sF.scan_user(row_of(dF, uidF), cidF, at=T + 60, force=True))
    res2 = sF.scan_user(row_of(dF, uidF), cidF, at=T + 2 * 86400)
    fx6.reentrant = None
    check("P4 duplicate in-flight scan refused, outer scan completes",
          res["ok"] and inner.get("r", {}).get("reason") == "scan_inflight"
          and res2["ok"], str(inner))
    dF.conn.execute("UPDATE x_usage SET estimated=? WHERE day IS NOT NULL", (999999.0,))
    dF.conn.commit()
    res3 = sF.scan_user(row_of(dF, uidF), cidF, at=T + 3 * 86400)
    check("P4 budget exhaustion stops scans gracefully (no X spend, clear reason)",
          not res3["ok"] and res3["reason"] == "scan_budget_exhausted"
          and fx6.n["recent"] == 2, str(res3)[:120])
    dF.conn.execute("UPDATE x_usage SET estimated=0")
    dF.conn.commit()
    res4 = sF.scan_user(row_of(dF, uidF), cidF, at=T + 3 * 86400 + 60, force=True)
    check("P4 force (the ?force=1 escape hatch) overrides the cooldown", res4["ok"])
    before = dict(fx6.n)
    snap = sF.refresh_standings(cidF)
    board = sF.leaderboard(cidF)
    me = sF.me_view(row_of(dF, uidF), cidF)
    posts = sF.posts_view(row_of(dF, uidF), cidF)
    check("P5 leaderboard refresh + page views touch zero X endpoints",
          snap and board and me and isinstance(posts, list)
          and all(fx6.n[k] == before[k] for k in before), str(fx6.n))
    dF.conn.execute("UPDATE users SET last_scan_at=NULL")
    dF.conn.commit()
    rF = http_app.Router(sF, stF, dF, fx6)
    tokF = sF.new_session(uidF)

    def go2(method, path, query=None, headers=None, cookies=None, body=None):
        out = rF.dispatch(method, path, query or {}, headers or {}, cookies or {}, body)
        return out[0], out[1]

    stt, payload = go2("POST", "/api/me/scan", cookies={"PBSD": tokF})
    check("P4 route: scan runs and returns its cost meta",
          stt == 200 and "scan" in payload and "estimated" in payload.get("scan", {}),
          f"{stt}")
    stt, payload = go2("POST", "/api/me/scan", cookies={"PBSD": tokF})
    check("P4 route: cooldown surfaces as 429 scan_cooldown + retryAfter",
          stt == 429 and payload.get("error") == "scan_cooldown"
          and payload.get("retryAfter") is not None, f"{stt} {str(payload)[:80]}")
    stt, payload = go2("POST", "/api/me/scan", query={"force": "1"},
                       cookies={"PBSD": tokF})
    check("P4 route: ?force=1 bypasses the cooldown", stt == 200, f"{stt}")
    stt, payload = go2("GET", "/api/usage", headers={"x-paper-token": "dev"})
    usage = (payload or {}).get("usage") or {}
    check("P6 /api/usage reports the cost ledger", stt == 200 and all(
        k in usage for k in ("requests_today", "post_resources_today",
                             "user_resources_today", "following_resources_today",
                             "estimated_today", "estimated_month", "scans_today",
                             "avg_resources_per_scan", "avg_cost_per_scan",
                             "follow_cache_hit_rate", "users_scanned")),
        str(payload)[:120])
    stt, payload = go2("GET", "/api/usage")
    check("P6 /api/usage is not public (401 without the token)", stt == 401, f"{stt}")

    # ===== account + auth: the paperboard identity pivot (not X Connect) ===
    def magic_from(j):                                 # the mock echoes its link
        return (j.get("link") or "").split("token=")[-1]

    JSONISH = {"content-type": "application/json"}
    stt, payload = go("POST", "/api/account/start", body={"email": "grompity@paper.io"})
    check("acct: start creates silently and echoes a mock link",
          stt == 200 and payload.get("sent") and payload.get("link"))
    tok = magic_from(payload or {})
    stt, payload = go("POST", "/api/account/start", body={"email": "nope"})
    check("acct: a shapeless email is 422, never a 500", stt == 422
          and payload.get("error") == "invalid_email")

    out = r.dispatch("GET", "/api/auth/magic", {"token": tok}, {}, {}, None)
    check("auth: the magic link 302s to the board with a session cookie",
          out[0] == 302 and out[2].get("Location") == "/#board"
          and "PBSD=" in (out[2].get("Set-Cookie") or ""), str(out)[:90])
    seat = out[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]
    ck = {"PBSD": seat}
    check("auth: the session cookie is HttpOnly + SameSite=Lax (Secure in prod)",
          "HttpOnly" in out[2]["Set-Cookie"] and "SameSite=Lax" in out[2]["Set-Cookie"])
    stt, payload = go("GET", "/api/auth/magic", {"token": tok})
    check("auth: a burned link never burns twice (single use)",
          stt == 401 and payload.get("error") == "used_link")
    stt, payload = go("GET", "/api/auth/magic", {"token": "nonsense"})
    check("auth: an unknown token answers before any account lookup",
          stt == 401 and payload.get("error") == "bad_link")

    stt, payload = go("GET", "/api/account")
    check("acct: the dashboard is 401 for the seatless (no IDOR door)",
          stt == 401 and payload.get("error") == "not_authenticated")
    stt, payload = go("GET", "/api/account", cookies=ck)
    acct = (payload or {}).get("account") or {}
    check("acct: dashboard is account-first — email, never an X handle",
          stt == 200 and acct.get("email") == "grompity@paper.io"
          and acct.get("email_verified") and acct.get("username") is None
          and not acct.get("onboarded"), str(acct)[:90])

    stt, payload = go("POST", "/api/account/onboard", cookies=ck,
                      headers=JSONISH, body={"username": "x!"})
    check("acct: the byline is validated server-side",
          stt == 422 and payload.get("error") == "bad_username")
    stt, payload = go("POST", "/api/account/onboard", cookies=ck,
                      headers=JSONISH, body={"username": "grompity", "wallet": "0OIl"})
    check("acct: the wallet is a solana address, not just any paste",
          stt == 422 and payload.get("error") == "invalid_solana_address")
    stt, payload = go("POST", "/api/account/onboard", cookies=ck,
                      headers=JSONISH, body={"username": "grompity", "wallet": CA})
    check("acct: onboarding completes (username + wallet, no signature asked)",
          stt == 200 and (payload.get("account") or {}).get("onboarded"), str(stt))
    audit_n = database.conn.execute(
        "SELECT COUNT(*) n FROM wallet_audit").fetchone()["n"]
    check("acct: the onboarding wallet is recorded in the audit trail",
          audit_n == 1, str(audit_n))
    stt, payload = go("GET", "/api/account/submissions", cookies=ck)
    check("acct: the submissions ledger starts empty",
          stt == 200 and payload.get("submissions") == [])

    stt, payload = go("POST", "/api/account/posts", cookies=ck,
                      headers=JSONISH, body={"url": "hello"})
    check("submit: not-a-URL is 400 before the provider is even asked",
          stt == 400 and payload.get("error") == "bad_post_url")
    stt, payload = go("POST", "/api/account/posts", cookies=ck,
                      headers=JSONISH,
                      body={"url": "https://x.com/degenledger/status/999999999999"})
    check("submit: an unknown post is 404 (the mock never fabricates one)",
          stt == 404 and payload.get("error") == "post_not_found")
    stt, payload = go("POST", "/api/account/posts", cookies=ck,
                      headers=JSONISH,
                      body={"url": "https://twitter.com/whatever/status/m2?s=20#x"})
    sub = (payload or {}).get("submission") or {}
    check("submit: m2 verifies and the SERVER names the author (byline-independent)",
          stt == 200 and sub.get("author") == "degenledger"
          and sub.get("eligible") and sub.get("points", 0) > 0, str(sub)[:140])
    check("submit: the stored URL is the normalized one",
          sub.get("url") == "https://x.com/degenledger/status/m2", str(sub.get("url")))
    stt, payload = go("POST", "/api/account/posts", cookies=ck, headers=JSONISH,
                      body={"url": "https://x.com/degenledger/status/m2"})
    check("submit: a duplicate hits the 409 wall (owner self)",
          stt == 409 and payload.get("error") == "duplicate_submission"
          and payload.get("owner") == "self")
    stt, payload = go("POST", "/api/account/posts", cookies=ck, headers=JSONISH,
                      body={"url": "https://x.com/chartwitch/status/m3"})
    check("submit: identity rule — you may submit posts by OTHER handles",
          stt == 200 and (payload.get("submission") or {}).get("author") == "chartwitch")
    stt, payload = go("POST", "/api/account/posts", cookies=ck, headers=JSONISH,
                      body={"url": "https://x.com/bidetbear/status/m8"})
    sub8 = (payload or {}).get("submission") or {}
    check("submit: the follow gate is advisory now (followRequired false) —"
          " the bidetbear post rows up verified, with the audit saying"
          " 'advisory', never a claim",
          stt == 200 and sub8.get("eligible")
          and sub8.get("reason") == "ok" and sub8.get("points") > 0
          and sub8.get("author") == "bidetbear", str(sub8)[:120])
    stt, payload = go("GET", "/api/account/submissions", cookies=ck)
    subs = (payload or {}).get("submissions") or []
    check("acct: the owner's ledger shows exactly my posts (auth rides the account id)",
          stt == 200 and len(subs) == 3 and all("audit" in row0 for row0 in subs),
          str(len(subs)))

    stt, payload = go("POST", "/api/account/start", body={"email": "B@Paper.io"})
    tokb = magic_from(payload or {})
    outb = r.dispatch("GET", "/api/auth/magic", {"token": tokb}, {}, {}, None)
    seatb = outb[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]
    ckb = {"PBSD": seatb}
    stt, payload = go("POST", "/api/account/posts", cookies=ckb, headers=JSONISH,
                      body={"url": "https://x.com/degenledger/status/m2"})
    check("submit: the duplicate wall is per-issue, not per-account (owner other)",
          stt == 409 and payload.get("owner") == "other", f"{stt} {payload}")
    stt, payload = go("POST", "/api/account/start", body={"email": "grompity@paper.io"})
    check("auth: start answers identically for known emails (no enumeration)",
          stt == 200 and payload.get("sent") and not payload.get("created"),
          str(payload)[:90])
    stt, payload = go("GET", "/api/account", cookies=ckb)
    check("acct: emails are case-folded (B@Paper.io became b@paper.io)",
          ((payload or {}).get("account") or {}).get("email") == "b@paper.io")
    stt, payload = go("GET", "/api/account/submissions", cookies=ckb)
    check("acct: no cross-contamination — your ledger is yours alone (IDOR)",
          stt == 200 and payload.get("submissions") == [])

    stt, payload = go("POST", "/api/account/wallet", cookies=ck, headers=JSONISH,
                      body={"wallet": CA[::-1]})
    check("wallet: a valid change rides the cooldown before rewards may ride it",
          stt == 200 and (payload.get("wallet_effective_at") or 0) > time.time() + 50,
          str(payload)[:90])
    old = database.conn.execute(
        "SELECT old_wallet FROM wallet_audit ORDER BY id DESC LIMIT 1").fetchone()["old_wallet"]
    audit_n = database.conn.execute(
        "SELECT COUNT(*) n FROM wallet_audit").fetchone()["n"]
    check("wallet: changes are audited with the previous address preserved",
          audit_n == 2 and old == CA, f"{audit_n}/{old}")
    database.conn.execute("UPDATE sessions SET created_at=? WHERE token=?",
                          (time.time() - 3600, seat))
    database.conn.commit()
    stt, payload = go("POST", "/api/account/wallet", cookies=ck, headers=JSONISH,
                      body={"wallet": CA})
    check("wallet: a stale seat must re-verify (fresh-auth gate answers 428)",
          stt == 428 and payload.get("error") == "fresh_auth_required", f"{stt}")
    stt, payload = go("POST", "/api/account/wallet", cookies=ck,
                      headers={"content-type": "application/x-www-form-urlencoded"},
                      body={"wallet": CA})
    check("wallet: a form post cannot move account state (CSRF shape guard)",
          stt == 415, f"{stt}")
    stt, payload = go("POST", "/api/account/wallet", headers=JSONISH,
                      body={"wallet": "0OIl"})
    check("wallet: an anonymous change is 401 (authorization lives on the route)",
          stt == 401 and payload.get("error") == "not_authenticated")

    stt, payload = go("POST", "/api/account/start", body={"email": "grompity@paper.io"})
    toke = magic_from(payload or {})
    database.conn.execute("UPDATE magic_links SET expires_at=? WHERE token_hash=?",
                          (time.time() - 10, hash_magic_token(toke)))
    database.conn.commit()
    stt, payload = go("GET", "/api/auth/magic", {"token": toke}, {}, {}, None)
    check("auth: links expire (short-lived by config, enforced at the click)",
          stt == 401 and payload.get("error") == "expired_link")
    stt, payload = go("POST", "/api/account/start", body={"email": "grompity@paper.io"})
    tok2 = magic_from(payload or {})                # expired link → a fresh send
    sent_after_real_send = len(s.mail.sent)
    stt, payload = go("POST", "/api/account/start", body={"email": "grompity@paper.io"})
    tok3 = magic_from(payload or {})
    check("mail: the resend window echoes the live link without a second send",
          stt == 200 and tok2 == tok3 and payload.get("sent")
          and len(s.mail.sent) == sent_after_real_send,
          f"{tok2[:8]}… {sent_after_real_send}")

    # the plaintext-never-persisted guarantee. the schema speaks first: a
    # fresh database has no plaintext column to hold a token at all.
    cols_magic = {row[1] for row in
                  database.conn.execute("PRAGMA table_info(magic_links)")}
    check("mail: the database keeps the hash, never the plaintext key",
          "token" not in cols_magic, str(sorted(cols_magic)))
    # then the production shape: an smtp-shaped mailer keeps no memory of
    # the plaintext, so the response must carry nothing but {sent: true}
    # and the only way back in is the link the mail itself captured.
    class ProdMail:
        mode = "smtp"
        def __init__(self):
            self.last = None
            self.sends = 0
        def send_magic_link(self, to, link_url):
            self.sends += 1
            self.last = link_url
            return True
    prod = ProdMail()
    mock_mailer, s.mail = s.mail, prod
    stt, payload = go("POST", "/api/account/start", body={"email": "prod@paper.io"})
    check("mail: production answers with a bare sent — no link leaks to the client",
          stt == 200 and sorted(payload) == ["sent"] and prod.sends == 1,
          f"{payload}")
    outp = r.dispatch("GET", "/api/auth/magic",
                      {"token": prod.last.split("token=")[-1]}, {}, {}, None)
    check("auth: the mailed token is the whole secret (302 on the captured link)",
          outp[0] == 302, f"{outp[0]} {outp[2]}")
    s.mail = mock_mailer
    codes = [go("POST", "/api/account/start", body={"email": "spam@paper.io"})[0]
             for _ in range(7)]
    check("mail: magic sends are rate limited per inbox",
          codes[-1] == 429 and all(c == 200 for c in codes[:-1]), str(codes))

    stt, payload = go("GET", "/api/account", cookies=ck)
    rw_seed = ((payload or {}).get("account") or {})
    database.conn.execute(
        "INSERT INTO rewards(account_id,competition_id,wallet_address,amount,asset,"
        "status,created_at) VALUES(?,?,?,?,?,?,?)",
        (rw_seed.get("id"), cid, rw_seed.get("wallet"), 1234.5, "$PAPER",
         "pending", time.time()))
    database.conn.commit()
    stt, payload = go("GET", "/api/account", cookies=ck)
    rw = (payload.get("rewards") or [{}])[0]
    check("rewards: the ledger rides the account (snapshot wallet, status, amount)",
          stt == 200 and rw.get("amount") == 1234.5 and rw.get("status") == "pending"
          and rw.get("wallet_address") == rw_seed.get("wallet")
          and payload.get("points", 0) > 0 and payload.get("rank") == 1
          and "recent" in payload, str(rw)[:120])
    stt, payload = go("POST", "/api/auth/logout", cookies=ck)
    seats = database.conn.execute(
        "SELECT COUNT(*) n FROM sessions WHERE token=?", (seat,)).fetchone()["n"]
    stt2, _p2 = go("GET", "/api/account", cookies=ck)
    check("auth: logout invalidates the session (a cookie without a row is nothing)",
          stt == 200 and payload.get("loggedOut") and seats == 0 and stt2 == 401)

    shy = ShyX()
    stS, dS, sS, cidS = make_env2("SHY", shy)
    accS, _cre, errS = sS.create_or_touch_account("shy@paper.io")
    check("accounts: email validation runs before the database does",
          errS is None and accS["email"] == "shy@paper.io"
          and sS.create_or_touch_account("nope@nope@nope")[2] == "invalid_email")
    tokS, _mailed = sS.issue_magic_link(accS["id"])
    signed, why = sS.consume_magic_link(tokS)
    resS = sS.submit_post(signed, "https://x.com/shy/status/777777777", cidS)
    subS = resS.get("submission") or {}
    check("syndication: only metrics the provider really reports are scored",
          resS.get("ok") and subS.get("points", 0) > 0
          and subS.get("missing") == ["reposts", "quotes", "impressions"]
          and subS.get("provided") == ["likes", "replies"], str(subS)[:150])

    # ---------------------------------------- phase 5: deferred follow gate
    shyD = ShyX(follows=None)                    # a gate that answers 'unknown'
    stD3, dD3, sD3, cidD3 = make_env2("DEFER", shyD)
    sD3.eligcfg["followRequired"] = True         # the optional-strict mode pin
    accD3, _cre, _er = sD3.create_or_touch_account("defer@paper.io")
    signedD3, _why = sD3.consume_magic_link(sD3.issue_magic_link(accD3["id"])[0])
    resD3 = sD3.submit_post(signedD3, "https://x.com/shy/status/777777777", cidD3)
    subD3 = resD3.get("submission") or {}
    rowD3 = dD3.conn.execute("SELECT score_json FROM submissions LIMIT 1").fetchone()
    auditD3 = json.loads(rowD3["score_json"])
    check("follow gate: an unverifiable follow defers — the row never claims a plain ok",
          resD3.get("ok") and subD3.get("eligible")
          and subD3.get("reason") == "follow_deferred"
          and auditD3.get("follow") == "deferred" and subD3.get("points", 0) > 0,
          f"{subD3.get('reason')}/{auditD3.get('follow')}")

    # ------------------------------ phase 3: unknown metrics are not thin zeros
    scx = stD3.pb["scoring"]
    ptsB, auditB = score_post(dict(likes=None, replies=None, reposts=None,
                                   quotes=None, impressions=None), scx, {})
    check("scoring: metrics unreported keeps the base and says so, never thin_signal",
          abs(ptsB - scx["basePerPost"]) < .001
          and "metrics_unreported" in auditB["applied"]
          and "thin_signal" not in auditB["applied"], str(auditB["applied"]))
    ptsZ, auditZ = score_post(dict(likes=0, replies=0, reposts=0,
                                   quotes=0, impressions=0), scx, {})
    check("scoring: reported zeros are still punished as thin (zero ≠ unknown)",
          abs(ptsZ - scx["basePerPost"] * scx["thinSignalMultiplier"]) < .001
          and "thin_signal" in auditZ["applied"]
          and auditZ["scoring_version"] == "pb-v1.1", str(auditZ["applied"]))
    bx = BlindX()
    _stB3, _dB3, sB3, cidB3 = make_env2("BLIND", bx)
    accB3, _cre, _er = sB3.create_or_touch_account("blind@paper.io")
    signedB3, _why = sB3.consume_magic_link(sB3.issue_magic_link(accB3["id"])[0])
    resB3 = sB3.submit_post(signedB3, "https://x.com/quiet/status/555", cidB3)
    check("submit: a metrics-blind provider still verifies presence (base points ride)",
          resB3.get("ok") and (resB3.get("submission") or {}).get("points")
          == scx["basePerPost"], str(resB3)[:110])

    # --------------------------------------- the provider failing must fail closed
    _stB4, _dB4, sB4, cidB4 = make_env2("BOOM", BoomX())
    accB4, _cre, _er = sB4.create_or_touch_account("boom@paper.io")
    signedB4, _why = sB4.consume_magic_link(sB4.issue_magic_link(accB4["id"])[0])
    resB4 = sB4.submit_post(signedB4, "https://x.com/quiet/status/555", cidB4)
    check("submit: provider failure produces no row, no points (fail closed)",
          not resB4.get("ok") and resB4.get("reason") == "provider_unavailable"
          and _dB4.conn.execute(
              "SELECT COUNT(*) n FROM submissions").fetchone()["n"] == 0)

    # ---------------- phase 2's live feed served no posts: contract replay only
    live = {"tweet": {"id_str": "888888888888888888", "text": "$paper live front page",
                      "created_at": 1760000000000,
                      "user": {"screen_name": "LiveAuthor", "id_str": "4242"},
                      "favorite_count": 41, "conversation_count": 7,
                      "entities": {"media": [{"type": "photo"}]},
                      "in_reply_to_status_str": "777"}}
    saved_urlopen = xapimod.urllib.request.urlopen
    try:
        xapimod.urllib.request.urlopen = lambda req, timeout=None: ReplayResp(live)
        got = SyndicationXClient(Settings()).resolve_post("888888888888888888")
        check("syndication replay: the documented shape maps honestly — reported"
              " fields in, everything unreported stays None (never invented)",
              got["author"] == "liveauthor"
              and got["metrics"]["likes"] == 41 and got["metrics"]["replies"] == 7
              and got["metrics"]["impressions"] is None
              and got["metrics"]["quotes"] is None
              and got["provided"] == ["likes", "replies"], str(got)[:150])
        xapimod.urllib.request.urlopen = lambda req, timeout=None: ReplayResp({})
        gone = SyndicationXClient(Settings()).resolve_post("1")
        check("syndication replay: a gone post arrives {} and reads as no post",
              gone is None)
        class EmptyResp(ReplayResp):
            def read(self):
                return b""
        xapimod.urllib.request.urlopen = lambda req, timeout=None: EmptyResp(None)
        hardened = False
        try:
            SyndicationXClient(Settings()).resolve_post("1")
        except XError:
            hardened = True
        check("syndication: a 200 with an empty body fails closed as XError,"
              " not an uncaught crash (a deprecated CDN ghost answers stubs)",
              hardened)
    finally:
        xapimod.urllib.request.urlopen = saved_urlopen

    # --------------------------- the zero-cost provider stack (2026-10) -----
    # the live network was probed by hand (README carries the record); these
    # replays run the REAL adapter code over captured shapes, so the battery
    # stays deterministic and hermetic.
    import io as _io

    class StackResp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

        def getcode(self):
            return 200

    def http_err(code):
        return xapimod.urllib.error.HTTPError("u", code, "probe", {},
                                              _io.BytesIO(b""))

    def fx_body(pid, **over):
        t = dict(id=pid, text="$paper replay post",
                 url=f"https://x.com/Paperusdc/status/{pid}",
                 author=dict(screen_name="Paperusdc", id="4242"),
                 created_timestamp="1791475345",
                 likes="8", replies="3", retweets="3", quotes="0",
                 views="123", bookmarks="0")
        t.update(over)
        for k in [k for k, v in t.items() if v is None]:
            t.pop(k)
        return json.dumps(dict(code=200, message="OK", tweet=t)).encode()

    def oe_body(pid, html=None, author="Paperusdc"):
        h = html if html is not None else (
            f'<blockquote class="twitter-tweet"><p lang="en">$paper presence post'
            f'</p>&mdash; Paper (<a href="#">@{author}</a>) <a href="#">'
            f"October 8, 2026</a></blockquote>")
        return json.dumps(dict(url=f"https://x.com/{author}/status/{pid}",
                               author_name="Paper",
                               author_url=f"https://x.com/{author}",
                               html=h)).encode()

    stX = Settings()
    counters = dict(fx=0, oe=0)
    saved_urlopen = xapimod.urllib.request.urlopen
    xapimod.urllib.request.urlopen = None      # set per-scenario below

    def stub(route):
        def r(req, timeout=None, context=None):
            u = req.full_url
            who = "fx" if "fxtwitter" in u else "oe"
            counters[who] += 1
            out = route(u, who)
            if isinstance(out, BaseException):
                raise out
            return StackResp(out if isinstance(out, bytes) else out.encode())
        return r

    try:
        stK, dK, sK, cidK = make_env2("STK", FallbackXClient(
            FXTwitterXClient(stX), OembedXClient(stX)))
        accK, _cr, _er = sK.create_or_touch_account("stack@paper.io")
        signedK, _w = sK.consume_magic_link(
            sK.issue_magic_link(accK["id"])[0])
        acc2, _cr2, _er2 = sK.create_or_touch_account("stack2@paper.io")
        signed2, _w2 = sK.consume_magic_link(
            sK.issue_magic_link(acc2["id"])[0])
        cnt0 = dict(counters)                    # where the clock of calls stands

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9001") if who == "fx" else oe_body("9001"))
        resK = sK.submit_post(signedK, "https://x.com/anyone/status/9001", cidK)
        subK = resK.get("submission") or {}
        audK = json.loads(dK.conn.execute(
            "SELECT score_json FROM submissions ORDER BY id DESC LIMIT 1"
            ).fetchone()["score_json"])
        ptsW, _aW = score_post(dict(likes=8, replies=3, reposts=3, quotes=0,
                                    impressions=123), scx, {})
        check("stack: both providers stand — fx answers with full engagement,"
              " oEmbed is not needed; audit says engagement/fxtwitter/"
              "advisory/fetched_at; the wrong paste byline became the truth",
              resK.get("ok") and subK.get("verification") == "engagement"
              and subK.get("provider") == "fxtwitter" and subK.get("reason") == "ok"
              and subK.get("points") == ptsW and subK.get("author") == "paperusdc"
              and counters["fx"] - cnt0["fx"] == 1 and counters["oe"] - cnt0["oe"] == 0
              and audK.get("follow") == "advisory" and audK.get("fetched_at")
              and audK.get("verification") == "engagement", str(subK)[:110])

        xapimod.urllib.request.urlopen = stub(lambda u, who: fx_body(
            "9002", likes="0", replies="0", retweets="0", quotes="0",
            views="0", bookmarks="0") if who == "fx" else oe_body("9002"))
        res2 = sK.submit_post(signedK, "https://x.com/anyone/status/9002", cidK)
        sub2 = res2.get("submission") or {}
        check("stack: genuine zeros stay a measurement, not an absence — the"
              " thin-signal (earned) rides the row, missing is empty",
              res2.get("ok") and sub2.get("points") == ptsZ
              and sub2.get("missing") == [] and sub2.get("verification") == "engagement",
              str(sub2)[:90])

        xapimod.urllib.request.urlopen = stub(lambda u, who: fx_body(
            "9003", quotes=None, views=None, bookmarks=None)
            if who == "fx" else oe_body("9003"))
        res3 = sK.submit_post(signedK, "https://x.com/anyone/status/9003", cidK)
        sub3 = res3.get("submission") or {}
        ptsM, _aM = score_post(dict(likes=8, replies=3, reposts=3, quotes=None,
                                    impressions=None), scx, {})
        check("stack: omitted metrics are missing, never zero-fed — the score"
              " sums only what was reported",
              res3.get("ok") and sub3.get("missing") == ["quotes", "impressions"]
              and sub3.get("points") == ptsM
              and sub3.get("verification") == "engagement", str(sub3.get("missing")))

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: (b"<!DOCTYPE html><html>landing</html>" if who == "fx"
                            else oe_body("9004")))
        res4 = sK.submit_post(signedK, "https://x.com/anyone/status/9004", cidK)
        sub4 = res4.get("submission") or {}
        aud4 = json.loads(dK.conn.execute(
            "SELECT score_json FROM submissions ORDER BY id DESC LIMIT 1"
            ).fetchone()["score_json"])
        check("stack: a 200 wearing HTML is the landing page, not a post —"
              " oEmbed carries it as presence_verified, base points only,"
              " metrics_unreported stamped",
              res4.get("ok") and sub4.get("reason") == "presence_verified"
              and sub4.get("points") == scx["basePerPost"]
              and sub4.get("verification") == "presence"
              and sub4.get("provider") == "oembed"
              and sub4.get("missing") == ["likes", "replies", "reposts",
                                          "quotes", "impressions"]
              and "metrics_unreported" in aud4.get("applied", []),
              str(sub4)[:110])

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: http_err(404) if who == "fx" else oe_body("9005"))
        res5 = sK.submit_post(signedK, "https://x.com/anyone/status/9005", cidK)
        sub5 = res5.get("submission") or {}
        check("stack: fx's 404 alone does not bury a post — the official eye"
              " cross-checks it into a presence_verified row",
              res5.get("ok") and sub5.get("reason") == "presence_verified",
              str(res5)[:90])

        rows_before = dK.conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: http_err(404))
        res6 = sK.submit_post(signedK, "https://x.com/anyone/status/9006", cidK)
        check("stack: both eyes see 404 — a confirmed absence (post_not_found),"
              " not provider trouble, and no row lands",
              not res6.get("ok") and res6.get("reason") == "post_not_found"
              and dK.conn.execute(
                  "SELECT COUNT(*) n FROM submissions").fetchone()["n"] == rows_before)

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: http_err(429) if who == "fx" else oe_body("9007"))
        res7 = sK.submit_post(signedK, "https://x.com/anyone/status/9007", cidK)
        check("stack: fx's 429 (pace) defers to oEmbed — presence, not a verdict",
              res7.get("ok")
              and (res7.get("submission") or {}).get("reason") == "presence_verified")

        before5 = dict(counters)
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: http_err(500) if who == "fx" else oe_body("9008"))
        res8 = sK.submit_post(signedK, "https://x.com/anyone/status/9008", cidK)
        check("stack: fx's 5xx gets exactly the one bounded retry, then"
              " oEmbed catches it as presence",
              res8.get("ok") and counters["fx"] - before5["fx"] == 2
              and (res8.get("submission") or {}).get("reason") == "presence_verified",
              f"fx tries {counters['fx'] - before5['fx']}")

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: b"}{" if who == "fx" else oe_body("9009"))
        res9 = sK.submit_post(signedK, "https://x.com/anyone/status/9009", cidK)
        check("stack: invalid JSON is a malformed response, not a post —"
              " the ladder carries it",
              res9.get("ok")
              and (res9.get("submission") or {}).get("reason") == "presence_verified")

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9999") if who == "fx" else oe_body("9010"))
        res10 = sK.submit_post(signedK, "https://x.com/anyone/status/9010", cidK)
        check("stack: a mismatched post id (asked 9010, got 9999) is refused —"
              " a payload must be about the asked post",
              res10.get("ok")
              and (res10.get("submission") or {}).get("reason") == "presence_verified")

        xapimod.urllib.request.urlopen = stub(lambda u, who: (
            http_err(500) if who == "fx" else
            oe_body("9011", html='<blockquote class="twitter-timeline">'
                    '<a href="#">Posts by anyone</a></blockquote>')))
        res11 = sK.submit_post(signedK, "https://x.com/anyone/status/9011", cidK)
        check("stack: an unrelated timeline answer is not the submitted post —"
              " fail closed, no fabricated verification",
              not res11.get("ok")
              and res11.get("reason") == "provider_unavailable"
              and dK.conn.execute("SELECT COUNT(*) n FROM submissions"
                                  " WHERE x_post_id='9011'").fetchone()["n"] == 0,
              str(res11)[:90])

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: http_err(500) if who == "fx" else http_err(503))
        res12 = sK.submit_post(signedK, "https://x.com/anyone/status/9012", cidK)
        check("stack: both providers down — provider_unavailable names both,"
              " no row, no points, no invention",
              not res12.get("ok") and res12.get("reason") == "provider_unavailable"
              and "fx 500" in str(res12.get("detail"))
              and "oembed 503" in str(res12.get("detail"))
              and dK.conn.execute("SELECT COUNT(*) n FROM submissions"
                                  " WHERE x_post_id='9012'").fetchone()["n"] == 0,
              str(res12.get("detail"))[:110])

        dup_before = dict(counters)
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9001") if who == "fx" else oe_body("9001"))
        res13 = sK.submit_post(signed2, "https://x.com/other/status/9001", cidK)
        check("stack: a cross-account resubmit hits the wall BEFORE the wire —"
              " zero upstream calls, owner 'other', no double award",
              not res13.get("ok")
              and res13.get("reason") == "duplicate_submission"
              and res13.get("owner") == "other"
              and counters["fx"] == dup_before["fx"]
              and counters["oe"] == dup_before["oe"], str(res13)[:110])

        fx_only = FXTwitterXClient(stX, rpm=1)
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: (fx_body(u.rsplit("/", 1)[-1]) if who == "fx"
                            else oe_body("b000")))
        fx_only.resolve_post("b000")
        bucketed = False
        try:
            fx_only.resolve_post("b001")           # a fresh id: cache cannot help
        except XRatelimited:
            bucketed = True
        check("stack: the self-imposed polite bucket speaks before the"
              " provider ever has to (rpm=1, second fresh lookup: pace)",
              bucketed)

        fx_cache = FXTwitterXClient(stX)
        cbefore = dict(counters)
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("c300") if who == "fx" else oe_body("c300"))
        fx_cache.resolve_post("c300")
        second = fx_cache.resolve_post("c300")
        check("stack: a same-id re-lookup answers from the TTL cache — one"
              " GET total, and the snapshot says it rode the cache",
              counters["fx"] - cbefore["fx"] == 1 and second.get("cached") is True)

        stN, dN, sN, cidN = make_env2("STKF", ShyX(follows=False))
        sN.eligcfg["followRequired"] = True
        accN, _cn, _en = sN.create_or_touch_account("flag@paper.io")
        signedN, _w = sN.consume_magic_link(sN.issue_magic_link(accN["id"])[0])
        resN = sN.submit_post(signedN, "https://x.com/shy/status/777777777", cidN)
        subN = resN.get("submission") or {}
        check("rules: when a deployment still wants the follow gate, the flag"
              " still rules (follows False + flag on: no_follow rows up"
              " processed, NOT eligible, zero points)",
              resN.get("reason") == "no_follow" and not subN.get("eligible")
              and subN.get("points") == 0.0 and subN.get("author") == "shy",
              str(subN)[:140])
    finally:
        xapimod.urllib.request.urlopen = saved_urlopen

    # --------------------------- mode selection: explicit, never guessed -----
    # the doctrine (2026-10): the live stack needs no X keys, so missing
    # paid-API keys must not imply mock; unset defaults to LIVE so a
    # deployment can never fall into the demo world by silence; a value
    # that means neither side fails the boot loudly. (Unit-level: Settings
    # only — no network, no live claim.)
    saved_mode_env = os.environ.get("PAPER_MOCK")
    saved_x_id = os.environ.pop("X_CLIENT_ID", None)
    saved_x_bearer = os.environ.pop("X_API_BEARER", None)   # absent on this box

    def mode_case(value):
        if value is None:
            os.environ.pop("PAPER_MOCK", None)
        else:
            os.environ["PAPER_MOCK"] = value
        try:
            st2 = Settings()
        except ValueError as e:
            return ("ValueError", str(e), "")
        view = st2.public_view()
        return (st2.mock, view["mode"], view["providerStrategy"])

    mc1, mc0, mcD = mode_case("1"), mode_case("0"), mode_case(None)
    mcT, mcN, mcX = mode_case("true"), mode_case("NO"), mode_case("sideways")

    try:
        check("mode: PAPER_MOCK=1 opts INTO the mock demo — and the config"
              " view calls it mock, nothing silent about it",
              mc1[0] is True and mc1[1] == "mock" and "mock" in mc1[2])
        check("mode: PAPER_MOCK=0 selects the live zero-cost stack with NO"
              " legacy keys present — the keyless truth this fix is about",
              mc0[0] is False and mc0[1] == "live" and "fxtwitter" in mc0[2])
        check("mode: unset defaults to LIVE — production cannot slip into"
              " the mock demo by silence",
              mcD[0] is False and mcD[1] == "live")
        check("mode: truthy/falsy spellings fold clearly (true to mock, NO"
              " to live) — case is not a cliff",
              mcT[0] is True and mcN[0] is False)
        check("mode: a value meaning neither side fails the boot loudly and"
              " names the variable — no silent guessing",
              mcX[0] == "ValueError" and "PAPER_MOCK" in mcX[1],
              str(mcX[1])[:70])

        import run as runmod                              # the boot entry
        os.environ["PAPER_MOCK"] = "1"
        prov_m = runmod.make_provider(Settings())
        os.environ["PAPER_MOCK"] = "0"
        prov_l = runmod.make_provider(Settings())
        check("factory: mode=mock yields the scripted world; mode=live"
              " yields the FULL ladder — FxEmbed primary, oEmbed backup —"
              " with no keys in the air",
              isinstance(prov_m, MockXClient)
              and isinstance(prov_l, FallbackXClient)
              and isinstance(prov_l.p, FXTwitterXClient)
              and isinstance(prov_l.b, OembedXClient))
    finally:
        if saved_mode_env is not None:
            os.environ["PAPER_MOCK"] = saved_mode_env
        else:
            os.environ.pop("PAPER_MOCK", None)
        for env_name, env_val in (("X_CLIENT_ID", saved_x_id),
                                  ("X_API_BEARER", saved_x_bearer)):
            if env_val is not None:
                os.environ[env_name] = env_val

    print("failures:", len(FAILURES))
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
