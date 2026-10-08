#!/usr/bin/env python3
"""PAPERBOARD self-tests — deterministic, stdlib only, no network.

  python3 server/selftest.py

Covers: eligibility truth table, scoring math + anti-gaming, prize models,
wallet validation, snapshot ranking/tiers, and every HTTP route (via direct
dispatch, no sockets). Exit code is the failure count.
"""

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
from paperboard.xapi import MockXClient                               # noqa: E402

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

    def resolve_post(self, post_id):
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
    check("submit: the follow gate prints its reason (verified post, zero points)",
          stt == 200 and not sub8.get("eligible") and sub8.get("reason") == "no_follow"
          and sub8.get("points") == 0.0, str(sub8)[:120])
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

    print("failures:", len(FAILURES))
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
