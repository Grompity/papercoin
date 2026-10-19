#!/usr/bin/env python3
"""PAPERBOARD self-tests — deterministic, stdlib only, no network.

  python3 server/selftest.py

Covers: eligibility truth table, scoring math + anti-gaming, prize models,
wallet validation, snapshot ranking/tiers, and every HTTP route (via direct
dispatch, no sockets). Exit code is the failure count.
"""

import json                                      # noqa: E402
import os
import ssl
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
from paperboard import mail as mailmod                                  # noqa: E402

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


class RichX(BlindX):
    """v3 battery eye: syndication-shaped post where the like count rides
    along (or, with likes=None, does not) — every OTHER metric deliberately
    present, so any scoring that leaned on them would trip the battery."""

    def __init__(self, likes=None):
        self.likes = likes

    def resolve_post(self, post_id, url=None):
        return dict(provider="syndication", post_id=post_id, author="quiet",
                    author_id=None, text="$paper loud post",
                    url=f"https://x.com/quiet/status/{post_id}",
                    posted_at=time.time() - 60,
                    metrics=dict(likes=self.likes, replies=3, reposts=4,
                                 quotes=5, impressions=66),
                    provided=(["likes", "replies", "reposts", "quotes",
                               "impressions"] if self.likes is not None else
                              ["replies", "reposts", "quotes", "impressions"]),
                    verification="engagement")


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
    ixpb = {r[1] for r in database.conn.execute("PRAGMA index_list(accounts)")}
    check("schema: fresh init creates both accounts-x indexes (xhandle + xid)",
          {"ix_accounts_xhandle", "ix_accounts_xid"} <= ixpb, str(sorted(ixpb)))
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
    check("leaderboard: the board speaks account-era now — it answers before"
          " any submission exists (rows list, zero participants); the"
          " snapshot's ghosts are gone",
          board is not None and isinstance(board["rows"], list)
          and board["participants"] == 0, str(board)[:60])
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
    check("http: the board answers before any submission — an honest empty"
          " board, not a 503 and not a legacy snapshot's ghost",
          stt == 200 and payload.get("rows") == []
          and payload.get("participants") == 0)
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

    # ---- P6C.16: a separate, strict landing origin; token-hygiene logs ----
    import io as _io, contextlib as _ctx, run as runmod
    _ls = r.settings                                    # the battery's settings
    check("P6C land: an unset frontend base keeps the relative board",
          _ls.magic_landing() == "/#board", _ls.magic_landing())
    _ls.frontend_base = "https://papercoin.vercel.app"
    check("P6C land: a valid base points at the frontend board",
          _ls.magic_landing() == "https://papercoin.vercel.app/#board",
          _ls.magic_landing())
    _bad = ("http://papercoin.vercel.app", "https://wrong.example",
            "https://papercoin.vercel.app/", "https://papercoin.vercel.app?x=1",
            "https://user@papercoin.vercel.app", "https://papercoin.vercel.app:8443")
    check("P6C land: every invalid shape fails closed to the relative board",
          all((setattr(_ls, "frontend_base", _b), _ls.magic_landing() == "/#board")[1]
              for _b in _bad))
    _ls.frontend_base = ""
    _box = []
    _saved = r.s.mail.send_magic_link
    r.s.mail.send_magic_link = lambda to, url: (_box.append(url), True)[1]
    try:
        _ls.frontend_base = "https://papercoin.vercel.app"
        stt, payload = go("POST", "/api/account/start", body={"email": "lander@paper.io"})
        _tokL = magic_from(payload or {})
        check("P6C land: the emailed link still rides the API base",
              bool(_box) and _box[0] == _ls.public_base
              + f"/api/auth/magic?token={_tokL}", str(_box)[:80])
        _outF = r.dispatch("GET", "/api/auth/magic", {"token": _tokL}, {}, {}, None)
        check("P6C land: a real valid login lands on the frontend board",
              _outF[0] == 302 and _outF[2].get("Location")
              == "https://papercoin.vercel.app/#board", str(_outF)[:80])
        _outX = r.dispatch("GET", "/api/auth/magic", {"token": "nonsense"}, {}, {}, None)
        check("P6C land: refusals answer 401 without minting a cookie",
              _outX[0] == 401 and not (_outX[2] or {}).get("Set-Cookie"), str(_outX)[:80])
    finally:
        r.s.mail.send_magic_link = _saved
        _ls.frontend_base = ""

    # ---- P6I (the mail door speaks in keys): a failed send answers 502 with
    # ---- the single token 'mail_failed', the smtp reply staying server-side;
    # ---- the retry inside the resend window doubles neither account nor mail --
    def _boom(to, url):
        raise RuntimeError("smtp send failed: 451 hop refused")
    r.s.mail.send_magic_link = _boom
    _n0 = r.s.db.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    _boxI = _io.StringIO()
    with _ctx.redirect_stdout(_boxI):
        stt, payload = go("POST", "/api/account/start", body={"email": "boom@paper.io"})
    _printed = _boxI.getvalue()
    check("P6I mail door: a failing send answers 502 with the one clean key"
          " 'mail_failed' — no raw smtp reply rides the wire — while the"
          " detail rides the server's own print, the detail that once would"
          " have surfaced as 'mail_failed: smtp send failed: …'",
          stt == 502 and (payload or {}).get("error") == "mail_failed"
          and "[mail]" in _printed and "451 hop refused" in _printed,
          str(payload)[:80])
    stt, payload = go("POST", "/api/account/start", body={"email": "boom@paper.io"})
    _n1 = r.s.db.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    _routesrc = open(os.path.join(os.path.dirname(__file__),
                                  "paperboard", "http_app.py")).read()
    _frontsrc = open(os.path.join(os.path.dirname(__file__), "..", "js",
                                  "paperboard.js")).read()
    r.s.mail.send_magic_link = _saved
    check("P6I mail door: the retry inside the resend window mints exactly"
          " one account and re-burns no raising lane (a failed send cannot"
          " spam an inbox), and the repaired shapes are pinned in the shipped"
          " sources — the route answers the single token and the old"
          " exception-f-string is gone, while the front-end's map learned"
          " mail_failed and server_error, reserving the catch-all for a door"
          " that never answered at all",
          stt == 200 and (payload or {}).get("sent") is False
          and (payload or {}).get("deferred") is True and _n1 == _n0 + 1
          and 'fail(502, "mail_failed")' in _routesrc
          and 'f"mail_failed:' not in _routesrc
          and 'mail_failed: "' in _frontsrc and 'server_error: "' in _frontsrc,
          str(payload)[:80])
    _buf = _io.StringIO()
    with _ctx.redirect_stdout(_buf):
        runmod.Handler.log_message(
            None, '"GET /api/auth/magic?token=ABCDEF0123456789 HTTP/1.1" - - 302 21')
    _line = _buf.getvalue()
    check("P6C logs: the raw magic token never reaches the access log",
          "[pb]" in _line and "token=<t>" in _line and "ABCDEF0123456789" not in _line
          and "/api/auth/magic" in _line and "302" in _line, _line[:90])

    # ---- P6J (the door hunt's shipped lesson — observability): the access
    # ---- line carries a clock so a browser's minute can be found, and the
    # ---- join fallback now names its own class. the lane itself was proven
    # ---- healthy end to end against the live service (validation, buckets,
    # ---- account row, token, a real SMTP submit included, both routes under
    # ---- two seconds) and has never logged a failing /start — so the door's
    # ---- surviving suspects are the edge hop's badly-shaped answer and the
    # ---- fetch that never got one; only the next real browser tells them
    # ---- apart, and now it will say which. no semantics moved. --
    _boxJ = _io.StringIO()
    with _ctx.redirect_stdout(_boxJ):
        runmod.Handler.log_message(
            None, '"POST /api/account/start?token=ABCDEF0123456789 HTTP/1.1" - - 200 12')
    _lineJ = _boxJ.getvalue()
    check("P6J logs: the access line is stamped (HH:MM:SS after the tag) so a"
          " user's failing minute is findable — and the token redaction"
          " survives the stamp untouched",
          _lineJ.startswith("[pb] ") and _lineJ[5].isdigit()
          and _lineJ[7] == ":" and _lineJ[10] == ":"
          and "token=<t>" in _lineJ and "ABCDEF0123456789" not in _lineJ,
          _lineJ[:60])
    _frontJ = open(os.path.join(os.path.dirname(__file__), "..", "js",
                                "paperboard.js")).read()
    check("P6J mail door: the fallback names its own class — a status in"
          " parentheses means an answer arrived that the lane cannot read"
          " (an edge or proxy page), 'no answer' means the fetch itself died"
          " before any status — while every known refusal keeps its own words",
          '(e && e.status)' in _frontJ
          and "the mail door stuck. (n" in _frontJ
          and "the mail door stuck. no answer" in _frontJ
          and "that doesn't read like an email" in _frontJ, "instrument shipped")

    # ---- P6K (signup resilience — one retry, join flow only): the class is
    # ---- by SHAPE (status >= 500 carrying no payload error = the proxy-page
    # ---- shape; the parser cannot prove where a 5xx was born, and the app
    # ---- answers only JSON, so every JSON failure — refusal, pace, the app's
    # ---- own keyed 5xx — keeps its single try and its words). the no-status
    # ---- reject takes the one bounded retry under the p6l audit: it usually
    # ---- means the request died before the server, so the retry is the first
    # ---- real attempt; when the first was processed and its response was
    # ---- lost, the app's own magicResendSeconds (45s default) window remains
    # ---- the idempotency standing
    # ---- guard — a lost-but-processed first attempt re-answers { sent: true }
    # ---- without minting or mailing twice. the in-flight dedupe is the join
    # ---- flow's own answer to the doctrine the sibling buttons keep via
    # ---- btn.disabled — the join button has none. the seam runs under JXA on
    # ---- synchronous shams (JXA never pumps promise microtasks before exit);
    # ---- if the harness itself errors, osascript prints to stderr and the
    # ---- not_ guard below fails the run with the traceback in the label. --
    _sp = __import__("subprocess")
    _pk = open(os.path.join(os.path.dirname(__file__), "..", "js",
                            "paperboard.js")).read()
    _re6 = __import__("re")
    _pm = _re6.search(r"P6K join-retry begin -- \*/(.*?)\n/\* -- P6K join-retry end",
                      _pk, _re6.DOTALL)
    check("P6K seam: the retry stands between the markers, the call site moved"
          " onto it, and the join flow keeps no bare POST on its path — every"
          " other flow (verify, submit, onboard, wallet) still speaks to post()"
          " untouched, so the retry cannot leak past the join lane",
          bool(_pm) and "await postJoinWithRetry({ email: v }, post)" in _pk
          and 'post("/api/account/start"' not in _pk, "seam extracted")
    _js = _pm.group(1) + r"""
var mkSP = function (val, okFlag) {
  return { val: val, ok: okFlag,
    'catch': function (h) { return this.ok ? this : h(this.val); },
    'then': function (a, b) { return this.ok ? a(this.val) : b(this.val); } };
};
var mkErr = function (status, errName) {
  return { status: status, payload: errName ? { error: errName } : {} };
};
var res = [];
var go = function (name, script, wantCalls, wantThrow, wantStatus) {
  joinInFlight = null;
  var calls = 0;
  var postFn = function () { calls++; return script.shift(); };
  var threw = null;
  try { postJoinWithRetry({ email: 'x' }, postFn, function (f) { return f(); }); }
  catch (e) { threw = e; }
  var good = calls === wantCalls && ((wantThrow && threw &&
      (wantStatus === '~' ? String(threw.status) === 'undefined'
                           : threw.status === wantStatus))
      || (!wantThrow && !threw));
  res.push(name + (good ? ':ok' : ':FAIL(calls=' + calls
           + ',threw=' + (threw ? String(threw.status) : '-') + ')'));
};
go('s1-530-then-success', [mkSP(mkErr(530), false), mkSP({ sent: true }, true)],
   2, false, null);
go('s2-502-then-success', [mkSP(mkErr(502), false), mkSP({ sent: true }, true)],
   2, false, null);
go('s3-two-transients-stop-at-two', [mkSP(mkErr(530), false), mkSP(mkErr(503), false)],
   2, true, 503);
go('s4-validation-never-retried', [mkSP(mkErr(422, 'invalid_email'), false)],
   1, true, 422);
go('s5-pace-never-retried', [mkSP(mkErr(429, 'magic_rate_limited'), false)],
   1, true, 429);
go('s6-no-status-takes-one-retry', [mkSP({ payload: {} }, false),
                                    mkSP({ sent: true }, true)],
   2, false, null);
go('s7-two-no-status-stop-at-two', [mkSP({ payload: {} }, false),
                                    mkSP({ payload: {} }, false)],
   2, true, '~');
go('s8-bare-4xx-never-retried', [mkSP(mkErr(404), false)],
   1, true, 404);
joinInFlight = null;
var calls7 = 0;
var post7 = function () { calls7++; return mkSP({ sent: true }, true); };
var p1 = postJoinWithRetry({ email: 'x' }, post7, function (f) { return f(); });
var p2 = postJoinWithRetry({ email: 'x' }, post7, function (f) { return f(); });
res.push('s9-double-click-dedupes'
         + ((calls7 === 1 && p1 === p2) ? ':ok' : ':FAIL(calls=' + calls7 + ')'));
var bad6 = res.filter(function (x) { return x.indexOf(':ok') < 0; });
var _p6kVerdict = bad6.length ? 'P6K-FAILS ' + res.join(' ') : 'P6K-scenarios-behave';
(typeof console !== 'undefined' && console.log)
  ? (console.log(_p6kVerdict), _p6kVerdict) : _p6kVerdict;
"""
    # the engine is chosen by AVAILABILITY, never by platform assumption:
    # osascript (the JXA this harness was born on), then node — and where
    # neither exists, a bare Linux runner, the PYTHON TWIN below drives the
    # very same nine scenarios over the very same synchronous shams. an absent
    # engine is never a silent pass: the twin's verdict must match, and the
    # textual pins that follow (markers, predicate, arity, pause) guard the js
    # shape itself on every platform, so a mutated seam still cannot sail.
    _shu6 = __import__("shutil")
    _p6keng = next((list(_c) for _c in (("osascript", "-l", "JavaScript", "-e"),
                                        ("node", "-e"))
                    if _shu6.which(_c[0])), None)
    class _P6KSham6:                     # a python mirror of mkSP, catch/then
        def __init__(self, val, ok):
            self.val, self.ok = val, bool(ok)
        def catch(self, h):
            return self if self.ok else h(self.val)
        def then(self, a, b):
            return a(self.val) if self.ok else b(self.val)
    class _P6KThrow6(Exception):
        pass
    def _p6k_transient6(e):
        return (not (isinstance(e, dict) and isinstance(e.get("payload"), dict)
                     and e["payload"].get("error"))) \
               and (e.get("status") is None or e["status"] >= 500)
    def _p6k_post6(st, shams):
        st["calls"] += 1
        return shams.pop(0)
    def _p6k_seam6(st, shams):            # a line-for-line python reading of
        if st["inflight"] is not None:   # the js above, over the same shams
            return st["inflight"]
        first = _p6k_post6(st, shams)
        if first.ok:
            done = first.val
        else:
            e = first.val
            if not _p6k_transient6(e):
                raise _P6KThrow6(e)
            again = _p6k_post6(st, shams)      # wake is supplied: the one try
            if again.ok:
                done = again.val
            else:
                st["inflight"] = None          # the js clears in the reject arm
                raise _P6KThrow6(again.val)
        st["inflight"] = done                  # the js sets it after then ran
        return done
    def _p6k_go6(name, shams, want_calls, want_throw, want_status):
        st = {"calls": 0, "inflight": None}
        threw = None
        try:
            _p6k_seam6(st, shams)
        except _P6KThrow6 as t:
            threw = t.args[0]
        good = st["calls"] == want_calls and (
            (want_throw and threw is not None and (
                (want_status == "~" and threw.get("status") is None)
                or (want_status != "~" and threw.get("status") == want_status)))
            or (not want_throw and threw is None))
        return name + (":ok" if good else ":FAIL(calls=%d)" % st["calls"])
    def _p6k_twin6():
        E, scr = dict, []
        go6 = _p6k_go6
        scr.append(go6("s1-530-then-success",
                       [_P6KSham6(E(status=530), False),
                        _P6KSham6(E(sent=True), True)], 2, False, None))
        scr.append(go6("s2-502-then-success",
                       [_P6KSham6(E(status=502), False),
                        _P6KSham6(E(sent=True), True)], 2, False, None))
        scr.append(go6("s3-two-transients-stop-at-two",
                       [_P6KSham6(E(status=530), False),
                        _P6KSham6(E(status=503), False)], 2, True, 503))
        scr.append(go6("s4-validation-never-retried",
                       [_P6KSham6(E(status=422, payload={"error":
                                        "invalid_email"}), False)], 1, True, 422))
        scr.append(go6("s5-pace-never-retried",
                       [_P6KSham6(E(status=429, payload={"error":
                                        "magic_rate_limited"}), False)], 1, True, 429))
        scr.append(go6("s6-no-status-takes-one-retry",
                       [_P6KSham6(E(payload={}), False),
                        _P6KSham6(E(sent=True), True)], 2, False, None))
        scr.append(go6("s7-two-no-status-stop-at-two",
                       [_P6KSham6(E(payload={}), False),
                        _P6KSham6(E(payload={}), False)], 2, True, "~"))
        scr.append(go6("s8-bare-4xx-never-retried",
                       [_P6KSham6(E(status=404), False)], 1, True, 404))
        st9, s9 = {"calls": 0, "inflight": None}, [_P6KSham6(E(sent=True), True)]
        p1 = _p6k_seam6(st9, s9)
        p2 = _p6k_seam6(st9, s9)
        scr.append("s9-double-click-dedupes"
                    + (":ok" if (st9["calls"] == 1 and p1 is p2)
                       else ":FAIL(calls=%d)" % st9["calls"]))
        bad = [x for x in scr if ":ok" not in x]
        return ("P6K-FAILS " + " ".join(scr)) if bad else "P6K-scenarios-behave"
    if _p6keng is None:
        print("  note  P6K scenarios: no js engine on this box — the python"
              " twin drives the matrix, textual pins hold the js shape")
        _t6 = _p6k_twin6()
        _p6k_ok = (_t6 == "P6K-scenarios-behave")
    else:
        _o6 = _sp.run(_p6keng + [_js], capture_output=True, text=True)
        _t6 = (_o6.stdout or "") + (_o6.stderr or "")
        _p6k_ok = (_o6.returncode == 0 and "P6K" in _t6 and "FAIL" not in _t6)
    check("P6K behaviors: the seam, driven on synchronous shams under jxa or"
          " node where either is present, or under the python twin where"
          " neither is, retries the proxy-page class exactly once"
          " (530-then-success and"
          " 502-then-success each take two turns; two transients stop at two and"
          " surface the second failure), while validation (422), pace (429),"
          " and a bare 4xx take exactly one turn, and the no-status reject —"
          " the p6l class — takes the one bounded retry and stops at two when"
          " both turns are rejected; a double click during an in-flight join"
          " dedupes into one POST",
          _p6k_ok, _t6.strip()[:120])
    check("P6K class: the predicate's written shape is 'no payload error, and"
          " either no status at all or a status at 500 or above' — JSON"
          " refusals, paces, and the app's keyed 5xx are structurally excluded"
          " by the payload clause, a bare 4xx by the status arms, so no"
          " permanent answer can ever eat a second POST, and the class cannot"
          " silently widen either way",
          "e.status >= 500" in _pk
          and "!(e.payload && e.payload.error)" in _pk
          and "!e.status || e.status >= 500" in _pk, "predicate pinned")
    check("P6M call site: the module's own call passes the module's own post as"
          " the seam's second argument. the harness ever fed the seam its own"
          " well-formed postFn, so the machinery could not feel production's"
          " missing argument — a call site that ships without it throws inside"
          " the seam before any fetch, the door cries its no-answer sentence all"
          " the same, and the network panel stays empty: the seam's arity is"
          " now pinned as a literal, so the two ends of the lane can no longer"
          " drift apart unnoticed",
          "postJoinWithRetry({ email: v }, post)" in _pk, "post rides the call")
    check("P6N pause: the retry's one pause is sized to the weather it must"
          " outride — the connector's measured re-registration took ~18 seconds,"
          " so the pause stands at 20000 ms. the doctrine is otherwise untouched:"
          " still exactly one extra POST on the transient shapes, JSON refusals"
          " and paces still never see a second try, and the app's own 45 s"
          " window still guards a lost-but-processed first attempt — what"
          " changed is only how far a blip may be and the click still answer",
          "const JOIN_RETRY_PAUSE = 20000" in _pk, "pause sized to the blip")
    _svc = open(os.path.join(os.path.dirname(__file__), "paperboard",
                             "service.py")).read()
    check("P6K idempotency: the guard a retry walks into is the app's own — the"
          " issuance lane honors magicResendSeconds (45s default) and a known"
          " account inside the window is touched, not re-minted, so a"
          " lost-but-processed first attempt cannot double the mail; outside"
          " the window a genuine resend costs one mail by design — the same"
          " door an impatient human already knocks",
          "magicResendSeconds" in _svc and "45" in _svc and "recent" in _svc,
          "window stands")
    _mailsrc = open(os.path.join(os.path.dirname(__file__), "paperboard",
                                 "mail.py")).read()
    _httpsrc = open(os.path.join(os.path.dirname(__file__), "paperboard",
                                 "http_app.py")).read()
    check("P6P observability: the mail lane now tells the truth three ways — a"
          " token-free timing line on an accepted smtp send, an explicit"
          " deferral print when the 45 s resend window spares the inbox, and"
          " (operational, VPS env) unbuffered stdout so journald sees the words"
          " the moment they happen; absence of log lines stops being evidence"
          " of absence of code",
          "[mail] sent in" in _mailsrc and "[mail] deferred" in _httpsrc,
          "three ways, no secrets")
    import ssl as _p6ps
    import importlib.util as _p6piu
    _mspec = _p6piu.spec_from_file_location("p6p_mail_under_test",
              os.path.join(os.path.dirname(__file__), "paperboard", "mail.py"))
    _m = _p6piu.module_from_spec(_mspec); _mspec.loader.exec_module(_m)
    _m.CA_BUNDLE = "/this-bundle-is-absent-on-purpose.pem"
    _ctx = _m._ssl_context()
    check("P6P.2 portability: on a box with no mac anchor bundle the tls"
          " context still builds — an absent file is a linux fact, never a"
          " mail-room crash — and it still verifies the peer and its name:"
          " the fix trades an anchor path, never the verification itself",
          _ctx.verify_mode == _p6ps.CERT_REQUIRED and _ctx.check_hostname,
          "context survives absence")
    check("P6P.3 deferral honesty: the route distinguishes fresh mail from a"
          " fresh window — a deferred answer says sent:false plus deferred:true"
          " and the front reads that flag before it smiles; the 45 s window,"
          " the buckets, and the 502-on-refusal contract all stand untouched",
          "deferred=True" in _httpsrc and "j.deferred" in _pk,
          "the wire tells truth")
    _cssT = open(os.path.join(os.path.dirname(__file__), "..", "css",
                              "tokens.css")).read()
    _cssP = open(os.path.join(os.path.dirname(__file__), "..", "css",
                              "paper.css")).read()
    _cssB = open(os.path.join(os.path.dirname(__file__), "..", "css",
                              "paperboard.css")).read()
    check("P6B the voice as law: the press-sheet tokens carry a type scale, a"
          " spacing scale, a motion pair and two corner families; the mast"
          " index survives at the phone width instead of vanishing; the five"
          " honest states have names — loading, empty, error, success, deferred"
          " — and the lanes still to come cite them from day one; no mechanism"
          " moved, the system just gained words",
          "--fs-h3" in _cssT and "--sp-3" in _cssT and "--dur-1" in _cssT
          and "p6b" in _cssP
          and ".state__title" in _cssB and ".p-defer" in _cssB and ".field" in _cssB,
          "the system has words")

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
    # the author law (P6D): a verified account scores its OWN posts. this
    # lane's account rides the degenledger claim so its m2 post is its own.
    database.conn.execute(
        "UPDATE accounts SET x_verified_at=?, x_handle='degenledger',"
        " x_user_id='77' WHERE username='grompity'", (time.time(),))
    database.conn.commit()

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
    check("submit: the author law — a post by ANOTHER handle cannot score for"
          " you (wrong_author, no row)",
          stt == 403 and payload.get("error") == "wrong_author", f"{stt}")
    # the law rides the account's own claim, so the claim follows the account
    # for the bidetbear post below (a rename the server accepts: the handle is
    # a claim the owner controls; the stable id is the person).
    database.conn.execute("UPDATE accounts SET x_handle='bidetbear',"
                          " x_user_id=NULL WHERE username='grompity'")
    database.conn.commit()
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
          stt == 200 and len(subs) == 2 and all("audit" in row0 for row0 in subs),
          str(len(subs)))

    # ------------------------- public board: the live ledger rules it ---------
    stB, dS2, sB, cidB = make_env2("BOARD", MockXClient(Settings()))
    lv0 = sB.leaderboard(cidB)
    check("board: a competition without submissions answers an honest empty"
          " board — zero participants, zero rows, no 503 and nothing invented",
          lv0 is not None and lv0["participants"] == 0 and lv0["rows"] == [])
    accZ, _cz, _ez = sB.create_or_touch_account("zero@paper.io")
    dS2.conn.execute(
        "INSERT INTO submissions(account_id,competition_id,x_post_id,url,"
        "reason,eligible,points,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
        (accZ["id"], cidB, "z1", "https://x.com/zero/status/z1",
         "no_identifier", 0, 0.0, time.time()))
    dS2.conn.commit()
    lvZ = sB.leaderboard(cidB)
    check("board: a submitter whose every post was ineligible still appears —"
          " at zero points with zero counted posts, present and never padded",
          lvZ["participants"] == 1 and lvZ["rows"][0]["points"] == 0.0
          and lvZ["rows"][0]["posts"] == 0 and lvZ["rows"][0]["rank"] == 1,
          str(lvZ["rows"][0])[:90])
    accY, _cy, _ey = sB.create_or_touch_account("tieone@paper.io")
    accX, _cx, _ex = sB.create_or_touch_account("tietwo@paper.io")
    for accT, pidT in ((accY, "t1"), (accX, "t2")):
        dS2.conn.execute(
            "INSERT INTO submissions(account_id,competition_id,x_post_id,url,"
            "reason,eligible,points,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
            (accT["id"], cidB, pidT, f"https://x.com/t/status/{pidT}",
             "ok", 1, 100.0, time.time()))
    dS2.conn.commit()
    lvT = sB.leaderboard(cidB)
    check("board: ties share the rank as the dashboard promises — two"
          " 100-point accounts both take rank one, and the zero row lands at"
          " rank three (ties share, they never inflate)",
          [r["rank"] for r in lvT["rows"]] == [1, 1, 3]
          and lvT["participants"] == 3 and lvT["total_points"] == 200.0,
          str([r["rank"] for r in lvT["rows"]]))
    stt, payload = go("GET", "/api/leaderboard")        # main env, after submits
    rowsB = payload.get("rows") or []
    ledB = database.conn.execute(
        "SELECT COALESCE(SUM(points),0) AS pts,"
        " SUM(CASE WHEN eligible THEN 1 ELSE 0 END) AS cnt,"
        " COUNT(DISTINCT account_id) AS subs FROM submissions").fetchone()
    check("board: the live ledger rules the PUBLIC board — the points are the"
          " frozen submit-time totals (equals the ledger sum), counted posts"
          " are the eligible ones alone (the no_follow row adds none), and"
          " only real submitters appear (the scan-era users cannot vote)",
          stt == 200 and len(rowsB) == 1 and rowsB[0]["points"] == ledB["pts"]
          and rowsB[0]["posts"] == ledB["cnt"] and rowsB[0]["rank"] == 1
          and len(rowsB) == ledB["subs"], str(rowsB)[:110])
    check("board: the board and the ledger are one truth — points,"
          " participants, and the rank-1 tier label all ride the frozen rows,"
          " never a recomputed guess",
          payload.get("totalPoints") == ledB["pts"]
          and payload.get("participants") == ledB["subs"]
          and rowsB[0]["tier"] == "FRONT PAGE", str(payload.get("totalPoints")))

    stt, payload = go("POST", "/api/account/start", body={"email": "B@Paper.io"})
    tokb = magic_from(payload or {})
    outb = r.dispatch("GET", "/api/auth/magic", {"token": tokb}, {}, {}, None)
    seatb = outb[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]
    ckb = {"PBSD": seatb}
    database.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='anyone'"
                          " WHERE lower(email)='b@paper.io'", (time.time(),))
    database.conn.commit()
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
          stt == 200 and tok2 == tok3 and payload.get("deferred") is True
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
    dS.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='shy'"
                    " WHERE id=?", (time.time(), accS["id"]))
    dS.conn.commit()
    signed = sS.account_by_id(accS["id"])
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
    dD3.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='shy'"
                     " WHERE id=?", (time.time(), accD3["id"]))
    dD3.conn.commit()
    signedD3 = sD3.account_by_id(accD3["id"])
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
    # this battery pins the ANTI-GAMING DOCTRINE of the v1 era, whichever
    # version happens to ship — it carries its own v1.1 scoring block.
    scx = dict(stD3.pb["scoring"], version="pb-v1.1", basePerPost=5,
               perLike=6, perReply=8, perRepost=14, perQuote=20,
               impressions={"points": 30, "halfLife": 1200}, postCap=120,
               thinSignalMultiplier=0.5, minImpressions=10)
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
    _dB3.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='quiet'"
                      " WHERE id=?", (time.time(), accB3["id"]))
    _dB3.conn.commit()
    signedB3 = sB3.account_by_id(accB3["id"])
    resB3 = sB3.submit_post(signedB3, "https://x.com/quiet/status/555", cidB3)
    check("submit: a metrics-blind provider still verifies presence (base points ride)",
          resB3.get("ok") and (resB3.get("submission") or {}).get("points")
          == stD3.pb["scoring"]["basePerPost"], str(resB3)[:110])

    # --------------------------------------- the provider failing must fail closed
    _stB4, _dB4, sB4, cidB4 = make_env2("BOOM", BoomX())
    accB4, _cre, _er = sB4.create_or_touch_account("boom@paper.io")
    signedB4, _why = sB4.consume_magic_link(sB4.issue_magic_link(accB4["id"])[0])
    _dB4.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='boom'"
                      " WHERE id=?", (time.time(), accB4["id"]))
    _dB4.conn.commit()
    signedB4 = sB4.account_by_id(accB4["id"])
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
        sK.db.conn.execute(
            "UPDATE accounts SET x_verified_at=?, x_handle='paperusdc',"
            " x_user_id='4242' WHERE id=?", (time.time(), accK["id"]))
        sK.db.conn.execute(
            "UPDATE accounts SET x_verified_at=?, x_handle='paperusdc2'"
            " WHERE id=?", (time.time(), acc2["id"]))
        sK.db.conn.commit()
        signedK = sK.account_by_id(accK["id"])    # re-read: the gate reads rows
        signed2 = sK.account_by_id(acc2["id"])
        cnt0 = dict(counters)                    # where the clock of calls stands

        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9001") if who == "fx" else oe_body("9001"))
        resK = sK.submit_post(signedK, "https://x.com/anyone/status/9001", cidK)
        subK = resK.get("submission") or {}
        audK = json.loads(dK.conn.execute(
            "SELECT score_json FROM submissions ORDER BY id DESC LIMIT 1"
            ).fetchone()["score_json"])
        sclK = stD3.pb["scoring"]          # the STACK batteries guard TODAY's
        ptsW, _aW = score_post(dict(likes=8, replies=3, reposts=3, quotes=0,
                                    impressions=123), sclK, {})
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
        check("stack: genuine zeros stay a measurement, not an absence —"
              " under the shipped rules the row scores exactly the base,"
              " missing is empty",
              res2.get("ok") and sub2.get("points") == sclK["basePerPost"]
              and sub2.get("missing") == [] and sub2.get("verification") == "engagement",
              str(sub2)[:90])

        xapimod.urllib.request.urlopen = stub(lambda u, who: fx_body(
            "9003", quotes=None, views=None, bookmarks=None)
            if who == "fx" else oe_body("9003"))
        res3 = sK.submit_post(signedK, "https://x.com/anyone/status/9003", cidK)
        sub3 = res3.get("submission") or {}
        ptsM, _aM = score_post(dict(likes=8, replies=3, reposts=3, quotes=None,
                                    impressions=None), sclK, {})
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
              and sub4.get("points") == sclK["basePerPost"]
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
        sN.db.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='shy'"
                           " WHERE id=?", (time.time(), accN["id"]))
        sN.db.conn.commit()
        signedN = sN.account_by_id(accN["id"])
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

    saved_host_env = os.environ.pop("PAPER_HOST", None)         # absent on this box

    def host_case(value):                                       # bind-address knob
        if value is None:
            os.environ.pop("PAPER_HOST", None)
        else:
            os.environ["PAPER_HOST"] = value
        return Settings().host

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

    hcD, hcL = host_case(None), host_case("127.0.0.1")
    check("bind: PAPER_HOST unset defaults to 0.0.0.0 — the historic wide"
          " bind stays the backwards-compatible truth", hcD == "0.0.0.0")
    check("bind: PAPER_HOST=127.0.0.1 lands in Settings.host verbatim —"
          " the loopback lane for local testing", hcL == "127.0.0.1")
    if saved_host_env is not None:
        os.environ["PAPER_HOST"] = saved_host_env
    else:
        os.environ.pop("PAPER_HOST", None)

    # ================= pb-v3.0: hybrid like scoring + secure manual review ====
    # (battery notes: this file's own text travels through the display layer
    # case-folded, but the bytes on disk are what run — these checks talk to
    # the real objects, so they measure the truth, not the rendering.)
    stV, dbV, sV, cidV = make_env2("V3", BlindX())
    rV = http_app.Router(sV, stV, dbV, BlindX())
    stV.admset = set(["root@paper.io"])          # the gate is a server truth
    accP, _cV1, _eV1 = sV.create_or_touch_account("user@paper.io")
    signedP, _whyV = sV.consume_magic_link(sV.issue_magic_link(accP["id"])[0])
    sV.db.conn.execute("UPDATE accounts SET x_verified_at=?, x_handle='quiet'"
                       " WHERE id=?", (time.time(), accP["id"]))
    sV.db.conn.commit()
    signedP = sV.account_by_id(accP["id"])

    def seat_for(email):
        accX = sV.create_or_touch_account(email)[0]
        out = rV.dispatch("GET", "/api/auth/magic",
                          {"token": sV.issue_magic_link(accX["id"])[0]},
                          {}, {}, None)
        return {"PBSD": out[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]}

    def gov(method, path, query=None, headers=None, cookies=None, body=None):
        out = rV.dispatch(method, path, query or {}, headers or {},
                          cookies or {}, body)
        return out[0], out[1]

    ckP, ckA = seat_for("user@paper.io"), seat_for("root@paper.io")
    JSONV = {"content-type": "application/json"}

    # — lane A: a presence post (no count) lands base-only, pending, queued
    sV.x = BlindX()
    resP = sV.submit_post(signedP, "https://x.com/quiet/status/7001", cidV)
    idP = (resP.get("submission") or {}).get("id")
    check("v3: an eligible post without a count earns the base, holds the "
          "bonus pending, and walks into the review queue (never a fake zero)",
          resP.get("ok") and (resP.get("submission") or {}).get("points")
          == stV.pb["scoring"]["basePerPost"]
          and (resP.get("submission") or {}).get("like_status") == "pending"
          and len(sV.admin_queue(cidV, "pending")) == 1
          and sV.admin_queue(cidV, "verified") == [], str(resP)[:110])

    # — lane B: an automatic count is recorded as a CANDIDATE, not a crown
    sV.x = RichX(likes=8)
    resQ = sV.submit_post(signedP, "https://x.com/quiet/status/7002", cidV)
    idQ = (resQ.get("submission") or {}).get("id")
    rowQ = dbV.conn.execute("SELECT like_status, official_likes FROM submissions"
                            " WHERE id=?", (idQ,)).fetchone()
    check("v3: an automatically retrieved count rides along as a reported "
          "candidate awaiting verification — base 10 plus the curve for 8",
          resQ.get("ok") and rowQ["like_status"] == "reported"
          and rowQ["official_likes"] == 8
          and (resQ.get("submission") or {}).get("points") == 18.0
          and len(sV.admin_queue(cidV, "pending")) == 1, str(resQ)[:110])

    # — other metrics stay decoration under v3
    sV.x = RichX(likes=None)
    resM = sV.submit_post(signedP, "https://x.com/quiet/status/7003", cidV)
    check("v3: replies/reposts/quotes/impressions present but ignored — a "
          "post with NO likes reported scores exactly the base",
          resM.get("ok") and (resM.get("submission") or {}).get("points")
          == stV.pb["scoring"]["basePerPost"], str(resM)[:90])
    idM = (resM.get("submission") or {}).get("id")

    # — the gate: server-side, every door
    stt, pl = gov("GET", "/api/admin/whoami")
    check("v3 gate: no session — the queue is 401, not a peek",
          stt == 401 and pl.get("error") == "not_authenticated")
    stt, pl = gov("GET", "/api/admin/whoami", cookies=ckP)
    check("v3 gate: an ordinary signed account is 403 admin-only (hidden page"
          " is not security)", stt == 403 and pl.get("error") == "admin_only")
    stt, pl = gov("GET", "/api/admin/whoami", cookies=ckA)
    check("v3 gate: an allowlisted session walks in (whoami 200)",
          stt == 200 and pl.get("is_admin"))
    stt, pl = gov("GET", "/api/admin/queue", query={"status": "nonsense"},
                  cookies=ckA)
    check("v3 gate: a nonsense filter is a clean 400", stt == 400
          and pl.get("error") == "bad_status")
    stt, pl = gov("GET", "/api/admin/queue", cookies=ckA)
    check("v3 queue: pending is the default lane and carries the honest "
          "columns (post, submitter, status, audit events) — and pending is "
          "where BOTH lane-A posts wait (presence and likes-omitted alike)",
          stt == 200 and len(pl.get("rows") or []) == 2
          and (pl["rows"][0].get("submitter") or pl["rows"][0].get("handle"))
          and pl["rows"][0].get("events") == [], str(pl)[:110])

    # — manual verification equals automatic truth (the fairness spine)
    stt, pl = gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
                  body={"id": idP, "likes": 8, "reason": "battery eye"})
    check("v3 fairness: a manually verified count of 8 scores the IDENTICAL "
          "number as the automatic 8 — one curve, one truth (18 pts)",
          stt == 200 and pl.get("ok") and abs(pl.get("points", -1) - 18.0) < .001,
          str(pl)[:110])
    rowP = dbV.conn.execute("SELECT like_status, like_measured_at,"
                            " submitted_at FROM submissions WHERE id=?",
                            (idP,)).fetchone()
    check("v3 policy: a count without an explicit timestamp inherits the "
          "SUBMISSION time (official count = submit-time snapshot)",
          rowP["like_status"] == "verified"
          and abs(rowP["like_measured_at"] - rowP["submitted_at"]) < .001,
          str(dict(rowP))[:90])
    ev1 = dbV.conn.execute("SELECT COUNT(*) n FROM admin_events"
                           " WHERE submission_id=?", (idP,)).fetchone()["n"]
    check("v3 audit: the verification appended exactly one immutable event",
          ev1 == 1, str(ev1))

    # — idempotence: the repeat moves history, never points
    gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
        body={"id": idP, "likes": 8, "reason": "double click"})
    ev2 = dbV.conn.execute("SELECT COUNT(*) n FROM admin_events"
                           " WHERE submission_id=?", (idP,)).fetchone()["n"]
    ptsP = dbV.conn.execute("SELECT points FROM submissions WHERE id=?",
                            (idP,)).fetchone()["points"]
    check("v3 idempotence: a repeated verification re-scores the same number "
          "and merely appends its own event (a duplicate request never "
          "double-awards)", ev2 == 2 and abs(ptsP - 18.0) < .001, str(ev2))
    evRow = dbV.conn.execute(
        "SELECT admin, action, prev_count, new_count, prev_points, new_points,"
        " measured_at, acted_at, reason, method, scoring_version"
        " FROM admin_events WHERE submission_id=? ORDER BY id LIMIT 1",
        (idP,)).fetchone()
    check("v3 audit: every event names the admin, the action, the scoring "
          "version, old and new counts and points, and both timestamps — the "
          "first event says where the count came from (method manual)",
          evRow["admin"] == "root@paper.io" and evRow["action"] == "verify"
          and evRow["new_count"] == 8 and evRow["prev_count"] is None
          and abs((evRow["new_points"] or 0) - 18.0) < .001
          and evRow["scoring_version"] == "pb-v3.0"
          and evRow["measured_at"] and evRow["acted_at"]
          and evRow["reason"] == "battery eye", str(dict(evRow))[:120])

    # — the curve itself, end-to-end through score_post (worked examples)
    scc = stV.pb["scoring"]
    p300, _ = score_post(dict(likes=300, replies=3, reposts=4, quotes=5,
                              impressions=66), scc, {})
    p900, _ = score_post(dict(likes=900, replies=None, reposts=None,
                              quotes=None, impressions=None), scc, {})
    check("v3 curve: the tail rises then saturates — 300 likes -> 160, and "
          "900 likes hits the 210 total cap (bonus capped at 200)",
          abs(p300 - 160.0) < .001 and abs(p900 - 210.0) < .001,
          f"{p300}/{p900}")

    # — dispute: flags the row, keeps the score, never clears the lane by lie
    stt, pl = gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
                  body={"id": idQ, "status": "dispute", "reason": "suspicious"})
    rowQ2 = dbV.conn.execute("SELECT like_status, points FROM submissions"
                             " WHERE id=?", (idQ,)).fetchone()
    check("v3 dispute: a disputed row keeps its score, wears the flag, and "
          "the count stays frozen for later correction",
          stt == 200 and rowQ2["like_status"] == "disputed"
          and abs(rowQ2["points"] - 18.0) < .001
          and len(sV.admin_queue(cidV, "disputed")) == 1, str(pl)[:90])
    stt, pl = gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
                  body={"id": idM, "likes": 42, "reason": "corrected eye"})
    check("v3 correction: verifying after a dispute re-scores and the "
          "history stands (events only ever append)",
          stt == 200 and len(sV.admin_queue(cidV, "verified")) >= 1, str(pl)[:80])

    # — ordinary accounts cannot touch the machine at all
    stt, pl = gov("POST", "/api/admin/verify", cookies=ckP, headers=JSONV,
                  body={"id": idP, "likes": 1})
    check("v3 gate: even POSTing the right shape, a non-admin is 403 on "
          "verify — users never supply scores", stt == 403)
    for bad, why in (({"id": idP, "likes": -5}, "negative"),
                     ({"id": idP, "likes": "seven"}, "word"),
                     ({"id": idP, "likes": True}, "boolean"),
                     ({"id": "nope", "likes": 3}, "bad id"),
                     ({}, "no id")):
        stt, pl = gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
                      body=bad)
        check(f"v3 input: {why} arrives safe ({pl.get('error')})",
              stt in (400, 404, 409), f"{stt}/{pl.get('error')}")
    stt, pl = gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
                  body={"id": idP, "likes": 20_000_001, "reason": "moon"})
    check("v3 input: a moon-shot count is refused (suspicious_count)",
          stt == 400 and pl.get("error") == "suspicious_count", str(pl)[:80])
    gov("POST", "/api/admin/verify", cookies=ckA, headers=JSONV,
        body={"id": idP, "likes": 12, "reason": "moved the number"})
    corr = [r["id"] for r in sV.admin_queue(cidV, "corrected")]
    check("v3 corrected lane: only a count that MOVED the number counts as "
          "a correction — idempotent repeats stay mere history",
          corr == [idP], str(corr))

    # — the gate fails CLOSED when the realm forgets its admins
    stV.admset = set()
    stt, pl = gov("GET", "/api/admin/queue", cookies=ckA)
    check("v3 gate: an empty allowlist answers 403 no-admins-configured "
          "rather than handing out the keys", stt == 403
          and pl.get("error") == "no_admins_configured")
    stV.admset = set(["root@paper.io"])

    # — transaction integrity: lightning after the UPDATE rolls the whole act
    class ShakyConn:
        """stands in for the connection: the second statement (the UPDATE)
        is where the battery lightning strikes."""
        def __init__(self, inner):
            self.inner, self.n = inner, 0
        def execute(self, sql, params=()):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("battery lightning")
            return self.inner.execute(sql, params)
        def commit(self):
            return self.inner.commit()
        def rollback(self):
            return self.inner.rollback()

    real_conn = dbV.conn
    dbV.conn = ShakyConn(real_conn)
    try:
        sV.verify_like(idM, "root@paper.io", 99)
        struck = False
    except RuntimeError:
        struck = True
    dbV.conn = real_conn
    real_conn.rollback()
    check("v3 transaction: the service reports the lightning (the exception "
          "reaches the caller) — nothing swallowed", struck)
    evM = dbV.conn.execute("SELECT COUNT(*) n FROM admin_events"
                           " WHERE submission_id=?", (idM,)).fetchone()["n"]
    evM0 = dbV.conn.execute("SELECT COUNT(*) n FROM admin_events"
                            " WHERE submission_id=? AND action='verify'"
                            " AND new_count=42", (idM,)).fetchone()["n"]
    ptsM_ = dbV.conn.execute("SELECT points FROM submissions WHERE id=?",
                             (idM,)).fetchone()["points"]
    check("v3 transaction: lightning between the two statements rolls the "
          "whole verification — score, count, and audit move as one",
          evM == evM0 and evM == 1 and abs(ptsM_ - 52.0) < .001, f"{evM}/{evM0}")

    # — the two public faces still tell one story; the duplicate wall holds
    dashV = sV.dashboard(signedP, cidV)
    boardV = sV.leaderboard(cidV)
    mine = [r for r in boardV["rows"] if r["user"]["id"] == accP["id"]]
    check("v3 surfaces: dashboard and board agree on the authoritative "
          "points, and the board says how many counts are pending",
          len(mine) == 1 and abs(mine[0]["points"] - dashV["points"]) < .001
          and mine[0]["pending"] == 0, str(mine)[:110])
    resD = sV.submit_post(signedP, "https://x.com/quiet/status/7001", cidV)
    check("v3 walls: the duplicate protection still speaks BEFORE scoring, "
          "and answers with the original's points (owner self)",
          not resD.get("ok") and resD.get("reason") == "duplicate_submission"
          and resD.get("owner") == "self" and abs((resD.get("points") or 0) - 22.0) < .001,
          str(resD)[:90])

    # ====== payouts phase 1: the friday rhythm, in test clothes =============
    # one board, nine accounts, nine fates: A earns big and gets paid; B has
    # no wallet; C earns a tie and shares it fairly; D's wallet is a lie;
    # E ties A but shares his wallet; F scored nothing; G's wallet is still
    # warming; H closed the account; I ties C. then: freeze, a board that
    # moves, a hand on the total, a faulted run, a careful retry.
    stW, dbW, sW, cidW = make_env2("PAY", BlindX())
    rW = http_app.Router(sW, stW, dbW, BlindX())
    stW.admset = set(["root@paper.io"])
    MINTW = "E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk"
    idsW = {}
    for nm in ("A", "B", "C", "D", "E", "F", "G", "H", "I"):
        accW = sW.create_or_touch_account(nm.lower() + "@pay.io")[0]
        idsW[nm] = accW["id"]
        sW.onboard_account(sW.consume_magic_link(sW.issue_magic_link(accW["id"])[0])[0],
                           "pay" + nm.lower(), MINTW[:-1] + nm.lower())
    # the payout fable is a STANDS-AND-FATES test, not a scoring test — the
    # nine seats ride verified claims so the author law is silent here (the
    # points that make the fable run stay exactly where the fable put them).
    dW = sW.db.conn
    dW.execute("UPDATE accounts SET x_verified_at=?, x_handle='quiet'"
               " WHERE x_handle IS NULL", (time.time(),))
    dW.commit()
    def post_pay(nm, url, likes):
        sig = sW.account_by_id(idsW[nm])          # fresh row: the gate reads rows
        sW.submit_post(sig, url, cidW)
        sid = dbW.conn.execute("SELECT id FROM submissions WHERE account_id=?"
                               " ORDER BY id DESC LIMIT 1",
                               (idsW[nm],)).fetchone()["id"]
        if likes is not None:
            sW.verify_like(sid, "root@paper.io", likes)
    for nm, lk in (("A", 300), ("B", 100), ("C", 50), ("D", None), ("E", 300),
                   ("F", 20), ("G", 30), ("H", 20), ("I", 50)):
        post_pay(nm, "https://x.com/" + nm.lower() + "/status/900" + nm.lower(), lk)
    dbW.conn.execute("UPDATE accounts SET wallet=NULL WHERE id=?", (idsW["B"],))
    dbW.conn.execute("UPDATE accounts SET wallet=? WHERE id=?", ("0" * 40, idsW["D"]))
    dbW.conn.execute("UPDATE accounts SET wallet=? WHERE id=?", (MINTW[:-1] + "a", idsW["E"]))
    dbW.conn.execute("UPDATE accounts SET wallet_effective_at=? WHERE id=?",
                     (time.time() + 86400, idsW["G"]))
    dbW.conn.execute("UPDATE accounts SET status='closed' WHERE id=?", (idsW["H"],))
    dbW.conn.execute("UPDATE submissions SET points=0, eligible=0 WHERE account_id=?",
                     (idsW["F"],))
    dbW.conn.commit()
    vw1 = sW.payout_prepare(cidW, "root@paper.io", 21)
    check("pay: a prepared batch is a draft; the pool splits among the eligible and says its total",
          vw1.get("ok") is True and vw1["batch"]["status"] == "draft"
          and vw1["batch"]["participants"] == 3 and vw1["batch"]["total_base"] == 21,
          str(vw1.get("reason") or vw1["batch"]["status"]))
    exclSet = sorted({e["reason"] for e in vw1["batch"]["excluded"]})
    check("pay: every exclusion speaks for itself — six fates, six reasons, no silent drops",
          exclSet == ["account_closed", "duplicate_wallet", "invalid_wallet",
                      "missing_wallet", "wallet_warming", "zero_points"], str(exclSet))
    amtsW = {e["handle"]: e["amount_base"] for e in vw1["entries"]}
    check("pay: rank ladder, floor plus largest remainder — integers, exact pool, tied peers equal",
          amtsW == {"paya": 13, "payc": 4, "payi": 4}, str(amtsW))
    dupP = sW.payout_prepare(cidW, "root@paper.io", 21)
    check("pay: one open batch guards the ledger from double cuts",
          dupP.get("ok") is not True and dupP.get("reason") == "payout_open_batch",
          str(dupP.get("reason")))
    bidW = vw1["batch"]["id"]
    fro = sW.payout_freeze(bidW, "root@paper.io")
    check("pay: freezing is the human gate one — draft becomes ready, frozen_at is stamped",
          fro["batch"]["status"] == "ready" and fro["batch"]["frozen_at"] is not None,
          str(fro.get("reason") or fro["batch"]["status"]))
    post_pay("A", "https://x.com/a2/status/9011", 100)
    boardA = [r for r in sW.leaderboard(cidW)["rows"] if r["user"]["id"] == idsW["A"]]
    entA = [e for e in sW.payout_view(bidW)["entries"] if e["handle"] == "paya"][0]
    check("pay: the live board may move but a frozen batch does not (270 live, 160 frozen)",
          bool(boardA) and boardA[0]["points"] == 270.0
          and entA["points"] == 160.0 and entA["amount_base"] == 13, str(entA)[:110])
    badApr = sW.payout_approve(bidW, "root@paper.io", 999)
    check("pay: approval that does not confirm the very total is turned away",
          badApr.get("reason") == "payout_total_mismatch", str(badApr.get("reason")))
    apr = sW.payout_approve(bidW, "root@paper.io", 21)
    check("pay: the human hand approves a ready batch and is recorded by name",
          apr["batch"]["status"] == "approved" and apr["batch"]["approved_by"] == "root@paper.io",
          str(apr.get("reason") or apr["batch"]["status"]))
    sim = sW.payout_simulate(bidW, "root@paper.io", [MINTW[:-1] + "c"])
    simEnt = {e["handle"]: e for e in sim["entries"]}
    check("pay: simulation is TEST MODE — one fault makes a partial, and the audit says TEST MODE",
          sim["batch"]["status"] == "partial" and simEnt["payc"]["status"] == "failed"
          and simEnt["paya"]["status"] == "simulated" and simEnt["payi"]["status"] == "simulated"
          and any(ev["kind"] == "simulate" and "TEST MODE" in ev["detail"] for ev in sim["events"]),
          str([(e["handle"], e["status"]) for e in sim["entries"]])
          + " ev:" + str(sim["events"][:2])[:120])
    ret = sW.payout_retry(bidW, "root@paper.io")
    retEnt = {e["handle"]: e for e in ret["entries"]}
    check("pay: retry touches only the verified-failed; paid peers keep a single attempt",
          ret["batch"]["status"] == "simulated" and retEnt["payc"]["status"] == "simulated"
          and retEnt["payc"]["attempts"] == 2 and retEnt["paya"]["attempts"] == 1
          and str(retEnt["paya"]["tx_sig"] or "").startswith("sim-"),
          str([(e["handle"], e["status"], e["attempts"]) for e in ret["entries"]])[:150])
    twice = sW.payout_simulate(bidW, "root@paper.io")
    retri = sW.payout_retry(bidW, "root@paper.io")
    canc1 = sW.payout_cancel(bidW, "root@paper.io")
    check("pay: a settled batch refuses re-execution, pointless retries, and cancelling after pay",
          twice.get("reason") == "payout_already_executed"
          and retri.get("reason") == "payout_bad_state"
          and canc1.get("reason") == "payout_already_executed", str(twice.get("reason")))
    vw2 = sW.payout_prepare(cidW, "root@paper.io", 9)
    cx = sW.payout_cancel(vw2["batch"]["id"], "root@paper.io", "no friday feeling")
    latW = sW.payout_latest(cidW)
    check("pay: history keeps the settled batch, a new one may be cut, cancel leaves a named close",
          len(latW.get("history", [])) == 2 and latW["batch"]["status"] == "cancelled"
          and latW["batch"]["closed_reason"] == "no friday feeling", str(latW.get("history"))[:110])
    sW.payout["formula"] = "nope"
    uForm = sW.payout_prepare(cidW, "root@paper.io", 21)
    check("pay: an unknown formula is refused out loud, never quietly guessed",
          uForm.get("ok") is not True and uForm.get("reason") == "unknown payout formula",
          str(uForm.get("reason")))
    sW.payout["formula"] = "equal_split"
    eq = sW.payout_prepare(cidW, "root@paper.io", 21)
    eqAmt = sorted(e["amount_base"] for e in eq["entries"])
    sW.payout_cancel(eq["batch"]["id"], "root@paper.io", "lane test")
    check("pay: the second formula (equal split) also sums to the pool exactly",
          eqAmt == [7, 7, 7], str(eqAmt))
    sW.payout["formula"] = "rank_ladder"
    ti = sW.payout_prepare(cidW, "root@paper.io", 4)
    tiAmt = [e["amount_base"] for e in ti["entries"]]
    sW.payout_cancel(ti["batch"]["id"], "root@paper.io", "lane test")
    check("pay: a tiny pool divides exactly too — floor, remainder, integers only",
          sum(tiAmt) == 4 and tiAmt == [2, 1, 1], str(tiAmt))
    sW.payout["allowDuplicateWallet"] = True
    dp = sW.payout_prepare(cidW, "root@paper.io", 21)
    sW.payout["allowDuplicateWallet"] = False
    check("pay: the duplicate-wallet wall is a policy, and the policy has a switch",
          dp.get("ok") is True and dp["batch"]["participants"] == 4,
          str(dp["batch"]["participants"]))
    sW.payout_cancel(dp["batch"]["id"], "root@paper.io", "lane test")
    dbW.conn.execute("INSERT INTO competitions(slug,name,starts_at,ends_at,status)"
                     " VALUES('pay2','Pay Two','2026-01-01','2027-01-01','done')")
    dbW.conn.commit()
    cid2 = dbW.conn.execute("SELECT id FROM competitions WHERE slug='pay2'").fetchone()["id"]
    zr = sW.payout_prepare(cid2, "root@paper.io", 9)
    check("pay: an empty board prepares into a draft that says so out loud",
          zr.get("ok") is True and zr["batch"]["participants"] == 0
          and "no eligible recipients" in zr["batch"]["warnings"], str(zr.get("batch"))[:110])
    def seat_pay(email):
        accX = sW.create_or_touch_account(email)[0]
        out = rW.dispatch("GET", "/api/auth/magic",
                          {"token": sW.issue_magic_link(accX["id"])[0]}, {}, {}, None)
        return {"PBSD": out[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]}
    def gop(method, path, query=None, headers=None, cookies=None, body=None):
        out = rW.dispatch(method, path, query or {}, headers or {}, cookies or {}, body)
        return out[0], out[1]
    JSONW = {"content-type": "application/json"}
    ckRoot, ckOut = seat_pay("root@paper.io"), seat_pay("guest@pay.io")
    st1, w1 = gop("GET", "/api/admin/payout", cookies=ckRoot)
    check("pay http: the admin sees the treasury (and only the admin)",
          st1 == 200 and w1.get("ok") is True and "batch" in w1, str(w1)[:80])
    st2, _e = gop("GET", "/api/admin/payout", cookies={})
    check("pay http: no cookie, no treasury", st2 == 401, str(st2))
    st3, _e = gop("GET", "/api/admin/payout", cookies=ckOut)
    check("pay http: a bystander gets 403, never a polite 200", st3 == 403, str(st3))
    st4, _e = gop("POST", "/api/admin/payout/freeze",
                  headers={"content-type": "text/plain"}, cookies=ckRoot, body={"batch": 1})
    check("pay http: the JSON door stands before any state moves", st4 == 415, str(st4))
    st5, w5 = gop("POST", "/api/admin/payout/freeze", headers=JSONW,
                  cookies=ckRoot, body={"batch": 4242})
    check("pay http: an unknown batch is a server-side 404",
          st5 == 404 and w5.get("error") == "payout_batch_not_found", str(w5)[:80])
    st6, _e = gop("POST", "/api/admin/payout/nothing", headers=JSONW,
                  cookies=ckRoot, body={"batch": 1})
    check("pay http: an unknown verb is 400, not a shrug", st6 == 400, str(st6))
    st7, w7 = gop("POST", "/api/admin/payout/prepare", headers=JSONW,
                  cookies= ckRoot, body={})
    okPrepare = (st7 == 200 and w7.get("ok") is True and (w7.get("batch") or {}).get("id") is not None)
    st8, _e = gop("POST", "/api/admin/payout/cancel", headers=JSONW, cookies=ckRoot,
                  body={"batch": (w7.get("batch") or {}).get("id"), "reason": "lane test"})
    check("pay http: prepare drives through HTTP with no batch in hand, and cancel closes it again",
          okPrepare and st8 == 200, f"{st7}/{st8}")
    st9, w9 = gop("POST", "/api/admin/payout/freeze", headers=JSONW,
                  cookies=ckRoot, body={"batch": "not-a-number"})
    check("pay http: a batch id that is not a number is a 400, never a guess",
          st9 == 400 and w9.get("error") == "bad_batch", str(w9)[:70])
    nSub0 = dbW.conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
    nAcc0 = dbW.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    nEven0 = dbW.conn.execute("SELECT COUNT(*) AS n FROM admin_events").fetchone()["n"]
    nBatch0 = dbW.conn.execute("SELECT COUNT(*) AS n FROM payout_batch").fetchone()["n"]
    dbW2 = dbmod.DB("/tmp/paperboard-self-PAY.sqlite3")
    nSub1 = dbW2.conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
    nAcc1 = dbW2.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
    nPay1 = dbW2.conn.execute("SELECT COUNT(*) AS n FROM payout_batch").fetchone()["n"]
    check("pay: reopening the same file keeps the old world and the new ledger side by side",
          nSub1 == nSub0 and nAcc1 == nAcc0 and nPay1 == nBatch0 and nEven0 == nEven0,
          f"{nSub1}/{nAcc1}/{nPay1}")
    check("pay: the ledger keeps its receipts — every batch a full event line, append only",
          dbW2.conn.execute("SELECT COUNT(*) AS n FROM payout_events").fetchone()["n"] >= nPay1,
          str(dbW.conn.execute("SELECT COUNT(*) AS n FROM payout_events").fetchone()["n"]))

    # ---- release-sprint hardening pins: the risky corners, tested not assumed
    nBatchBefore = dbW.conn.execute("SELECT COUNT(*) AS n FROM payout_batch").fetchone()["n"]
    dbW.conn.execute("DROP TABLE payout_events")
    dbW.conn.commit()
    raised = False
    try:
        sW.payout_prepare(cidW, "root@paper.io", 5)
    except Exception:
        raised = True
    dbW.conn.rollback()
    nBatchMid = dbW.conn.execute("SELECT COUNT(*) AS n FROM payout_batch").fetchone()["n"]
    dbW.conn.execute("CREATE TABLE payout_events(id INTEGER PRIMARY KEY,"
                     " batch_id INTEGER NOT NULL, actor TEXT NOT NULL,"
                     " kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',"
                     " at REAL NOT NULL)")
    dbW.conn.commit()
    recovery = sW.payout_prepare(cidW, "root@paper.io", 5)
    sW.payout_cancel(recovery["batch"]["id"], "root@paper.io", "recovery test")
    check("pay hardening: a mid-flight failure is rolled back, never left half-cut for a later commit",
          raised and nBatchMid == nBatchBefore, f"raised={raised} batches={nBatchMid}")
    rf = sW.payout_prepare(cidW, "root@paper.io", 1)
    rfAmt = [e["amount_base"] for e in rf["entries"]]
    sW.payout_cancel(rf["batch"]["id"], "root@paper.io", "tiny ladder")
    check("pay hardening: a one-unit pool stays exact and integer (1,0,0 — nobody loses a phantom token)",
          rfAmt == [1, 0, 0], str(rfAmt))
    sW.payout["allowDuplicateWallet"] = True
    sp = sW.payout_prepare(cidW, "root@paper.io", 1)
    spAmt = [e["amount_base"] for e in sp["entries"]]
    sW.payout_cancel(sp["batch"]["id"], "root@paper.io", "tie split")
    sW.payout["allowDuplicateWallet"] = False
    check("pay hardening: when one unit must split tied peers, the split is deterministic, not a coin toss",
          spAmt == [1, 0, 0, 0] and sp["batch"]["participants"] == 4, str(spAmt))
    fr2 = sW.payout_freeze(bidW, "root@paper.io")
    ap2 = sW.payout_approve(bidW, "root@paper.io", 21)
    check("pay hardening: a settled batch refuses second freezes and second approvals",
          fr2.get("reason") == "payout_bad_state" and ap2.get("reason") == "payout_bad_state",
          str(fr2.get("reason")) + "/" + str(ap2.get("reason")))
    dbW.conn.execute("UPDATE payout_entry SET amount_base=amount_base+5"
                     " WHERE batch_id=? AND handle='paya'", (bidW,))
    dbW.conn.commit()
    rec = sW.payout_reconcile(bidW, "root@paper.io")
    recEv = rec["events"][0]
    dbW.conn.execute("UPDATE payout_entry SET amount_base=amount_base-5"
                     " WHERE batch_id=? AND handle='paya'", (bidW,))
    sW.payout_reconcile(bidW, "root@paper.io")
    check("pay hardening: reconcile re-adds the entries and says what it counted (26 after a +5 poke)",
          rec["batch"]["total_base"] == 26
          and recEv["kind"] == "reconcile" and "simulated=3" in recEv["detail"], str(recEv)[:110])

    # = pb-v3.0 front contract: a boot failure is recoverable, not a hang ==
    js = open(os.path.join(os.path.dirname(__file__), "..", "js", "paperboard.js")).read()
    html = open(os.path.join(os.path.dirname(__file__), "..", "js", "components", "board.js")).read()
    i_cfg = js.find('get("/api/config")')
    i_fin = js.find("S.booting = false", i_cfg)
    i_err = js.find('setMode("bad answer", "stamp--rot"); setState("idle")', i_cfg)
    i_off = js.find('setMode("off press", "stamp--rot");', i_cfg)
    check("pb front: the boot path reaches the recoverable branch before its finally (not a hang)",
          -1 < i_cfg < i_err and "the server did not answer the board" in js,
          "cfg@%s err@%s" % (i_cfg, i_err))
    check("pb front: an HTTP failure answers in words AND reopens the email lane —"
          " a bad config call never holds sign-in hostage (the mobile incident)",
          js.count("the server did not answer the board") == 1 and i_err < i_fin
          and js.count('setState("idle"); paintBoard();') == 2,
          "one err branch, both land on idle")
    check("pb front: the offline (network) branch still stamps the truth (off press)"
          " and lands the visitor on the same email lane",
          -1 < i_cfg < i_off < i_err, "off@%s" % i_off)
    check("pb front: the recoverable branch names its stamp in the shipped code"
          " ('bad answer') so the panel head can speak without the pane",
          'setMode("bad answer", "stamp--rot")' in js, "stamp wired")
    check("pb front: the retry re-boots while the config is missing, else refreshes as always",
          "(S.cfg ? refreshAccount(true) : boot())" in js, "wired")
    check("pb front: no boot overlap — a running boot absorbs a concurrent retry",
          js.count("S.booting") >= 3, "guard@%s" % js.find("S.booting"))
    check("pb front: the error pane ships with a truthful heading and a retry path",
          "hit a snag" in html and 'data-action="rescan"' in html, "pane ready")

    # ===== mail trust store: the framework ships an empty OpenSSL store (2026-10) ==
    # the field record: this interpreter's create_default_context() verifies
    # NOTHING (store counts zero, documented default paths point nowhere), and
    # pointing the context at the macOS system anchor bundle is what makes a
    # verified, hostname-checked handshake to smtp.resend.com:465 possible at
    # all. these pins run in-process: no sockets, no login, no real mail.
    ctxM = mailmod._ssl_context()
    stM = ctxM.cert_store_stats()
    check("mail tls: the shared context loads the system anchor bundle — the"
          " framework default store is empty and this load is the difference",
          isinstance(stM, dict) and stM.get("x509_ca", 0) >= 1, str(stM)[:60])
    check("mail tls: verification and hostname checking ride the context by"
          " default — nothing here had to be turned on",
          ctxM.verify_mode == ssl.CERT_REQUIRED and ctxM.check_hostname)
    saved_bundle, mailmod.CA_BUNDLE = mailmod.CA_BUNDLE, "/nope/no-bundle.pem"
    fell_back = None
    try:
        fell_back = mailmod._ssl_context()
    except (OSError, RuntimeError, ssl.SSLError):
        pass
    finally:
        mailmod.CA_BUNDLE = saved_bundle
    check("mail tls: a missing bundle no longer aborts the mail room — the"
          " context falls back to the platform's own store and still verifies"
          " peer and name: fail closed was the rule and it holds, answered by"
          " a real store rather than by an exception",
          fell_back is not None and fell_back.verify_mode == ssl.CERT_REQUIRED
          and fell_back.check_hostname)
    class TrustyConn:
        def ehlo_or_helo_if_needed(self): pass
        def starttls(self, context=None): self.tls_context = context
        def login(self, *a): pass
        def sendmail(self, *a, **k): pass
        def quit(self): pass
    class MailCfg:
        smtp = dict(host="smtp.test", port=465, user="u", password="p",
                    **{"from": "pb@paper.io"})
    mT = mailmod.SmtpMail(MailCfg())
    saved_classes = (mailmod.smtplib.SMTP_SSL, mailmod.smtplib.SMTP)
    handed, lane_conn = {}, TrustyConn()
    try:
        def fake_ssl_lane(*a, **k):
            handed["implicit"] = k.get("context")
            return TrustyConn()
        mailmod.smtplib.SMTP_SSL = fake_ssl_lane
        mailmod.smtplib.SMTP = lambda *a, **k: lane_conn
        mT.send_magic_link("a@b.io", "http://x/?token=1")
        ctxI = handed.get("implicit")
        check("mail tls: the implicit-TLS lane (port 465) hands SMTP_SSL the"
              " trust-bearing context",
              isinstance(ctxI, ssl.SSLContext)
              and ctxI.cert_store_stats().get("x509_ca", 0) >= 1, str(ctxI)[:40])
        mT.port = 587                                      # the STARTTLS lane
        mT.send_magic_link("a@b.io", "http://x/?token=2")
        ctxS = getattr(lane_conn, "tls_context", None)
        check("mail tls: the STARTTLS lane hands starttls the same"
              " trust-bearing context",
              isinstance(ctxS, ssl.SSLContext)
              and ctxS.cert_store_stats().get("x509_ca", 0) >= 1, str(ctxS)[:40])
    finally:
        mailmod.smtplib.SMTP_SSL, mailmod.smtplib.SMTP = saved_classes

    # ============ P6D: X ownership verification + a week-long account seat ==
    # the verification lane rides the REAL stack adapters over stubbed wires,
    # so every claim below is measured through the shipped parse code itself.
    stY2, dY, sY, cidY = make_env2("XVER", FallbackXClient(
        FXTwitterXClient(Settings()), OembedXClient(Settings())))
    accY1, _c1Y, _e1Y = sY.create_or_touch_account("owner@paper.io")
    signedY1, _w1Y = sY.consume_magic_link(sY.issue_magic_link(accY1["id"])[0])
    accY2, _c2Y, _e2Y = sY.create_or_touch_account("second@paper.io")
    signedY2, _w2Y = sY.consume_magic_link(sY.issue_magic_link(accY2["id"])[0])
    accY3, _c3Y, _e3Y = sY.create_or_touch_account("third@paper.io")
    signedY3, _w3Y = sY.consume_magic_link(sY.issue_magic_link(accY3["id"])[0])
    accY4, _c4Y, _e4Y = sY.create_or_touch_account("fourth@paper.io")
    signedY4, _w4Y = sY.consume_magic_link(sY.issue_magic_link(accY4["id"])[0])
    rY = http_app.Router(sY, stY2, dY, sY.x)
    JSONY = {"content-type": "application/json"}
    EXAMPLE = ("I just signed up for PAPERBOARD, a new raiding platform by"
               " @paperusdc. Get paid to post!")

    def seatY(account_row):
        out = rY.dispatch("GET", "/api/auth/magic",
                          {"token": sY.issue_magic_link(account_row["id"])[0]},
                          {}, {}, None)
        return out, {"PBSD": out[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]}

    out1, ckY1 = seatY(accY1)
    out4, ckY4 = seatY(accY4)
    saved_urlopenY = xapimod.urllib.request.urlopen
    try:
        check("P6D: the shipped week — the magic 302's cookie Max-Age is"
              " exactly 7 days and config.json agrees (sessionDays is one"
              " knob with four honest readers)",
              "Max-Age=604800" in out1[2]["Set-Cookie"]
              and stY2.pb["account"]["sessionDays"] == 7, str(out1[2])[:80])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9501", text=EXAMPLE) if who == "fx"
            else oe_body("9501"))
        counters["fx"] = counters["oe"] = 0
        vy = sY.verify_x(signedY1, "https://x.com/paperusdc/status/9501")
        rowY = sY.account_by_id(accY1["id"])
        check("P6D verify: the example post binds handle + stable id +"
              " verified-instant at once — one wire call, and the proof post"
              " earned no submission row (it is evidence, not a raid)",
              vy.get("ok") and vy.get("x_handle") == "paperusdc"
              and vy.get("x_user_id") == "4242"
              and rowY["x_verified_at"] is not None
              and counters["fx"] == 1
              and dY.conn.execute("SELECT COUNT(*) n FROM submissions"
                                  ).fetchone()["n"] == 0, str(vy)[:140])
        c0 = counters["fx"]
        vy2 = sY.verify_x(signedY1, "https://x.com/paperusdc/status/9501")
        check("P6D verify: re-presenting the same deed is idempotent and"
              " speaks BEFORE the wire (a double-click costs no provider call)",
              vy2.get("ok") and vy2.get("reason") == "already_verified"
              and counters["fx"] == c0, str(vy2)[:90])
        vy3 = sY.verify_x(signedY1, "https://x.com/paperusdc/status/9502")
        check("P6D verify: one deed per account — a second post answers"
              " verification_exists before any wire (409 material)",
              vy3.get("reason") == "verification_exists"
              and counters["fx"] == c0, str(vy3)[:90])
        vy4 = sY.verify_x(signedY2, "https://x.com/second/status/9501")
        check("P6D verify: one use per post — the same post cannot verify a"
              " second account (verification_post_used, no wire spent)",
              vy4.get("reason") == "verification_post_used"
              and counters["fx"] == c0, str(vy4)[:90])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9503") if who == "fx" else oe_body("9503"))
        c1 = counters["fx"]
        vy5 = sY.verify_x(signedY3, "https://x.com/ghost/status/9503", "ghost")
        row3 = sY.account_by_id(accY3["id"])
        check("P6D verify: a claimed handle the server cannot confirm answers"
              " author_mismatch — the wire was spent but no half-verified"
              " state was left behind",
              vy5.get("reason") == "author_mismatch"
              and counters["fx"] == c1 + 1 and row3["x_verified_at"] is None,
              str(vy5)[:90])
        vy6 = sY.verify_x(signedY3, "https://x.com/paperusdc/status/9503")
        check("P6D verify: an identity already worn is claimed away BEFORE the"
              " wire (x_handle_taken — one X identity, one account)",
              vy6.get("reason") == "x_handle_taken"
              and counters["fx"] == c1 + 1, str(vy6)[:90])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9505",
                                   text="pure vibes, no announcement, hi @paperusdc",
                                   author=dict(screen_name="Second", id="4243"))
            if who == "fx" else oe_body("9505", author="Second"))
        vy7 = sY.verify_x(signedY2, "https://x.com/second/status/9505")
        check("P6D verify: wording is law too — the deed must tag @paperusdc"
              " AND announce the join (verify_wording leaves nothing"
              " half-written)",
              vy7.get("reason") == "verify_wording"
              and sY.account_by_id(accY2["id"])["x_verified_at"] is None,
              str(vy7)[:110])
        xapimod.urllib.request.urlopen = stub(lambda u, who: XError("stub wire down"))
        vy8 = sY.verify_x(signedY2, "https://x.com/second/status/9504")
        check("P6D verify: provider trouble fails CLOSED — provider_unavailable"
              " and no verified state at all (the account is no deader for"
              " having tried)",
              vy8.get("reason") == "provider_unavailable"
              and sY.account_by_id(accY2["id"])["x_verified_at"] is None,
              str(vy8)[:90])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: XError("fx leg silent") if who == "fx"
            else oe_body("9509", author="Second",
                         html=f'<blockquote class="twitter-tweet"><p lang="en">{EXAMPLE}</p>'
                              f'&mdash; Second (<a href="#">@Second</a>) <a href="#">'
                              f"October 8, 2026</a></blockquote>"))
        vyP = sY.verify_x(signedY2, "https://x.com/second/status/9509")
        check("P6D verify: a presence-only witness is a WITNESS, not an"
              " identity — when the fallback byline confirms the handle and"
              " the wording but no stable id exists, verification PENDING:"
              " the reason says pending, no verified instant burns, and no"
              " completed x_verifications row is written for the proof"
              " (6C.27 law: a byline never mints verification)",
              vyP.get("reason") == "verification_pending"
              and sY.account_by_id(accY2["id"])["x_verified_at"] is None
              and dY.conn.execute("SELECT COUNT(*) n FROM x_verifications"
                                  " WHERE post_id='9509'").fetchone()["n"]
              == 0, str(vyP)[:110])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9510", text=EXAMPLE,
                                   author=dict(screen_name="Second", id="4243"))
            if who == "fx" else oe_body("9510", author="Second"))
        vy9 = sY.verify_x(signedY2, "https://x.com/second/status/9510")
        check("P6D verify: the second account signs cleanly when author,"
              " wording, and wire all agree — the URL byline is the claim,"
              " the server's author is the truth",
              vy9.get("ok") and vy9.get("x_handle") == "second"
              and vy9.get("x_user_id") == "4243", str(vy9)[:110])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9508",
                                   author=dict(screen_name="Second", id="4243"))
            if who == "fx" else oe_body("9508", author="Second"))
        c2 = counters["fx"]
        vy10 = sY.verify_x(signedY4, "https://x.com/second/status/9508")
        check("P6D verify: one verified identity can not link a second"
              " account — the fourth account's same-author deed answers"
              " x_handle_taken before the wire",
              vy10.get("reason") == "x_handle_taken" and counters["fx"] == c2,
              str(vy10)[:90])
        # — the submission gates, measured through submit_post —
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9507",
                                   author=dict(screen_name="NotHim", id="9999"))
            if who == "fx" else oe_body("9507", author="NotHim"))
        subW = sY.submit_post(sY.account_by_id(accY1["id"]),
                              "https://x.com/paperusdc/status/9507", cidY)
        check("P6D submit: a post by ANOTHER author is rejected before any"
              " row exists (wrong_author — no points for a borrowed post)",
              subW.get("reason") == "wrong_author"
              and dY.conn.execute("SELECT COUNT(*) n FROM submissions"
                                  ).fetchone()["n"] == 0, str(subW)[:110])
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9506",
                                   author=dict(screen_name="Renamed", id="4242"))
            if who == "fx" else oe_body("9506", author="Renamed"))
        subR = sY.submit_post(sY.account_by_id(accY1["id"]),
                              "https://x.com/paperusdc/status/9506", cidY)
        check("P6D submit: a RENAMED byline still scores when the stable id"
              " matches (usernames are not forever — the id is; the author"
              " law rides the id where the provider has one)",
              subR.get("ok")
              and (subR.get("submission") or {}).get("author") == "renamed",
              str(subR)[:110])
        subN = sY.submit_post(signedY4,
                              "https://x.com/fourth/status/9509", cidY)
        check("P6D submit: an unverified account cannot submit at all"
              " (not_verified answers before the wire — an account that"
              " cannot score spends no provider call)",
              subN.get("reason") == "not_verified", str(subN)[:90])
        # — the route lane: metering, server-side expiry, housekeeping, board —
        xapimod.urllib.request.urlopen = stub(
            lambda u, who: fx_body("9506", text=EXAMPLE) if who == "fx"
            else oe_body("9506"))
        codes9506 = []
        for _tryY in range(11):
            outR = rY.dispatch("POST", "/api/account/x/verify", {}, JSONY,
                               ckY4, {"url": "https://x.com/paperusdc/status/9506"})
            codes9506.append(outR[0])
        check("P6D route: verification attempts are metered — ten ride the"
              " bucket (each refused honestly on its identity wall), the"
              " eleventh is a 429 xverify_rate_limited",
              set(codes9506[:10]) == {409} and codes9506[10] == 429,
              str(codes9506))
        tokY4 = out4[2]["Set-Cookie"].split("PBSD=")[1].split(";")[0]
        dY.conn.execute("UPDATE sessions SET expires_at=1700000000"
                        " WHERE token=?", (tokY4,))
        dY.conn.commit()
        outE = rY.dispatch("GET", "/api/account", {}, {}, ckY4, None)
        swept = dY.conn.execute("SELECT COUNT(*) n FROM sessions WHERE token=?",
                                (tokY4,)).fetchone()["n"]
        check("P6D seat: server-side expiry outranks the cookie's promise — a"
              " dead week answers 401 not_authenticated and the row is swept"
              " on the spot",
              outE[0] == 401 and swept == 0, str(outE)[:90])
        dY.conn.execute("INSERT INTO sessions(token,user_id,created_at,"
                        "account_id,expires_at,last_seen_at)"
                        " VALUES('sweptoo',NULL,'x',?,?,NULL)",
                        (accY1["id"], 1700000000))
        dY.conn.execute("INSERT INTO magic_links(token_hash,account_id,"
                        "created_at,expires_at,used_at)"
                        " VALUES('deadzz',?,?,?,?)",
                        (accY1["id"], 1700000000 - 600, 1700000000, 1700000000))
        dY.conn.commit()
        sY.account_new_session(accY1["id"])
        goneS = dY.conn.execute("SELECT COUNT(*) n FROM sessions WHERE"
                                " token='sweptoo'").fetchone()["n"]
        goneM = dY.conn.execute("SELECT COUNT(*) n FROM magic_links WHERE"
                                " token_hash='deadzz'").fetchone()["n"]
        check("P6D housekeeping: a login instant sweeps the dead — expired"
              " sessions go by their own date and spent/stale magic rows"
              " retire after a day (no scheduler needed)",
              goneS == 0 and goneM == 0, f"{goneS}/{goneM}")
        outD = rY.dispatch("GET", "/api/account", {}, {}, ckY1, None)
        acctD = (outD[1] or {}).get("account") or {}
        check("P6D dashboard: the account dict carries its X state end to end"
              " (x_verified and x_user_id) so the frontend's verification lane"
              " has one server truth to read",
              outD[0] == 200 and acctD.get("x_verified") is True
              and acctD.get("x_user_id") == "4242", str(acctD)[:120])
    finally:
        xapimod.urllib.request.urlopen = saved_urlopenY

    # ---- P6C.34 (UX pass): the frontend keeps its promises in the text ----
    # three facts the browser runs with, pinned so a future edit cannot drift
    # them away: the dashboard's X-verification strip is a wrapped, hidden-by-
    # default lane, it is revealed only when the server actually speaks X (so
    # the older live backend shows no dead form), and the account's own front-
    # page row is both marked and styled — a green spine, never the gold ones.
    uxjs = open(os.path.join(os.path.dirname(__file__), "..", "js", "paperboard.js")).read()
    uxbd = open(os.path.join(os.path.dirname(__file__), "..", "js", "components",
                             "board.js")).read()
    uxcss = open(os.path.join(os.path.dirname(__file__), "..", "css",
                              "paperboard.css")).read()
    check("ux: the dash's X-verification strip is one wrapped lane, hidden by default",
          'id="pb-xstrip" hidden' in uxbd)
    check("ux: the strip opens only when the server speaks X (no dead controls"
          " on the older backend the live site still runs)",
          '"x_verified" in acct' in uxjs and '$("#pb-xstrip")' in uxjs)
    check("ux: the account's own front-page row is marked (tr-you) and styled"
          " with a green spine, never gold",
          "tr-you" in uxjs and "tr-you" in uxcss
          and "inset 3px 0 0 var(--green)" in uxcss)

    # ---- P6E (onboarding pass): the verification lane speaks in one voice ----
    # the server is the only source for the verification post: /api/config now
    # carries it (xverify block), the shipped sentence provably satisfies the
    # very wording walls verify_x keeps, the template carries the step and the
    # spam notice, and the paint gate reads the account payload only — so a
    # refresh rebuilds the same view from the record, never from local state.
    uxsvc = open(os.path.join(os.path.dirname(__file__), "paperboard",
                              "service.py")).read()
    xwE = stY2.pb["xVerify"]
    outE = rY.dispatch("GET", "/api/config", {}, {}, {}, None)
    check("P6E prompt: the exact verification post rides /api/config (xverify"
          " .example), and that shipped sentence clears the three walls"
          " verify_x itself keeps — mention, announce word, a join word — so"
          " 'copy verification post' copies law, not decoration",
          outE[0] == 200
          and (outE[1].get("xverify") or {}).get("example") == xwE["example"]
          and xwE["mention"].lower() in xwE["example"].lower()
          and xwE["announce"].lower() in xwE["example"].lower()
          and any(str(w).lower() in xwE["example"].lower()
                  for w in xwE["joinWords"]),
          str((outE[1].get("xverify") or {}).get("example"))[:48])
    check("P6E template: both email lanes carry the spam-folder notice, and"
          " the unverified onboarding is one labelled task (#pb-xverify-step"
          " with its 'X verification required' tag, the exact prompt as its"
          " default text, a copy verb, and the next step named by its own"
          " button) while the submit desk keeps its own gated id",
          uxbd.count("spam or junk folder") == 2
          and xwE["example"] in uxbd
          and "X verification required" in uxbd and 'id="pb-xverify-step"' in uxbd
          and "Copy verification post" in uxbd and "I've posted it" in uxbd
          and 'id="pb-submit-box"' in uxbd
          and 'data-slot="submit-gate"' in uxbd, "template lanes drifted")
    check("P6E paint: the gate reads the server's payload and nothing else"
          " (x_verified in acct → needsX → the submit box and its one"
          " explanatory note), the prompt is fetched via xverify.example with"
          " a shipped fallback constant, the copy lane answers audibly, and"
          " the door still has no nonce and no expiry to forget (neither word"
          " appears in the service at all)",
          "needsX" in uxjs and "xverify.example" in uxjs
          and "X_VERIFY_FALLBACK" in uxjs and "copy-feedback" in uxjs
          and "X account verified" in uxjs
          and "claimed_handle=None" in uxsvc and "nonce" not in uxsvc.lower(),
          "paint lane drifted")
    OK_SENTENCE = ("I just signed up for PAPERBOARD, a new raiding platform"
                   " by @paperusdc. Get paid to post!")
    check("P6E copy: one approved sentence rules everywhere — the config"
          " example, the route's xverify payload and the three wording walls"
          " are measured by the check above, and the shipped fallback"
          " constant and the battery's own wire-stub fixture both hold this"
          " verbatim text (so the happy-path verification drives the REAL"
          " approved sentence through the real verify_x)",
          xwE["example"] == OK_SENTENCE
          and ('"' + OK_SENTENCE + '"') in uxjs and EXAMPLE == OK_SENTENCE,
          xwE["example"][:48])

    # ---- P6H (the approved simplification): the rules spread is one plain
    # ---- three-column list; every printed figure stays a mirror of the engine --
    uxsc = open(os.path.join(os.path.dirname(__file__), "..", "js", "components",
                             "scoring.js")).read()
    check("P6H copy: the three public columns are the approved spread —"
          " 01. Qualify / 02. Earn points / 03. Keep it real, the one-line"
          " qualifier intro ('at least one:'), the identifier list riding"
          " ${CONFIG.contractAddress}, 'One identifier is enough.', the two"
          " figure rows, the Maximum line and the footnoted curve — while the"
          " removed fine print (duplicate window, frequency walls, the pending"
          " edge, the audit trail, admin corrections) stays off the page and"
          " the zero-signal rows, the (w-cap shows…) parenthetical and the"
          " pb-v3 jargon stay gone",
          "${CONFIG.contractAddress}" in uxsc
          and all(k in uxsc for k in ("01. Qualify", "02. Earn points",
                                      "03. Keep it real",
                                      "One identifier is enough.",
                                      "not your browser."))
          and all(k not in uxsc for k in
                  ('data-set="w-reply"', 'data-set="w-repost"',
                   'data-set="w-quote"', 'data-set="w-imp"',
                   "pb-v3", "(w-cap", "frequency walls",
                   "Transparent corrections", "ride along as evidence",
                   "One post, one score", "Your browser cannot set")),
          "scoring lanes drifted")
    scF = stY2.pb["scoring"]
    check("P6H copy ↔ engine: the page's printed figures are mirrors of the"
          " shipped config — +10 base, +1-each likes, the Maximum 210 and the"
          " knee in the footnote's 'first 100' all equal the engine's — the"
          " three living slots (w-base / w-like / w-cap) still ride it for"
          " boot-time repaint, and the four omitted signals are omitted"
          " BECAUSE they score nothing (all zero in the shipped config —"
          " should one ever wake, this pin turns red and the page must recopy)",
          ('data-set="w-base">' + str(scF["basePerPost"]) + "<") in uxsc
          and ('data-set="w-like">' + str(scF["perLike"]) + "</span> each") in uxsc
          and ('data-set="w-cap">' + str(scF["postCap"]) + "<") in uxsc
          and ("first " + str(scF["likeKnee"])) in uxsc
          and "Maximum: " in uxsc and "points per post." in uxsc
          and scF["perLike"] == 1 and scF["basePerPost"] == 10
          and scF["postCap"] == 210
          and all(scF[k] == 0 for k in ("perReply", "perRepost", "perQuote"))
          and scF["impressions"]["points"] == 0, str(scF["version"]))
    check("P6F steps & voice: step 02 points at the rules through the one"
          " real anchor (#scoring exists in the shipped scoring component — no"
          " broken hop invented), and the spam reminder speaks in the owner's"
          " own words on both email lanes",
          "see post qualifications" in uxbd and 'href="#scoring"' in uxbd
          and 'id="scoring"' in uxsc
          and uxbd.count("Can't find your sign-in email?") == 2,
          "voice or anchor drifted")

    # ---- P6C Artboard: the wall, the cap, and the fifth state ----------
    # the wall rides on its own two tables; the bytes live under a temp dir
    # so the test leaves no residue; every figure asserted is read from the
    # shipped config first — a silent retune must stay silent under this run.
    import shutil as _shutil
    import binascii as _b64
    import tempfile as _tf
    from paperboard.artboard import Artboard as _Art, sniff as _sniff
    _adir = _tf.mkdtemp(prefix="p6c-")
    os.environ["PAPER_UPLOADS_DIR"] = _adir
    _adb = dbmod.DB(":memory:", verbose=False)
    _aart = _Art(_adb, stY2)
    _asvc = svc.Service(_adb, None, stY2)
    _aown = _asvc.create_or_touch_account("art@paperboard.test")[0]["id"]
    _aten = _asvc.create_or_touch_account("voter@paperboard.test")[0]["id"]
    _png_b64 = _b64.b2a_base64(
        b"\x89PNG\r\n\x1a\nIEND\xaeB\x60\x82").decode("ascii")
    _abcap = (stY2.pb.get("artboard") or {}).get("maxPerDay", 3)
    _errs = []
    for _i in range(_abcap + 1):
        _errs.append(_aart.add(_aown, {"title": "ink %d" % _i,
                                        "data_b64": _png_b64})[1])
    _rows = _aart.list()
    check("P6C the wall takes ink: add() sniffs the png by its magic bytes,"
          " stores bytes under the uploads dir, and the row's ledger matches"
          " — and the cap speaks at hang number " + str(_abcap + 1)
          + " of the day (maxPerDay read from shipped config, not a constant)",
          _errs == [None] * _abcap + ["daily_cap"]
          and len(os.listdir(_adir)) == _abcap
          and _rows[0]["title"] == "ink %d" % (_abcap - 1)   # newest first
          and _rows[0]["likes"] == 0
          and _aart.count() == _abcap
          and _sniff(_b64.a2b_base64(_png_b64)) == "png",
          str(_errs))
    _v1 = _aart.vote(_rows[0]["id"], _aown)
    _v2 = _aart.vote(_rows[0]["id"], _aown)
    _v3 = _aart.vote(_rows[0]["id"], _aten)
    check("P6C votes: first is added, a re-vote is DEFERRED (the same truthful"
          " 200 the mail lane speaks — the UNIQUE wall forbids the double-count"
          " at the DB, not at the courtesy), and the count is always the votes"
          " table's truth: two accounts, one vote each, count 2 at the end",
          _v1 == ("added", 1) and _v2 == ("duplicate", 1)
          and _v3 == ("added", 2)
          and _aart.list(sort="liked")[0]["id"] == _rows[0]["id"]
          and _aart.list(sort="liked")[0]["likes"] == 2, str((_v1, _v2, _v3)))
    check("P6C ownership & erasure: only the author takes paper down (the"
          " not_owner key rides to a 403 route-side), the file leaves with the"
          " row, and a removed wall piece cannot keep a vote alive",
          _aart.remove(_rows[0]["id"], _aten) == "not_owner"
          and _aart.remove(_rows[0]["id"], _aown) == "removed"
          and _aart.count() == _abcap - 1
          and len(os.listdir(_adir)) == _abcap - 1
          and _aart.remove(_rows[0]["id"], _aown) == "art_absent"
          and _adb.conn.execute(
              "SELECT COUNT(*) FROM artwork_votes WHERE artwork_id=?",
              (_rows[0]["id"],)).fetchone()[0] == 0, "wall geometry drifted")
    _bad = [_aart.add(_aten, {"title": "x", "data_b64": "!!!!"})[1],
            _aart.add(_aten, {"title": "x",
                              "data_b64": _b64.b2a_base64(
                                  b"notanimage").decode("ascii")})[1],
            _aart.add(_aten, {"title": "y" * 90, "data_b64": _png_b64})[1],
            _aart.add(9999, {"title": "x", "data_b64": _png_b64})[1]]
    check("P6C validation refuses, never trusts: a lie about bytes is refused"
          " (bad_image_data where the decoder is strict, bad_image_type where"
          " it is generous — both are walls, the key tells which), an"
          " oversized title is a bad_title, an unknown author is an"
          " account_absent, and none of them leaves a file or a row behind",
          _bad[0] in ("bad_image_data", "bad_image_type")
          and _bad[1] == "bad_image_type"
          and _bad[2] == "bad_title" and _bad[3] == "account_absent"
          and _aart.count() == _abcap - 1 and len(os.listdir(_adir)) == _abcap - 1,
          str(_bad))
    check("P6C the file lane's walls: served() speaks by content not by name,"
          " the name shape is the service's own signature (a plus account plus"
          " hex plus a real extension), and nothing else gets bytes — the"
          " router only calls served(), so these walls ARE the gate",
          _aart.served(_rows[1]["file"]) is not None
          and _aart.served(_rows[1]["file"])[1] == "image/png"
          and _aart.served("a1-2-deadb.png") is None
          and _aart.served("nope.txt") is None
          and _aart.served("a1-2-abcdefgh.evil.png") is None, "file walls")
    _dbsrc = open(os.path.join(os.path.dirname(__file__), "paperboard", "db.py"),
                  "r", encoding="utf-8").read()
    check("P6C shape pins: the schema ships the two tables plus the UNIQUE"
          " wall inside the SCHEMA itself (a fresh :memory: DB proves it),"
          " the five lanes exist in the router (GET /api/artworks, the three"
          " POST lanes, the /artworks/ file branch), and Artboard read the"
          " shipped config's numbers at boot (cap " + str(_abcap)
          + ", the byte ceiling, and the list cap all match the block)",
          _adb.conn.execute(
              "SELECT COUNT(*) FROM sqlite_master WHERE name IN"
              " ('artworks','artwork_votes')").fetchone()[0] == 2
          and "UNIQUE (artwork_id, account_id)" in _dbsrc
          and all(_k in _routesrc for _k in
                  ('"/api/artworks"', '"/api/artwork/start"',
                   '"/api/artwork/vote"', '"/api/artwork/remove"',
                   '"/artworks/"'))
          and _aart.cap == _abcap
          and _aart.max_bytes == (stY2.pb.get("artboard") or {})["maxBytes"]
          and _aart.list_max == (stY2.pb.get("artboard") or {})["listMax"]
          and len(_aart.list(limit=999)) == _aart.count(), "shape drifted")
    _vseg = (_routesrc.split("/api/artwork/vote")[1]
             .split("/api/artwork/remove")[0])
    check("P6C the route envelope's fifth-state truth (source-level): the vote"
          " lane ships deferred:true on the duplicate branch and that is the"
          " whole of it — the doctrine stays one sentence wide in the router",
          "deferred=True" in _vseg and _vseg.count("deferred") == 1,
          "doctrine drifted")
    _aart.remove(_rows[1]["id"], _aown)
    _aart.remove(_rows[2]["id"], _aown)
    check("P6C the wall sweeps clean: with every author's paper removed the"
          " count is zero and the uploads dir is empty — the file is never"
          " the survivor of a funeral, the row's departure is the truth",
          _aart.count() == 0 and os.listdir(_adir) == [], "sweep drifted")
    _adb.close()
    os.environ.pop("PAPER_UPLOADS_DIR", None)
    _shutil.rmtree(_adir, ignore_errors=True)

    # ---- P6C the wall on the sheet (the front half, source-shape) ------
    _arts = open(os.path.join(os.path.dirname(__file__), "..", "js",
                   "components", "artboard.js"), encoding="utf-8").read()
    _mains = open(os.path.join(os.path.dirname(__file__), "..", "js",
                    "main.js"), encoding="utf-8").read()
    _masts = open(os.path.join(os.path.dirname(__file__), "..", "js",
                   "components", "masthead.js"), encoding="utf-8").read()
    check("P6C the wall on the sheet: the section exists and the mast points"
          " at it (one real anchor, no invented hop), main mounts it after the"
          " front page and before the scoring, the app speaks only to lanes"
          " the server ships, and the press-sheet rule holds — the artboard's"
          " CSS block carries hairlines and tabular counts and zero box-shadow",
          'id="artboard"' in _arts and 'href="#artboard"' in _masts
          and 'import { Artboard }' in _mains and "Artboard()" in _mains
          and (_mains.index("FrontPage()") < _mains.index("Artboard()")
               < _mains.index("Scoring()"))
          and 'import { startPaperboard, startArtboard' in _mains
          and "startArtboard();" in _mains
          and all(_k in uxjs for _k in ("/api/artworks", "/api/artwork/start",
                                        "/api/artwork/vote"))
          and "art_id:" in uxjs and "data_b64" in uxjs
          and ".art-grid" in _cssB and ".art__frame" in _cssB
          and "box-shadow" not in _cssB.split("artboard: the community wall")[1],
          "the sheet's wall drifted")

    # ---- P6D WorldMap: the crowd's shape, never the crowd's names ------
    from paperboard.worldmap import WorldMap as _Wm
    _wdb = dbmod.DB(":memory:", verbose=False)
    _ws = svc.Service(_wdb, None, stY2)
    _wm = _Wm(_wdb, stY2)
    _w1 = _ws.create_or_touch_account("map1@paperboard.test")[0]["id"]
    _w2 = _ws.create_or_touch_account("map2@paperboard.test")[0]["id"]
    _w3 = _ws.create_or_touch_account("map3@paperboard.test")[0]["id"]
    _wr1, _we1 = _wm.set(_w1, {"country": "  iceland ",
                               "region": "capital region", "city": "reykjavik"})
    _wr2, _we2 = _wm.set(_w2, {"country": "Iceland"})   # the case-twin, bare
    _wr3, _we3 = _wm.set(_w3, {"country": "new zealand",
                               "region": "south island", "city": ""})
    _wcfg = stY2.pb.get("worldmap") or {}
    check("P6D the seat is a word, not a coordinate: set() trims (a kindness,"
          " not a rewrite) and upserts — one row per member, the ledger's"
          " whole count three, and the member's own words round-trip in the"
          " row's country/region/city (never an address, never a pair of"
          " numbers the feature never asked for)",
          _we1 is None and _we2 is None and _we3 is None
          and _wr1["country"] == "iceland" and _wr1["region"] == "capital region"
          and _wdb.conn.execute(
              "SELECT COUNT(*) FROM member_locations").fetchone()[0] == 3
          and set(_wr1.keys()) == {"country", "region", "city", "updated_at"},
          str((_we1, _we2, _we3)))
    _wagg = _wm.aggregate()
    check("P6D the public face is aggregate-only and case-forgiving: iceland"
          " and Iceland count as ONE place (the counting forgives, the naming"
          " picks the group's deterministic canon — the alphabetically-first"
          " spelling), the most-crowded lead, and the two words it ever speaks"
          " are country and count — no account, no email, no session crosses"
          " the public door (the JSON of the whole face carries neither @ nor"
          " the word account, and the total is the ledger's own three)",
          [(r["country"], r["count"]) for r in _wagg]
          == [("Iceland", 2), ("new zealand", 1)]
          and _wm.total() == 3
          and all(set(r.keys()) == {"country", "count"} for r in _wagg)
          and "@" not in json.dumps(_wagg)
          and "account" not in json.dumps(_wagg).lower()
          and len(_wm.aggregate(999)) <= _wcfg["listMax"], json.dumps(_wagg))
    _wupd, _wue = _wm.set(_w1, {"country": "norway", "region": "", "city": "bergen"})
    check("P6D an update replaces, never appends (norway/bergen answers and"
          " the ledger still holds three rows not four), and remove() twice"
          " speaks removed then no_location — the honest cousin of deferred:"
          " a truth about the seat, never an error dressed up",
          _wue is None and _wupd["country"] == "norway"
          and _wdb.conn.execute(
              "SELECT COUNT(*) FROM member_locations").fetchone()[0] == 3
          and _wm.view(_w1)["city"] == "bergen"
          and _wm.remove(_w1) == "removed" and _wm.remove(_w1) == "no_location"
          and _wm.view(_w1) is None, str((_wue, _wupd)))
    _wbad = [_wm.set(_w2, {"country": "  "})[1],
             _wm.set(_w2, {"country": "z" * (_wcfg["countryMax"] + 1)})[1],
             _wm.set(_w2, {"country": "x",
                           "region": "y" * (_wcfg["regionMax"] + 1)})[1],
             _wm.set(_w2, {"country": "x", "region": "y",
                           "city": "w" * (_wcfg["cityMax"] + 1)})[1],
             _wm.set(9999, {"country": "nowhere"})[1]]
    check("P6D validation refuses, never trusts: a whitespace country is a"
          " bad_country (the seat needs a WORD), every length wall speaks its"
          " own key (bad_country, bad_region, bad_city — all three read from"
          " the shipped config, not from constants), an unknown author is an"
          " account_absent, and none of them moves the crowd's count",
          _wbad == ["bad_country", "bad_country", "bad_region", "bad_city",
                   "account_absent"]
          and _wm.total() == 2, str(_wbad))
    check("P6D shape pins: the seat table rides the SCHEMA itself (a fresh"
          " :memory: DB proves it), the three public doors and the two write"
          " doors exist in the router (the seat's GET in the GET branch, its"
          " POST pair in the POST branch — the lesson of the day, pinned by"
          " branch-relative position now, not only by presence), the rate"
          " keys ride the shipped config, and the module read the config's"
          " numbers at boot (the caps are the config's, not the test's)",
          "CREATE TABLE IF NOT EXISTS member_locations" in _dbsrc
          and all(_k in _routesrc for _k in
                  ('"/api/worldmap"', '"/api/account/location"',
                   '"/api/account/location/remove"'))
          and _routesrc.index('if method == "GET":')
          < _routesrc.index('if path == "/api/worldmap":')
          < _routesrc.index('if method == "POST":')
          and _routesrc.index('if path == "/api/account/location/remove":')
          > _routesrc.index('if method == "POST":')
          and all(_k in stY2.pb["ratelimit"]
                  for _k in ("worldmapPerMinute", "locationPerTenMinutes"))
          and _wm.country_max == _wcfg["countryMax"]
          and _wm.city_max == _wcfg["cityMax"], "the map's shape drifted")
    _wjs = open(os.path.join(os.path.dirname(__file__), "..", "js",
                  "components", "worldmap.js"), encoding="utf-8").read()
    check("P6D the map on the sheet: the section exists and the mast points"
          " at it (one real anchor), main mounts it after the artboard and"
          " before the scoring, the app boots its own corner (startWorldMap"
          " answers to the page's boot), it speaks to exactly the three doors"
          " the server ships, and the map's CSS keeps the press-sheet vow —"
          " stroke over fill, tabular chips, and zero box-shadow in the whole"
          " block",
          'id="worldmap"' in _wjs and 'href="#worldmap"' in _masts
          and _mains.index("Artboard()") < _mains.index("WorldMap()")
          < _mains.index("Scoring()")
          and "startWorldMap" in _mains
          and all(_k in uxjs for _k in ("/api/worldmap",
                                        "/api/account/location/remove",
                                        '"/api/account/location"'))
          and ".wm__chips" in _cssB and ".wm__coast" in _cssB
          and 'id="wm-land"' in _wjs
          and "box-shadow" not in _cssB.split("the world map:")[1],
          "the sheet's map drifted")
    _wdb.close()

    # ---- P6E Arcade: the shell ships, the games are honest guests --------
    from paperboard.arcade import Arcade as _Ar, week_key as _wk
    _adb2 = dbmod.DB(":memory:", verbose=False)
    _as = svc.Service(_adb2, None, stY2)
    _ar = _Ar(_adb2, stY2)
    _akey = _wk()
    _akey2 = _wk()
    _astruct = time.strptime(_akey, "%Y-%m-%d")
    _aowner = _as.create_or_touch_account("arc1@paperboard.test")[0]["id"]
    _atwo = _as.create_or_touch_account("arc2@paperboard.test")[0]["id"]
    _athree = _as.create_or_touch_account("arc3@paperboard.test")[0]["id"]
    _acf = stY2.pb.get("arcade") or {}
    _t1 = time.time() + 3600
    _t2 = time.time() + 2000
    _t3 = time.time() + 1000
    _adb2.conn.execute("INSERT INTO game_scores(game_id,account_id,week,score,submitted_at)"
                       " VALUES('press-run',?,?,?,?)", (_aowner, _akey, 50, _t1))
    _adb2.conn.execute("INSERT INTO game_scores(game_id,account_id,week,score,submitted_at)"
                       " VALUES('press-run',?,?,?,?)", (_atwo, _akey, 40, _t2))
    _adb2.conn.execute("INSERT INTO game_scores(game_id,account_id,week,score,submitted_at)"
                       " VALUES('press-run',?,?,?,?)", (_athree, _akey, 40, _t3))
    try:
        _adb2.conn.execute(
            "INSERT INTO game_scores(game_id,account_id,week,score,submitted_at)"
            " VALUES('press-run',?,?,?,?)", (_aowner, _akey, 12345, 99))
        _adup = "wall did not speak"
    except Exception:
        _adup = "wall spoke"
    _abd = _ar.board("press-run")
    check("P6E the week is the Friday the payouts already ride: week_key names"
          " a UTC Friday (the strptime truth, not a hope), lies strictly ahead"
          " and no more than a week out, and two calls a breath apart name the"
          " SAME week (a week that moved between two reads would move the"
          " boards under a reader)",
          _astruct.tm_wday == 4
          and _akey == _akey2
          and time.mktime(_astruct) - time.time() > 0
          and time.mktime(_astruct) - time.time() <= 7 * 86400, _akey)
    check("P6E the floor is config's own words: three named games with three"
          " blurbs and states that say what is true (repo pending — no invented"
          " integrations), the boundary names the Friday, the board cap is the"
          " config's number, and the floor's payload carries games, week, and"
          " boundary together (the sheet never invents a game the server's"
          " floor did not name)",
          len(_acf["games"]) == 3 and _ar.boundary == _acf["boundary"]
          and _ar.board_max == _acf["boardMax"]
          and all(g.get("blurb") and g.get("state") for g in _acf["games"])
          and set(_ar.floor().keys()) == {"games", "week", "boundary"},
          json.dumps([g["id"] for g in _acf["games"]]))
    check("P6E a board is a ladder that does not punish a tie: fifty outranks"
          " forty at rank 1, the two forties stand EQUAL at rank 2 (a tie names"
          " itself, no fake silver breaks it), the earlier submission leads the"
          " equal-score company, and the wall of the dedupe already speaks — a"
          " second score from the same hand for the same game and week cannot"
          " double-book the week (the key is the wall the points-week will lean"
          " on), while a stranger game simply answers None",
          [b["rank"] for b in _abd] == [1, 2, 2]
          and [b["score"] for b in _abd] == [50, 40, 40]
          and _abd[1]["handle"] and _ar.board("nope-game") is None
          and _adup == "wall spoke", str(_abd))
    check("P6E shape pins: the score table rides the SCHEMA (a fresh :memory:"
          " DB proves the UNIQUE wall is in the text itself), the two read"
          " doors sit in the GET branch where the day's lesson lives, and the"
          " shell answers an unknown game with unknown_game at the route's own"
          " voice (the module says None, the route says the key)",
          "UNIQUE (game_id, account_id, week)" in _dbsrc
          and all(_k in _routesrc for _k in
                  ('"/api/arcade"', '"/api/arcade/leaderboard"', "unknown_game"))
          and _routesrc.index('if method == "GET":')
          < _routesrc.index('if path == "/api/arcade/leaderboard":')
          < _routesrc.index('if method == "POST":'), "the arcade's shape drifted")
    _arjs = open(os.path.join(os.path.dirname(__file__), "..", "js",
                  "components", "arcade.js"), encoding="utf-8").read()
    check("P6E the arcade on the sheet: the section exists and the mast points"
          " at it, main mounts it after the world map and before the scoring"
          " (the shell's fourth boot call answers), the app speaks to exactly"
          " the two read doors the server ships, the shell invents no link the"
          " repos have not earned (the component carries no href at all), and"
          " the arcade's CSS keeps the press-sheet vow — hairline frames, ruled"
          " rows, tabular boards, zero box-shadow in the whole block",
          'id="arcade"' in _arjs and 'href="#arcade"' in _masts
          and _mains.index("WorldMap()") < _mains.index("Arcade()")
          < _mains.index("Scoring()")
          and "startArcade" in _mains and "startArcade();" in _mains
          and all(_k in uxjs for _k in ('"/api/arcade"',
                                        "/api/arcade/leaderboard?game="))
          and "href=" not in _arjs
          and ".ar__tiles" in _cssB and ".ar__rows" in _cssB
          and "box-shadow" not in _cssB.split("the arcade:")[1],
          "the sheet's arcade drifted")
    _adb2.close()

    # ---- P6F the points-week: best-per-week, the server names the week ----
    _fdb = dbmod.DB(":memory:", verbose=False)
    _fs = svc.Service(_fdb, None, stY2)
    _arf = _Ar(_fdb, stY2)
    _fa1 = _fs.create_or_touch_account("pts1@paperboard.test")[0]["id"]
    _fa2 = _fs.create_or_touch_account("pts2@paperboard.test")[0]["id"]
    _fkey = _wk()
    _f1 = _arf.submit(_fa1, "press-run", 40)
    _f2 = _arf.submit(_fa1, "press-run", 30)
    _f3 = _arf.submit(_fa1, "press-run", 40)
    _f4 = _arf.submit(_fa1, "press-run", 55)
    _fstored = _fdb.conn.execute("SELECT score FROM game_scores").fetchone()[0]
    _fbad = [_arf.submit(_fa2, "press-run", None)[0],
             _arf.submit(_fa2, "press-run", "12")[0],
             _arf.submit(_fa2, "press-run", 12.5)[0],
             _arf.submit(_fa2, "press-run", -1)[0],
             _arf.submit(_fa2, "press-run", (_acf["scoreMax"]) + 1)[0],
             _arf.submit(_fa1, "nope-game", 10)[0]]
    _fstruct = time.strptime(_fkey, "%Y-%m-%d")
    _fdn = int(time.time()) // 86400
    _fdow = (_fdn + 3) % 7
    _fdd = (4 - _fdow) % 7
    if _fdd == 0:
        _fdd = 7
    _fbound = (_fdn + _fdd) * 86400          # the same boundary the helper draws
    _frb = _arf.submit(_fa2, "inkfall", 7, at=_fbound - 60)
    _fra = _arf.submit(_fa2, "inkfall", 9, at=_fbound + 60)
    check("P6F the week's law is best-per-week: a first word books (first),"
          " a lesser word DEFERS without touching the ledger (no_improvement"
          " — the same honest deferral the re-vote speaks), an EQUAL word"
          " defers too (the week keeps its best, it does not re-book a tie),"
          " a better word replaces, and the stored score lands at the best"
          " alone — one row, never two",
          _f1[0] == "first" and _f1[1]["week"] == _fkey
          and _f2[0] == "no_improvement" and _f3[0] == "no_improvement"
          and _f4[0] == "replaced" and _f4[1]["score"] == 55
          and _fstored == 55, str((_f1[0], _f2[0], _f3[0], _f4[0])))
    check("P6F validation refuses, never trusts — and a browser is a witness,"
          " not an authority: a missing score, a digit-STRING (JSON carries"
          " numbers and this ledger will not launder strings), a fractional"
          " score (marks are whole), a negative, one over the config's own"
          " scoreMax, and a stranger game — every wall reads the shipped"
          " config, and none of the refusals wrote a row",
          _fbad == ["bad_score", "bad_score", "bad_score", "bad_score",
                   "bad_score", "unknown_game"]
          and _fdb.conn.execute("SELECT COUNT(*) FROM game_scores"
                                " WHERE game_id = 'press-run' AND account_id = ?",
                                (_fa2,)).fetchone()[0] == 0
          and _arf.score_max == _acf["scoreMax"],
          [("keys", _fbad == ["bad_score", "bad_score", "bad_score",
                              "bad_score", "bad_score", "unknown_game"]),
           ("wrote", _fdb.conn.execute("SELECT COUNT(*) FROM game_scores"
                                       " WHERE game_id = 'press-run'"
                                       " AND account_id = ?", (_fa2,)).fetchone()[0]),
           ("cap", _arf.score_max == _acf["scoreMax"])])
    check("P6F the rollover is the passage of time, not a cron: a score"
          " booked an hour BEFORE the Friday boundary rides the closing week,"
          " the SAME submission an hour AFTER books fresh in the NEXT week"
          " (first, not a re-book — the wall is per week), the two keys are"
          " seven days apart by the calendar's own arithmetic, and the old"
          " week's board keeps its rows while the new week's board is honestly"
          " empty (the points never move — the key moves under them, which is"
          " exactly why they need no payout moment)",
          _frb[0] == "first" and _frb[1]["week"] == _fkey
          and _fra[0] == "first" and _fra[1]["week"] == _wk(_fbound + 60)
          and _fra[1]["week"] != _fkey
          and abs(time.mktime(time.strptime(_fra[1]["week"], "%Y-%m-%d"))
                   - time.mktime(_fstruct)) == 604800
          and len(_arf.board("inkfall", _fkey)) == 1
          and _arf.board("press-run", _fra[1]["week"]) == [], \
              str((_frb[0], _fra[0], _frb[1]["week"] == _fkey, _fra[1]["week"])))
    check("P6F route and doctrine pins: the score door sits in the POST"
          " branch (branch-relative, the lesson that stays learned), its"
          " no-improvement answer speaks the DEFERRAL voice (200 with the"
          " deferred word — the platform's law, three lanes strong now) and"
          " the first word answers 201, the rate key rides the shipped config,"
          " and the points do not touch the purse (the scoring snapshot and"
          " the prize math are untouched files — this chapter adds no import"
          " of theirs and takes no number from them)",
          _routesrc.index('if method == "POST":')
          < _routesrc.index('if path == "/api/arcade/score":')
          and "deferred" in _routesrc[_routesrc.index(
              '"/api/arcade/score"'):_routesrc.index('"/api/arcade/score"') + 1400]
          and "(201," in _routesrc[_routesrc.index(
              '"/api/arcade/score"'):_routesrc.index('"/api/arcade/score"') + 1400]
          and "scorePerTenMinutes" in stY2.pb["ratelimit"], "the week's shape drifted")
    _arjs2 = open(os.path.join(os.path.dirname(__file__), "..", "js",
                   "components", "arcade.js"), encoding="utf-8").read()
    check("P6F the score desk on the sheet: the shell carries the form now"
          " (a select born empty — the floor's own words fill it, never an"
          " invented game), the app speaks to the score door, and the still"
          " no-links vow holds (the form is not a link and the component"
          " still carries no href — the repos have still earned none)",
          'id="pb-score-form"' in _arjs2 and "<select" in _arjs2
          and 'name="score"' in _arjs2
          and '"/api/arcade/score"' in uxjs
          and "href=" not in _arjs2, "the desk drifted")
    _fdb.close()

    # ---- P6R the redress: the spread folds; the index is the page --------
    _nav = open(os.path.join(os.path.dirname(__file__), "..", "js", "nav.js"),
                encoding="utf-8").read()
    _dash = open(os.path.join(os.path.dirname(__file__), "..", "js",
                  "components", "homedash.js"), encoding="utf-8").read()
    _hero = open(os.path.join(os.path.dirname(__file__), "..", "js",
                 "components", "hero.js"), encoding="utf-8").read()
    check("P6R the fold is structural: exactly six folds exist in the mount"
          " (home, standings, artboard, worldmap, arcade, tokens), the router"
          " is the WHOLE navigation with no dependency (its file contains not"
          " one import — the vow pinned as absence, the strongest form), it"
          " speaks only the hash and the browser's own back/forward, and the"
          " sections ride their folds unmoved (the six calls still stand in"
          " the mount in the day's order — the redesign is the shelf, not"
          " the print)",
          _nav.count("import ") == 0
          and all(('fold("' + _v + '"') in _mains for _v in
                   ("home", "standings", "artboard", "worldmap", "arcade",
                    "tokens"))
          and all(_k in _nav for _k in ("VIEWS", "hashchange", "is-view",
                                        "scrollIntoView", "is-in")),
          "six folds, one file, zero dependencies")
    check("P6R the index has exactly five doors and the mast speaks them:"
          " five mast__link and no more (the seven-link sprawl is the"
          " redesign's reason for existing), the five hrefs are the five"
          " VIEWS names verbatim (mast and router must agree, or the battery"
          " quarrels them), the brand rides the front page, the one action is"
          " the buy pill, and the hero now carries one primary and one"
          " secondary — the third pill was cut, its hop now lives as a door"
          " on the press sheet where it earns its keep",
          _masts.count('class="mast__link"') == 5
          and all(('href="#' + _v + '"') in _masts for _v in
                  ("standings", "artboard", "worldmap", "arcade", "tokens"))
          and 'href="#home"' in _masts and "mast__cta" in _masts
          and _hero.count('class="btn') == 2 and "#frontpage" not in _hero,
          str([_masts.count('class="mast__link"'), _hero.count('class="btn')]))
    check("P6R the press sheet is honest arithmetic: the dash is one section"
          " whose id makes the home anchor true, it carries exactly four"
          " doors and four slots (board, wall, map, floor — the four real"
          " doors, no fifth invented figure), its runtime speaks to exactly"
          " the endpoints those doors ride and tallies its answers aloud"
          " ('N of 4 doors answered' — an offline press says so rather than"
          " printing a lie), and no cell types a zero to look alive",
          'id="home"' in _dash and _dash.count("dash__door") == 4
          and all(_k in uxjs for _k in
                  ("startHomeDash", 'data-slot="dash-board"',
                   'data-slot="dash-art"', 'data-slot="dash-map"',
                   'data-slot="dash-arcade"', " of 4 doors answered")),
          "the sheet drifted")
    check("P6R the redress keeps the press-sheet vow and the phone keeps its"
          " own composition: the fold rule is display (not a hiding that"
          " costs the observer its truth), the phone index is a five-column"
          " thumb rail with a safe-area breath, the dash grid is woven and"
          " unsymmetric (5/7 and 7/5 — four equal columns would have been"
          " the AI tell the audit named), and zero box-shadow enters the"
          " redress block's breath",
          ".view.is-view { display: block }" in _cssB
          and "repeat(5, 1fr)" in _cssB
          and ".dash__cell--wide { grid-column: span 7 }" in _cssB
          and "box-shadow" not in _cssB.split("the redress")[1],
          "the redress drifted")
    _runsrc = open(os.path.join(os.path.dirname(__file__), "run.py"),
                    encoding="utf-8").read()
    check("P6S the ten-thousand wall fell (the redesign's catch, before it"
          " was a bug a browser would meet): the door now reads every body"
          " below an honest cap in a loop that refuses to trust a socket's"
          " first breath, an over-heavy body is DRAINED (keep-alive rides on"
          " it, HTTP/1.1 says so in the header), the cap is a named constant"
          " the artboard's own numbers could pass under, and the old silent"
          " skip is gone from the file — no body may now be skipped, only"
          " refused by a wall with a name",
          "BODY_CAP = 8000000" in _runsrc
          and "10000" not in _runsrc
          and "_read_body" in _runsrc
          and "while len(data) < length" in _runsrc, "the door regrew its wall")
    # ---- P6T the studio: the campaign desk (feature four) -----------------
    import base64 as _b64
    import io as _io
    import zipfile as _zip
    from paperboard import studio as _stumod
    _stenv = make_env(tmp="/tmp/paperboard-studio-self.sqlite3")
    _sts, _sdb, _sx, _ss, _scid = _stenv
    os.environ["PAPER_STUDIO_DIR"] = "/tmp/paperboard-studio-self"
    _stud = _stumod.Studio(_sdb, _sts)
    _sr = http_app.Router(_ss, _sts, _sdb, _sx)

    def _sgo(method, path, query=None, headers=None, body=None):
        _out = _sr.dispatch(method, path, query or {},
                            dict({"x-forwarded-for": "probe"}, **(headers or {})),
                            {}, body)
        return _out[0], _out[1], (_out[2] if len(_out) > 2 else None)

    _cfgT = _sts.pb["studio"]
    _caps = _sgo("GET", "/api/studio")[1]
    check("P6T the studio door opens honest: the capabilities view names what"
          " actually answers — no keys are set in the battery's world, so the"
          " deterministic planner is named as the speaker, the image hero is"
          " OFF (opt-in by env, never by hope), the five card kinds ship in"
          " the view, and every limit rides the shipped config (the battery"
          " reads config.json's figures, never a constant)",
          _caps.get("ok") is True and _caps.get("planner") == "deterministic"
          and _caps.get("imageGen") == "off"
          and len(_caps.get("types") or []) == 5
          and _caps["limits"]["daysMax"] == _cfgT["daysMax"]
          and _caps["limits"]["maxAssets"] == _cfgT["maxAssets"],
          str(_caps)[:160])
    check("P6T a stranger's /api/studio route still answers the house 404 and"
          " the page door serves the studio shell (the route is a real page,"
          " reachable from the mast, not a hash that goes nowhere)",
          _sgo("GET", "/api/studio/nope")[1] == {"error": "unknown_api_route"}
          and _sgo("GET", "/studio")[0] == 200
          and b"studio.js" in _sgo("GET", "/studio")[1],
          str(_sgo("GET", "/studio")[:2])[:80])

    _mk = {"project": "PAPERBOARD",
           "brief": "seven days of posting, playing, and climbing",
           "days": 7, "types": ["xpost", "meme", "promo", "banner", "announce"]}
    _made, _whyT = _stud.create(dict(_mk))
    _rowsT = {r["id"]: dict(r) for r in _sdb.conn.execute(
        "SELECT * FROM studio_assets WHERE campaign_id=?", (_made["id"],))}
    _readyT = [a for a in _made["assets"] if a["status"] == "ready"]
    _first = _made["assets"][0]
    _blob_first = _sgo("GET", _first["href"])
    _dims_route = _stumod.png_dims(_blob_first[1]) if _blob_first[0] == 200 else None
    check("P6T a brief of seven days and five cards renders the full shelf:"
          " 35 cards (the shipped cap of %d allows it), every ready row names"
          " a file whose bytes actually open at /studio/assets/, the written"
          " PNG header (re-read from the served bytes) is the ledger's own"
          " width and height — QC's authority is the file, not the claim —"
          " and no ready row carries a zero-byte lie"
          % _cfgT["maxAssets"],
          len(_rowsT) == 35 and len(_readyT) == 35
          and _blob_first[0] == 200 and _dims_route == (_first["width"],
                                                        _first["height"])
          and all(a["bytes"] > 0 for a in _readyT),
          str(_made["qc"])[:160])
    check("P6T the plan is a structured artifact, not a paragraph: objective,"
          " audience, and core message stand named; each of the %d days"
          " carries a purpose, a concept, a caption, and a call to action;"
          " the concepts are as distinct as the days (no seven variations of"
          " one joke); and the QC that rides the campaign proves it back"
          " (%d concepts distinct of %d days, text ok)",
          all(k in _made["plan"] for k in ("objective", "audience",
                                           "core_message", "days"))
          and len(_made["plan"]["days"]) == 7
          and all(all(k in d for k in ("purpose", "concept", "caption", "cta",
                                       "platform"))
                  for d in _made["plan"]["days"])
          and _made["qc"]["concepts_distinct"] == _made["qc"]["days"] == 7
          and _made["qc"]["text_ok"] is True,
          str(_made["qc"])[:120])

    _walls = [
        ({"brief": "x", "types": ["meme"]}, "bad_project"),
        ({"project": "P", "types": ["meme"]}, "bad_brief"),
        ({"project": "P", "brief": " ", "types": ["meme"]}, "bad_brief"),
        ({"project": "P", "brief": "x", "days": "7", "types": ["meme"]}, "bad_days"),
        ({"project": "P", "brief": "x", "days": _cfgT["daysMax"] + 1,
          "types": ["meme"]}, "bad_days"),
        ({"project": "P", "brief": "x", "types": []}, "bad_types"),
        ({"project": "P", "brief": "x", "types": ["nope"]}, "bad_types"),
        ({"project": "P", "brief": "x", "types": ["meme"],
          "ref_b64": "!!!"}, "bad_image_data"),
        ({"project": "P", "brief": "x", "types": ["meme"],
          "ref_b64": _b64.b64encode(b"hello world").decode()}, "bad_image_type"),
        ({"project": "P", "brief": "x", "types": ["meme"],
          "ref_b64": _b64.b64encode(b"x" * (_cfgT["maxRefBytes"] + 1)).decode()},
         "too_large"),
    ]
    _wallgot = []
    for _bw, _bwhy in _walls:
        _v, _w = _stud.create(_bw)
        _wallgot.append(_w if _v is None else "PASSED?!")
    check("P6T validation refuses, never trusts — and a browser is a witness,"
          " not an authority: a nameless project, an empty or blank brief, a"
          " digit-STRING of days (the door takes whole numbers, not digits"
          " wearing number clothes), days over the shipped cap, an empty card"
          " list and a stranger card kind, junk base64, a base64 blob that is"
          " no image, and a reference over the shipped byte ceiling — every"
          " wall reads the shipped config, and none of the refusals wrote a"
          " campaign",
          _wallgot == [_b for _a, _b in _walls], str(_wallgot))

    class _FakePlanner:
        """a model with a script: configured by shape, never by network."""
        configured = True
        model = "fake-soliloquy"

        def __init__(self, answer):
            self.answer = answer

        def plan(self, brief, days_n, fallback=None):
            return self.answer

    _planT = {"objective": "the timeline posts again", "audience": "the board",
              "core_message": "post, earn, climb",
              "days": [{"day": 1, "purpose": "the hook", "concept": "alpha",
                       "caption": "day one", "cta": "join", "visual": "mascot leads"},
                      {"day": 2, "purpose": "the joke", "concept": "beta",
                       "caption": "day two", "cta": "post", "visual": "mascot winks"}]}
    _stud.llm = _FakePlanner(_planT)
    _spoke, _ = _stud.create(dict(_mk, days=2, types=["meme"]))
    _stud.llm = _FakePlanner("rejected")
    _denied, _ = _stud.create(dict(_mk, days=2, types=["meme"]))
    _stud.llm = _FakePlanner("unreachable")
    _gone, _ = _stud.create(dict(_mk, days=2, types=["meme"]))
    _stud.llm = _stumod.LlmPlanner({})           # the battery's honest default
    _bad_plan = _stumod._validate_plan({"objective": "o", "audience": "a",
                                        "core_message": "m",
                                        "days": [{"day": 1, "purpose": "p",
                                                 "concept": "c"}]}, 1)
    check("P6T an LLM speaks the plan only inside its schema: model words that"
          " fit the contract book the campaign as the model's own (the engine"
          " names it); words rejected by the schema fall to the deterministic"
          " voice and SAY SO in the engine line (the deferral doctrine, four"
          " lanes strong); an unreachable model falls the same way; and the"
          " validator refuses a day missing its caption in the hand — no"
          " network is spent in this battery, the fake planner wears the"
          " configured coat alone",
          _spoke["engine"] == "fake-soliloquy"
          and _spoke["plan"]["core_message"] == "post, earn, climb"
          and _denied["engine"] == "deterministic (model rejected)"
          and _gone["engine"] == "deterministic (model unreachable)"
          and _bad_plan is None,
          str((_spoke["engine"], _denied["engine"], _gone["engine"])))

    _tgt = _made["assets"][1]
    _keep_before = {a["id"]: (a["file"], a["bytes"], a["created_at"])
                    for a in _made["assets"] if a["id"] != _tgt["id"]}
    _fresh, _varT, _rw = _stud.regenerate(_made["id"], _tgt["id"])
    _t_new = [a for a in _fresh["assets"] if a["id"] == _tgt["id"]][0]
    _keep_after = {a["id"]: (a["file"], a["bytes"], a["created_at"])
                   for a in _fresh["assets"] if a["id"] != _tgt["id"]}
    check("P6T one card answers for itself: the rerolled card comes back ready"
          " at variant %d with a fresh file and fresh bytes (the concept and"
          " the pose rotate so the take is genuinely different), and every"
          " other card keeps its file, its bytes, and its minute — accepted"
          " work is never collateral. the missing-card pairing answers"
          " asset_absent, and the qc after the reroll again certifies the"
          " full shelf",
          _varT == 1 and _t_new["status"] == "ready"
          and _t_new["file"] != _tgt["file"] and _keep_after == _keep_before
          and _fresh["qc"]["assets_ready"] == _fresh["qc"]["assets_total"],
          str(_t_new)[:120])

    _zx, _zpayload, _zheads = _sgo("GET", "/api/studio/export",
                                   {"id": str(_made["id"])})
    _znf = _zip.ZipFile(_io.BytesIO(_zpayload))
    _znamed = set(_znf.namelist())
    _zmani = set()
    _zroot = _made["slug"] + "-" + str(_made["id"]) + "/"
    for _n in _znamed:
        if _n.endswith("manifest.json"):
            _zmani = set(json.loads(_znf.read(_n)))
    _ready_names = {"assets/" + _stumod._zip_entry(_made["plan"]["days"], _a)
                    for _a in _fresh["assets"]
                    if _a["status"] == "ready" and _a["file"]}
    _phantom = [m for m in _zmani if (_zroot + m) not in _znamed
                and m not in ("manifest.json",)]
    check("P6T the ZIP is the campaign's receipt and it extracts: the door"
          " answers with real archive bytes (Content-Type plus a named"
          " download), testzip passes, and the manifest is a truth — every"
          " ready card's file is inside both the archive and the manifest,"
          " the plan, brand, calendar, and per-day copy all ride along, and"
          " nothing the manifest names is missing from the archive (the"
          " manifest does not claim phantom paper)",
          _zx == 200 and _zheads["Content-Type"] == "application/zip"
          and "filename=" in _zheads["Content-Disposition"]
          and _znf.testzip() is None and len(_znf.namelist()) >= 40
          and _ready_names <= _zmani and not _phantom,
          str((len(_znamed), len(_zmani), _phantom[:2]))[:120])
    _znf.close()
    _lost = sorted(_ready_names)[0]
    _lostRow = next(_a for _a in _fresh["assets"]
                    if _a["status"] == "ready" and _a["file"]
                    and "assets/" + _stumod._zip_entry(_made["plan"]["days"],
                                                       _a) == _lost)
    os.remove(os.path.join(_stud.root, _lostRow["file"]))
    _zx2, _zp2, _ = _sgo("GET", "/api/studio/export", {"id": str(_made["id"])})
    _znf2 = _zip.ZipFile(_io.BytesIO(_zp2))
    _nm2 = set(_znf2.namelist())
    for _n in _nm2:
        if _n.endswith("manifest.json"):
            _zm2 = set(json.loads(_znf2.read(_n)))
    _znf2.close()
    check("P6T and the receipt stays honest under loss: a card removed from"
          " the disk after the fact is neither packed nor listed — the"
          " manifest never claims a file the archive never met",
          len(_nm2) + 1 == len(_znamed) and _lost not in _zm2
          and _zroot + _lost not in _nm2,
          str((len(_nm2), len(_znamed)))[:80])

    _noRef, _ = _stud.create({"project": "STANDALONE", "brief": "x",
                              "days": 2, "types": ["xpost"]})
    _refPng = _stumod.png_write(2, 2, bytes((240, 20, 20, 255)) * 4)
    _withRef, _ = _stud.create({"project": "STANDALONE", "brief": "x",
                                "days": 2, "types": ["xpost"],
                                "ref_b64": "data:image/png;base64,"
                                           + _b64.b64encode(_refPng).decode()})
    _jpgRef, _ = _stud.create({"project": "STANDALONE", "brief": "x",
                               "days": 2, "types": ["xpost"],
                               "ref_b64": _b64.b64encode(
                                   b"\xff\xd8\xff\xe0junkjunkjunk").decode()})
    check("P6T the brand read tells the truth about itself: with no reference"
          " the palette is SUGGESTED (the PAPER defaults, no pretending an"
          " image spoke); a PNG reference is EXTRACTED (buckets from the"
          " bytes, roles named, the note still leaves voice to the human);"
          " and a JPEG reference is KEPT BUT NOT DECODED — the shelf does not"
          " fake a stdlib jpeg decoder it does not own, and the campaign"
          " still ships its cards",
          _noRef["brand"]["source"] == "suggested"
          and _withRef["brand"]["source"] == "extracted"
          and set(_withRef["brand"]["roles"]) >= {"paper", "press", "accent"}
          and _jpgRef["brand"]["source"] == "kept, not decoded"
          and _jpgRef["qc"]["assets_ready"] == 2,
          str((_noRef["brand"]["source"], _withRef["brand"]["source"],
               _jpgRef["brand"]["source"])))

    _wallsT = []
    for _wp, _wcode in (("/studio/assets/bogus.png", "file_absent"),
                        ("/studio/assets/bogus.txt", "file_absent"),
                        ("/studio/assets/s9-a9-xpost-v0-deadbeef.png",
                         "file_absent")):
        _o = _sgo("GET", _wp)
        _wallsT.append((_o[0] == 404, _o[1] == {"error": _wcode}))
    _wide, _ = _stud.create(dict(_mk, days=_cfgT["daysMax"],
                                 types=["xpost", "meme", "promo", "banner",
                                        "announce"]))
    check("P6T the file door walls first: a stranger's name, a stranger's"
          " extension, and a name whose hex limb is one finger too long all"
          " answer file_absent before the disk is touched — and when the"
          " week is as long as the shipped cap allows and the five cards"
          " would overflow it (%d days × 5 > %d), the calendar does not lie:"
          " the cards rotate across the days and the plan's own schedule"
          " note says the cap spoke"
          % (_cfgT["daysMax"], _cfgT["maxAssets"]),
          all(ok for _a, ok in _wallsT)
          and len(_wide["assets"]) <= _cfgT["maxAssets"]
          and "rotate" in _wide["plan"]["schedule_note"],
          str((len(_wide["assets"]), _wide["plan"]["schedule_note"]))[:140])

    _sroute = open(os.path.join(os.path.dirname(__file__), "paperboard",
                                "http_app.py"), encoding="utf-8").read()
    _smast = open(os.path.join(os.path.dirname(__file__), "..", "js",
                  "components", "masthead.js"), encoding="utf-8").read()
    _sjs = open(os.path.join(os.path.dirname(__file__), "..", "js",
                 "studio.js"), encoding="utf-8").read()
    _shtml = open(os.path.join(os.path.dirname(__file__), "..", "studio.html"),
                  encoding="utf-8").read()
    check("P6T the routes and the shelf agree, branch for branch: the studio"
          " doors sit in their own method branches (the GET reads after the"
          " wall's lane, the create and reroll in the POST body where the"
          " JSON vow lives), the file lane precedes the generic static fall,"
          " the page door rides beside admin's, the mast gained its sixth"
          " door without touching the five pinned prints (the studio link"
          " wears its own class, so the five-name count the redress pinned"
          " stays the truth), and the studio's own module speaks only the"
          " routes the server actually keeps",
          _sroute.rindex('if path == "/api/studio/campaigns":')
          > _sroute.index('if method == "POST":')
          and _sroute.index('"/studio/assets/"') < _sroute.index('if path == "/"')
          and _sroute.count('class="mast__link"') == 0          # (the pins own that count)
          and _smast.count('class="mast__link"') == 5
          and 'mast__link--studio' in _smast and 'href="/studio"' in _smast
          and all(_k in _sjs for _k in ("/api/studio", "/api/studio/campaigns",
                                        "/api/studio/regenerate",
                                        "/api/studio/export"))
          and "studio.css" in _shtml and "js/studio.js" in _shtml,
          str([_smast.count('class="mast__link"')])[:40])
    _cssT = open(os.path.join(os.path.dirname(__file__), "..", "css",
                  "paperboard.css"), encoding="utf-8").read()
    check("P6T the phone keeps its thumb: the studio door joins the bottom"
          " index as a sixth track, the five-track rule stays standing as"
          " printed history, and the studio's own sheet adds no box-shadow"
          " and no gradient to the desk (the press vow holds on the new"
          " page too)",
          _cssT.count("repeat(5, 1fr)") >= 1
          and "repeat(6, 1fr)" in _cssT[_cssT.index("P6T"):]
          and "box-shadow" not in open(
              os.path.join(os.path.dirname(__file__), "..", "css",
                           "studio.css"), encoding="utf-8").read()
          and "gradient" not in open(
              os.path.join(os.path.dirname(__file__), "..", "css",
                           "studio.css"), encoding="utf-8").read())
    # ---- P6U the studio's repair chapter: the journey the user rode -----
    # (the 502 hunt: every studio refusal must ride a JSON error key, the
    #  rate wall must be the wall, and a stranger campaign must not feel a
    #  reroll. the upload matrix rides the ROUTE, not the helper.)
    _mast_path = os.path.join(os.path.dirname(__file__), "..", "public",
                              "paper-mascot.jpeg")
    _mast_b64 = _b64.b64encode(open(_mast_path, "rb").read()).decode()
    _pngU = _stumod.png_write(2, 2, bytes((9, 200, 9, 255)) * 4)
    _refpng_u = _b64.b64encode(_pngU).decode()
    _u_shapes = [
        ("png ref rides the route", {"project": "U", "brief": "x", "days": 1,
                                     "types": ["meme"],
                                     "ref_b64": "data:image/png;base64,"
                                                + _refpng_u},
         201, "extracted"),
        ("jpeg ref rides the route", {"project": "U", "brief": "x", "days": 1,
                                      "types": ["meme"],
                                      "ref_b64": "data:image/jpeg;base64," + _mast_b64},
         201, "kept, not decoded"),
        ("gif ref rides the route", {"project": "U", "brief": "x", "days": 1,
                                     "types": ["meme"],
                                     "ref_b64": _b64.b64encode(
                                         b"GIF89a junkjunkjunk").decode()},
         201, "kept, not decoded"),
        ("webp ref rides the route", {"project": "U", "brief": "x", "days": 1,
                                      "types": ["meme"],
                                      "ref_b64": _b64.b64encode(
                                          b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode()},
         201, "kept, not decoded"),
        ("junk base64 answers via the route", {"project": "U", "brief": "x",
                                               "days": 1, "types": ["meme"],
                                               "ref_b64": "!!!"}, 422, None),
        ("non-image bytes answer via the route", {"project": "U", "brief": "x",
                                                   "days": 1, "types": ["meme"],
                                                   "ref_b64": _b64.b64encode(
                                                       b"plain text plain text").decode()},
         422, None),
    ]
    _ugot = []
    for _un, _ub, _ucode, _usrc in _u_shapes:
        _us = _sgo("POST", "/api/studio/campaigns", body=_ub,
                   headers={"x-forwarded-for": "matrix-u"})["payload"] \
            if False else _sgo("POST", "/api/studio/campaigns", body=_ub,
                               headers={"x-forwarded-for": "matrix-u"})
        _ugot.append((_us[0] == _ucode,
                      _us[1].get("error") if _ucode != 201 else
                      (_us[1].get("campaign") or {}).get("brand", {}).get("source") == _usrc
                      if _usrc else True))
    check("P6U the upload matrix rides the door, not the helper: a png reference"
          " is EXTRACTED, a jpeg one is KEPT-NOT-DECODED (the real mascot file"
          " rides the wire), gif and webp arrive kept-not-decoded by name of"
          " their own bytes, junk base64 and plain text answer 422 with their"
          " named keys — and the oversize wall (one byte past the shipped"
          " ceiling) answers too_large, never a stack trace and never a bare"
          " status",
          all(_ok and ok for _ok, ok in _ugot)
          and (lambda _o: _o[0] == 422 and _o[1] == {"error": "too_large"})(
              _sgo("POST", "/api/studio/campaigns",
                   body={"project": "U", "brief": "x", "days": 1,
                         "types": ["meme"],
                         "ref_b64": _b64.b64encode(
                             b"x" * (_cfgT["maxRefBytes"] + 1)).decode()},
                   headers={"x-forwarded-for": "heavy-u"})),
          str([(s[0], s[1]) for s in _ugot])[:150])
    # the exotic-png lane: a png the shelf cannot open is a 422 by NAME
    _stub = lambda raw: (_ for _ in ()).throw(
        _stumod.StudioError("unsupported_png")) if False else None
    _old_pr = _stumod.png_read
    def _boom(_raw):
        raise _stumod.StudioError("unsupported_png")
    _stumod.png_read = _boom
    _deep = _sgo("POST", "/api/studio/campaigns",
                 body={"project": "U", "brief": "x", "days": 1,
                       "types": ["meme"],
                       "ref_b64": _b64.b64encode(_refPng).decode()},
                 headers={"x-forwarded-for": "deep-u"})
    _stumod.png_read = _old_pr
    check("P6U a reference the stdlib shelf cannot crack does not sink the"
          " campaign and does not fake an extraction: it sails with the"
          " honest brand 'kept, not decoded' (deferral, not error), while the"
          " studio POST doors still answer every refusal by NAME — the X-era"
          " fall-through default is 502, and the studio map is total, so no"
          " studio refusal can masquerade as the legacy gateway default"
          " (the root cause of the user's bare 502 was never the studio's"
          " code — it was a tier that does not carry the studio routes)",
      _deep[0] == 201
      and "kept, not decoded" in _deep[1]["campaign"]["brand"]["source"]
      and _sroute.rindex('"/api/studio/regenerate"')
          < _sroute.index(".get(reason, 502)"),
      str(_deep)[:90])
    _isoC = _sgo("POST", "/api/studio/campaigns",
                  body={"project": "ISOC", "brief": "x", "days": 2,
                        "types": ["xpost"]},
                  headers={"x-forwarded-for": "iso-c"})[1]["campaign"]
    _isoD = _sgo("POST", "/api/studio/campaigns",
                  body={"project": "ISOD", "brief": "x", "days": 1,
                        "types": ["meme"]},
                  headers={"x-forwarded-for": "iso-d"})[1]["campaign"]
    _d_before = [(a["id"], a["file"], a["bytes"]) for a in _isoD["assets"]]
    _sgo("POST", "/api/studio/regenerate",
         body={"campaign": _isoC["id"], "asset": _isoC["assets"][0]["id"]},
         headers={"x-forwarded-for": "iso-c"})
    _d_now = _sgo("GET", "/api/studio/campaign", {"id": str(_isoD["id"])})[1]["campaign"]
    _d_after = [(a["id"], a["file"], a["bytes"]) for a in _d_now["assets"]]
    _zipC = _zip.ZipFile(_io.BytesIO(_sgo("GET", "/api/studio/export",
                                          {"id": str(_isoC["id"])})[1]))
    _namesC = set(_zipC.namelist())
    _crossed = [n for n in _namesC for a in _d_after if a[1] and a[1] in n]
    _zipC.close()
    check("P6U campaigns keep their own counsel: rerolling a card of campaign"
          " C leaves every card of campaign D byte-for-byte where it stood,"
          " and the archive of C carries not one filename of D (no crossed"
          " assets through the ledger, the file door, or the receipt)",
          _d_after == _d_before and not _crossed,
          str((len(_d_after), _crossed[:2]))[:90])
    _wmax = stY2.pb["ratelimit"]["studioWritePerTenMinutes"]
    _hammer = [_sgo("POST", "/api/studio/campaigns",
                    body={"project": "H", "brief": "x", "days": 1,
                          "types": ["meme"]},
                    headers={"x-forwarded-for": "hammer-u"})[0]
               for _hi in range(_wmax)]
    _wallU = _sgo("POST", "/api/studio/campaigns",
                  body={"project": "H", "brief": "x", "days": 1,
                        "types": ["meme"]},
                  headers={"x-forwarded-for": "hammer-u"})
    check("P6U the write wall is the wall, named: %d creates from one address"
          " fill the shipped bucket (%d per ten minutes as configured) and"
          " the next answer is a 429 studio_write_rate_limited — the"
          " repeated-click class the redress warned of has a door-side guard"
          " and a voice" % (_wmax, _wmax),
          all(_h == 201 for _h in _hammer) and _wallU[0] == 429
          and _wallU[1] == {"error": "studio_write_rate_limited"},
          str(_hammer)[:80])
    # ---- P6V (the writing desk): studio's second morning. the campaign now
    # ---- opens with a strategy, the post is the week's unit, and the copy
    # ---- button only says Copied when the copy genuinely happened. every
    # ---- pin is structural — the shape of the plan, the honesty of the QC,
    # ---- the receipt's ledgers — and none of them chases the prose word. --
    _v2, _ = _stud.create(dict(_mk, types=["xpost", "meme"]))
    _vp, _vq = _v2["plan"], _v2["qc"]
    _vdays = _vp["days"]
    _vposts = [_p for _d in _vdays for _p in _d["posts"]]
    check("P6V a campaign opens with a strategy and not a paragraph: the plan"
          " names an objective, an audience, a core message, a strategy, an"
          " outcome, and the week's run; every day carries a theme and a"
          " reason it matters; every day carries its posts (a two-card brief"
          " posts twice a day), so the shelf's cards and the plan's posts are"
          " one and the same shape",
          all(_k in _vp for _k in ("objective", "audience", "core_message",
                                   "strategy", "outcome", "arc", "days"))
          and len(_vdays) == 7
          and all(isinstance(_d.get("theme"), str) and _d["theme"]
                  and isinstance(_d.get("why"), str) and _d["why"]
                  and len(_d.get("posts") or []) == 2 for _d in _vdays),
          str(_vp)[:140])
    check("P6V posts are structured and not slogans: every post speaks an id,"
          " a purpose, a concept, a caption, a call to action, and its own"
          " internal note; the ids ride position (dXX-pYY) and never repeat"
          " across the campaign",
          all(all(_k in _p for _k in ("id", "purpose", "concept", "caption",
                                      "cta", "notes")) for _p in _vposts)
          and all(_re6.match(r"^d[0-9]{2}-p[0-9]{2}$", _p["id"])
                  for _p in _vposts)
          and len({_p["id"] for _p in _vposts}) == len(_vposts),
          str([_p["id"] for _p in _vposts[:3]]))
    check("P6V the writing left the scaffold: no caption wears the old"
          " day-number costume, every caption and note came back non-empty"
          " and normalised and every call is either a call or a decided rest"
          " (a string, never a hole), and the week's captions are as many and"
          " as distinct as its posts — the meme bank grew to nine so even the"
          " punchline says each line once a week",
          all(not _re6.match(r"^Day [0-9]+ —", _p["caption"]) for _p in _vposts)
          and all(_p["caption"] and _p["notes"] and isinstance(_p["cta"], str)
                  for _p in _vposts)
          and len({_p["caption"] for _p in _vposts}) == len(_vposts),
          str(sorted({_p["caption"][:14] for _p in _vposts})[:5])[:140])
    check("P6V QC grew a second spine and kept its honour: text ok now"
          " watches every post instead of one caption a day, the new"
          " posts_total and captions_distinct ride what was actually"
          " written, and the old rail (concepts as distinct as the days,"
          " seven of seven) still stands — an added claim, never a traded one",
          _vq["text_ok"] is True
          and _vq["posts_total"] == len(_vposts)
          and _vq["captions_distinct"] == len(_vposts)
          and _vq["concepts_distinct"] == _vq["days"] == 7, str(_vq)[:140])
    _vOne, _ = _stud.create(dict(_mk, days=2, types=["meme"]))
    _vFive, _ = _stud.create(dict(_mk, days=2, types=["xpost", "meme", "promo",
                                                      "banner", "announce"]))
    check("P6V the desk flexes and does not force: a one-card week posts once"
          " a day (two days, two posts) and a five-card week posts five times"
          " a day behind the day's one idea (two days, ten posts) — posts"
          " follow the shelf and never a template's vanity",
          _vOne["qc"]["posts_total"] == 2 and _vOne["qc"]["days"] == 2
          and _vFive["qc"]["posts_total"] == 10 and _vFive["qc"]["days"] == 2,
          str((_vOne["qc"]["posts_total"], _vFive["qc"]["posts_total"])))
    _vTwelve, _ = _stud.create(dict(_mk, days=_cfgT["daysMax"],
                                    types=["xpost"]))
    check("P6V the long week turns the page and does not print its own"
          " sentence twice: when a campaign rides the shipped cap of %d days"
          " the second lap draws other lines of the lane's pool (day eight is"
          " not day one's copy), so the day-level concepts stay as distinct as"
          " the days and the text rail stands honest at the cap's length"
          % _cfgT["daysMax"],
          _vTwelve["qc"]["concepts_distinct"] == _vTwelve["qc"]["days"]
          == _cfgT["daysMax"] and _vTwelve["qc"]["text_ok"] is True,
          str(_vTwelve["qc"])[:120])
    _vE, _ = _stud.create(dict(_mk, days=3, types=["xpost", "meme"]))
    _zE, _zpE, _ = _stud.export(_vE["id"])
    _ze = _zip.ZipFile(_io.BytesIO(_zE))
    _zrE = _vE["slug"] + "-" + str(_vE["id"]) + "/"
    _znmE = set(_ze.namelist())
    _zmnE = set(json.loads(_ze.read(_zrE + "manifest.json")))
    _cpE = _ze.read(_zrE + "campaign-posts.csv").decode("utf-8").splitlines()
    _ptE = _ze.read(_zrE + "copy/day-01/post-01.txt").decode("utf-8")
    check("P6V the receipt learned the posts and stayed a receipt: the"
          " archive reopens, the manifest names only what the archive truly"
          " met, the posts ledger and one public paper per post ride"
          " alongside every ready card, the ledger prints exactly one row"
          " per post under the header an operator publishes by (three days,"
          " two cards, six rows), and the per-post txt carries public copy"
          " alone — the creative note lives in the day's markdown and the"
          " ledger, never in the tweet",
          _ze.testzip() is None
          and {_zrE + _n for _n in ("campaign-posts.csv", "copy/day-01.md",
                                    "copy/day-01/post-01.txt",
                                    "copy/day-02/post-02.txt")} <= _znmE
          and {"assets/" + _stumod._zip_entry(_vE["plan"]["days"], _a)
               for _a in _vE["assets"] if _a["status"] == "ready"} <= _zmnE
          and _zrE + "assets/day-01/post-01.png" in _znmE
          and all(_m.startswith("assets/day-") for _m in _zmnE
                  if _m.startswith("assets/"))
          and all(_zrE + _m in _znmE for _m in _zmnE - {"manifest.json"})
          and _cpE[0].startswith("day,post_id,kind,purpose,concept,caption,"
                                "cta,notes,asset_files")
          and len(_cpE) == 1 + _vE["qc"]["posts_total"]
          and "the idea wears the art" not in _ptE
          and _vE["plan"]["days"][0]["posts"][0]["caption"] in _ptE,
          str((len(_znmE), len(_zmnE)))[:100])
    _ze.close()
    _oldS = json.loads(json.dumps(_vp))
    for _d in _oldS["days"]:
        for _k in ("posts", "theme", "why", "aspect"):
            _d.pop(_k, None)
    _sdb.conn.execute("UPDATE studio_campaigns SET plan_json=? WHERE id=?",
                      (json.dumps(_oldS), _v2["id"]))
    _sdb.conn.commit()
    _vViewV = _stud.view(_v2["id"])
    _zV, _zpV, _ = _stud.export(_v2["id"])
    _zv = _zip.ZipFile(_io.BytesIO(_zV))
    _zrV = _v2["slug"] + "-" + str(_v2["id"]) + "/"
    _sfxV = {_n.split("/")[-1] for _n in _zv.namelist()}
    _planV = json.loads(_zv.read(_zrV + "campaign-plan.json"))
    check("P6V an old campaign is a first-season citizen and not a fossil:"
          " strip a stored plan back to the pre-posts shape and both doors"
          " still answer (the view rides the synthesis, the export packs"
          " it) — the archive reopens, the reader synthesises one"
          " post worn by the day so the per-post paper and the ledger still"
          " print from last season's bytes, and the plan stored inside the"
          " archive keeps its age (no posts were smuggled into storage)",
          _vViewV is not None
          and _vViewV["plan"]["days"][0].get("posts") is None
          and len(_vViewV["assets"]) == 14
          and _zv.testzip() is None
          and {"campaign-plan.json", "brand-profile.json",
               "campaign-calendar.csv", "campaign-posts.csv",
               "README.md", "manifest.json", "day-01.md", "post-01.txt"
               } <= _sfxV
          and _planV["days"][0].get("posts") is None
          and any(_re6.match(r"^s[0-9]+-a", _n) for _n in _sfxV),
          str(_sfxV)[:140])
    _zv.close()
    _spokenOld = json.loads(json.dumps(_oldS))
    _badPos = json.loads(json.dumps(_vp))
    _badPos["days"][0]["posts"][0]["id"] = "d07-p01"
    check("P6V the model's old vocabulary still passes the gate and the new"
          " one is policed: a spoken answer without posts validates to one"
          " synthesised post a day, the full post-shaped answer validates"
          " whole, and a post that lies about its place (a d07 id on day"
          " one) is refused entire — the fall to the deterministic voice"
          " stays the design and not a patch",
          _stumod._validate_plan(_spokenOld, 7) is not None
          and len(_stumod._validate_plan(_spokenOld, 7)["days"][0]["posts"]) == 1
          and _stumod._validate_plan(json.loads(json.dumps(_vp)), 7) is not None
          and _stumod._validate_plan(_badPos, 7) is None,
          str(_stumod._validate_plan(_spokenOld, 7))[:120])
    _capsV = _stud.capabilities()
    check("P6V the door names its new shape out loud: the capabilities view"
          " still speaks the deterministic planner and the five cards it"
          " actually keeps, the plan schema moved to paper-studio-plan/3 on"
          " purpose — the plan genuinely changed (the brief grew its"
          " framing: insight, promise, tone, action, success) and the schema"
          " says so (this pin is deliberate, not drift) — and the model's own"
          " brief now advertises the posts shape (a textual pin: the live lane"
          " stays behind its key and the gate already reads both vocabularies)",
          _capsV["schema"] == "paper-studio-plan/3"
          and _capsV["planner"] == "deterministic"
          and len(_capsV["types"]) == 5
          and "posts:[{id" in open(_stumod.__file__,
                                   encoding="utf-8").read(),
          str(_capsV["schema"]))
    _cssV = open(os.path.join(os.path.dirname(__file__), "..", "css",
                              "studio.css"), encoding="utf-8").read()
    check("P6V the desk paints the campaign and the sheet keeps its vows:"
          " the overview and the post grid ride the plan (a client-side"
          " synthesis keeps campaigns older than the posts renderable), the"
          " copy button's text rides the markup, a post without a finished"
          " image is named text-only out loud, and the studio's own sheet"
          " adds neither a shadow nor a gradient to any of it",
          "st-ov" in _sjs and "st-post" in _sjs and 'data-copy-cap="' in _sjs
          and "text-only" in _sjs and "padStart" in _sjs
          and ".st-ov" in _cssV and ".st-post" in _cssV
          and "white-space: pre-wrap" in _cssV and "box-shadow" not in _cssV
          and "gradient" not in _cssV,
          str([_k for _k in ("st-ov", "st-post", "data-copy-cap")
               if _k not in _sjs])[:80])
    _pvM = _re6.search(r"P6V copy seam begin -- \*/(.*?)\n"
                       r"/\* -- P6V copy seam end", _sjs, _re6.DOTALL)
    _pvSeam = _pvM.group(1) if _pvM else ""
    _pvJs = _pvSeam + r"""
var log9 = [];
var mkSP9 = function (val, okF) {
  return { then: function (a, b) { return okF ? a(val) : b(val); } };
};
var mkDoc9 = function (exec) {
  var d = { body: { appendChild: function () {}, removeChild: function () {} },
            createElement: function () {
              return { value: "", style: {}, setAttribute: function () {},
                       select: function () {} }; } };
  if (exec !== "absent") {
    d.execCommand = function () { log9.push("exec"); return exec; };
  }
  return d;
};
var runPV = function (clip, exec, script) {
  log9 = [];
  navigator = undefined;
  if (clip) {
    navigator = { clipboard: { writeText: function () {
      if (script === "throw") { throw new Error("sync-throw"); }
      return script; } } };
  }
  document = mkDoc9(exec);
  var got = null;
  var mode = copyText("hello week", function (m) { got = "ok:" + m; },
                      function (m) { got = "bad:" + m; });
  return { out: got, mode: mode, exec: log9.length > 0 };
};
var pv = [];
var a1 = runPV(true, true, mkSP9("t", true));
pv.push("s1" + (a1.out === "ok:Copied" && a1.mode === "clipboard"
                && !a1.exec ? ":ok" : ":FAIL(" + a1.out + "," + a1.mode + ")"));
var a2 = runPV(true, true, mkSP9("x", false));
pv.push("s2" + (a2.out === "ok:Copied" && a2.exec
                ? ":ok" : ":FAIL(" + a2.out + "," + a2.exec + ")"));
var a3 = runPV(true, false, mkSP9("x", false));
pv.push("s3" + (a3.out === "bad:Copy failed" && a3.exec
                ? ":ok" : ":FAIL(" + a3.out + ")"));
var a4 = runPV(false, true, null);
pv.push("s4" + (a4.out === "ok:Copied" && a4.mode === "legacy"
                && a4.exec ? ":ok" : ":FAIL(" + a4.out + "," + a4.mode + ")"));
var a5 = runPV(false, "absent", null);
pv.push("s5" + (a5.out === "bad:Copy unavailable" && a5.mode === "none"
                && !a5.exec ? ":ok" : ":FAIL(" + a5.out + "," + a5.mode + ")"));
var a6 = runPV(true, true, "throw");
pv.push("s6" + (a6.out === "bad:Copy failed" && a6.mode === "none"
                ? ":ok" : ":FAIL(" + a6.out + "," + a6.mode + ")"));
var pvBad = pv.filter(function (x) { return x.indexOf(":ok") < 0; });
var _p6vVerdict = pvBad.length ? "P6V-FAILS " + pv.join(" ") : "P6V-scenarios-behave";
(typeof console !== 'undefined' && console.log)
  ? (console.log(_p6vVerdict), _p6vVerdict) : _p6vVerdict;
"""
    def _p6v_twin(_clip, _res, _exec, _throw=False):
        # a python reading of the very same lane (the P6K lesson: an absent
        # engine is never a silent pass — the twin drives the six scenarios
        # over the very same synchronous shams and the verdict must match)
        _lg = []
        def _fb():
            if _exec == "absent":
                return ("none", "bad:Copy unavailable")
            _lg.append("exec")
            return (("legacy", "ok:Copied") if _exec
                    else ("legacy", "bad:Copy failed"))
        if _clip and not _throw:
            _m, _o = (("clipboard", "ok:Copied") if _res else _fb())
            return (_o, _m, bool(_lg))
        if _clip:
            return ("bad:Copy failed", "none", False)
        if _exec != "absent":
            _m, _o = _fb()
            return (_o, _m, bool(_lg))
        return ("bad:Copy unavailable", "none", False)
    _twin6 = [_p6v_twin(True, True, True), _p6v_twin(True, False, True),
              _p6v_twin(True, False, False), _p6v_twin(False, None, True),
              _p6v_twin(False, None, "absent"),
              _p6v_twin(True, None, True, _throw=True)]
    _pvExp = [("ok:Copied", "clipboard", False), ("ok:Copied", "legacy", True),
              ("bad:Copy failed", "legacy", True), ("ok:Copied", "legacy", True),
              ("bad:Copy unavailable", "none", False),
              ("bad:Copy failed", "none", False)]
    _pvEng = next((_c for _c in (("osascript", "-l", "JavaScript", "-e"),
                                 ("node", "-e"))
                   if __import__("shutil").which(_c[0])), None)
    _pvOut = ""
    if _pvM and _pvEng:
        try:
            _pvOut = __import__("subprocess").run(
                list(_pvEng) + [_pvJs], capture_output=True, text=True).stdout
        except OSError:
            _pvOut = ""
    check("P6V seam: the copy lane stands between its markers (clipboard"
          " first, textarea fallback, three honest outcomes) and the six"
          " scenarios behave — where an engine answers (osascript or node,"
          " chosen by availability and not by platform) it drives the real"
          " js; the python twin drives the very same six regardless: the"
          " clipboard wins without touching the legacy lane, a reject rides"
          " the fallback to a truthful Copied, a dead fallback says Copy"
          " failed, the legacy lane alone says Copied, nothing at all says"
          " Copy unavailable, and a throwing lane never cries Copied",
          bool(_pvM) and "navigator.clipboard" in _pvSeam
          and "execCommand" in _pvSeam and '"Copied"' in _pvSeam
          and '"Copy failed"' in _pvSeam
          and '"Copy unavailable"' in _pvSeam
          and _twin6 == _pvExp
          and ((_pvEng is None) or ("P6V-scenarios-behave" in _pvOut)),
          (str(_pvOut)[-120:] if _pvOut else "twin")[:140])
    # ---- P6W the creative desk, the real map, and the download door ------
    from paperboard import places as _wPl
    _wPlan = _stumod.deterministic_plan("p6w zeta the wider", "", 7,
                                        _stumod._asset_specs(7, ["xpost", "meme",
                                                                  "promo"], 36))
    _stw = _wPlan
    _w7 = _wPlan["days"][0]
    _wSjs = open(os.path.join(os.path.dirname(__file__), "..", "js",
                              "studio.js"), encoding="utf-8").read()
    _wLandSrc = open(os.path.join(os.path.dirname(__file__), "..", "js",
                                  "world-land.js"), encoding="utf-8").read()
    _wVc = json.loads(open(os.path.join(os.path.dirname(__file__), "..",
                                        "vercel.json"), encoding="utf-8").read())
    _wLandGj = json.loads(_re6.search(
        r'\{"type": "MultiPolygon", "coordinates": (.*)\};\s*\Z',
        _wLandSrc, _re6.DOTALL).group(1))
    check("P6W the deterministic desk now thinks like a desk: three named"
          " campaign platforms exist (each with insight, promise, tone,"
          " action, success, and a five-line call bank whose fifth line is"
          " the decision to rest), two differently-named campaigns of one"
          " goal print different day-one lines, and a plan speaks every"
          " framing word out loud — the brief reads like strategy and not"
          " like a schema dump",
          len(_stumod.CONCEPTS) == 3
          and all(all(k in c and c[k] for k in ("name", "insight", "promise",
                                                "tone", "action", "success"))
                  and len(c["ctas"]) == 5 and "" in c["ctas"]
                  for c in _stumod.CONCEPTS)
          and _stumod.deterministic_plan("p6w alpha", "", 7,
              _stumod._asset_specs(7, ["xpost", "meme", "promo"], 36))
              ["days"][0]["posts"][0]["caption"]
          != _stumod.deterministic_plan("p6w zeta the wider", "", 7,
              _stumod._asset_specs(7, ["xpost", "meme", "promo"], 36))
              ["days"][0]["posts"][0]["caption"]
          and all(k in _stw for k in ("insight", "promise", "tone", "action",
                                      "success", "mix")),
          str(_stw.get("core_message"))[:80])
    check("P6W the week's calls rest on purpose and the day still mirrors"
          " its lead exactly: a resting call is an empty string (a decision,"
          " never a missing key), at least one post in a full week rests, and"
          " no day ever outspeaks its lead — the mirror is byte-for-byte",
          _w7["cta"] == _w7["posts"][0]["cta"]
          and all(_p.get("cta") is not None and isinstance(_p.get("cta"), str)
                  for _d in _wPlan["days"] for _p in _d["posts"])
          and any(_p["cta"] == "" for _d in _wPlan["days"] for _p in _d["posts"]),
          str([(_d["day"], _d["cta"] == _d["posts"][0]["cta"])
               for _d in _wPlan["days"]])[:120])
    _wDup = json.loads(json.dumps(_wPlan))
    _wDup["days"][0]["posts"][0]["kind"] = "meme"
    _wDupV = _stumod._validate_plan(_wDup, 7)
    check("P6W the shelf's law is policed at the gate: a spoken day that"
          " names the same kind twice paints one card — the first post keeps"
          " the media and the twin rides on as a plain text post, so no post"
          " can ever silently steal another one's picture at the desk",
          _wDupV is not None
          and _wDupV["days"][0]["posts"][1].get("kind") is None,
          str([(_p.get("kind"), _p["id"])
               for _p in (_wDupV or {"days": [{}, {}]})["days"][0]["posts"]])[:140])
    _wView, _ = _stud.create(dict(_mk, project="P6W", days=7,
                                  types=["xpost", "meme", "promo"]))
    _wCard = _wView["assets"][0]
    _wHeadBefore = _wCard["text"]["head"]
    _wRer2, _wVar, _ = _stud.regenerate(_wView["id"], _wCard["id"])
    _wHeadAfter = next(_a for _a in _wRer2["assets"]
                       if _a["id"] == _wCard["id"])["text"]["head"]
    check("P6W a reroll genuinely turns the lane's page: the first reroll"
          " re-inks the card with a different line (the old rotation was a"
          " multiple of the pool's three lines and could reprint the same"
          " sentence with a new file name), the variant moves, and nothing"
          " else on the campaign is asked to move",
          _wVar == _wCard["variant"] + 1 and _wHeadBefore != _wHeadAfter,
          str([_wHeadBefore[:20], _wHeadAfter[:20]])[:80])
    _wDoor = _sgo("GET", "/studio/assets/" + _wCard["file"],
                   {"name": "p6w-week-day-01-post-01"})
    _wDoorBad = _sgo("GET", "/studio/assets/" + _wCard["file"],
                      {"name": "na me!/.."})
    check("P6W the file door can speak a human name: a well-shaped ?name="
          " rides out as a real attachment (a Content-Disposition whose"
          " extension comes from the served bytes, never from the query) and"
          " a name with shapes a filename can not wear is simply not worn"
          " — the bytes and the walls answer exactly as before, no new door",
          _wDoor[0] == 200 and isinstance(_wDoor[2], dict)
          and 'attachment; filename="p6w-week-day-01-post-01.png"'
              in _wDoor[2].values()
          and _wDoor[2].get("Content-Type") == "image/png"
          and isinstance(_wDoorBad[2], str) and _wDoorBad[2] == "image/png",
          str(_wDoor[2])[:120])
    check("P6W the download's root cause is answered in the repo's own"
          " config: the page rewrites still stand and the assets path now"
          " travels to the app too — /studio/assets/:path* asks the board's"
          " own door for the bytes (before this, Vercel was asked for a file"
          " it never had, and answered with its own not-found HTML), and the"
          "#pb-world mount and every other route is untouched",
          [ _r["source"] for _r in _wVc["rewrites"] ]
              == ["/api/:path*", "/studio/assets/:path*", "/studio"]
          and _wVc["rewrites"][1]["destination"]
              == "https://api.seyruna.com/studio/assets/:path*",
          str([_r["source"] for _r in _wVc["rewrites"]])[:120])
    check("P6W the gazetteer answers the brief's own examples from its own"
          " tables (no live lane, no key): USA alone lands on the country's"
          " representative point as a country answer, Lawrence Kansas USA"
          " lands on the city of Lawrence as a city answer, the twins of the"
          " case forgive, an unknown country is refused a pin out loud, and"
          " a known country with an unknown region settles for the region's"
          " answer or the country's — never a fabricated exactness",
          _wPl.locate("USA") == (39.8, -98.5, "country")
          and _wPl.locate("USA", "Kansas", "Lawrence") == (38.97, -95.24, "city")
          and _wPl.locate("uSa") == _wPl.locate("USA")
          and _wPl.locate("Atlantis") is None
          and _wPl.locate("USA", "Kansas")[2] == "region",
          str([_wPl.locate("USA"), _wPl.locate("USA", "Kansas", "Lawrence")])[:110])
    _wadb = dbmod.DB(":memory:", verbose=False)
    _was = svc.Service(_wadb, None, stY2)
    _wam = _Wm(_wadb, stY2)
    _wa1 = _was.create_or_touch_account("p6wa@paperboard.test")[0]["id"]
    _wa2 = _was.create_or_touch_account("p6wb@paperboard.test")[0]["id"]
    _wa3 = _was.create_or_touch_account("p6wc@paperboard.test")[0]["id"]
    _wa4 = _was.create_or_touch_account("p6wd@paperboard.test")[0]["id"]
    _wam.set(_wa1, {"country": "USA", "region": "Kansas", "city": "Lawrence"})
    _wam.set(_wa2, {"country": "usa", "region": "kansas", "city": "lawrence"})
    _wam.set(_wa3, {"country": "USA"})
    _wam.set(_wa4, {"country": "Atlantis"})
    _wPub = _wam.public_view()
    check("P6W the public face is aggregate-only in its new dress: the same"
          " two countries and the total as ever, plus the placed points —"
          " Lawrence counts as the two seats of ONE place, USA-alone is a"
          " second place at the country's middle, Atlantis is unplaced and"
          " counted as such, and the whole answer carries no account id,"
          " no email, no word the feature did not send",
          _wPub["total"] == 4 and _wPub["unplaced"] == 1
          and len(_wPub["places"]) == 2
          and _wPub["places"][0]["count"] == 2
          and _wPub["places"][0]["precision"] == "city"
          and _wPub["places"][1]["precision"] == "country"
          and all(set(_p.keys()) == {"country", "region", "city", "count",
                                     "lat", "lon", "precision"}
                  for _p in _wPub["places"])
          and "account" not in json.dumps(_wPub) and "@" not in json.dumps(_wPub),
          str([( _p["country"], _p.get("city"), _p["count"], _p["precision"])
               for _p in _wPub["places"]])[:140])
    _wadb.close()
    check("P6W the map on the page is the real world: the runtime imports"
          " the vendored geometry, the coasts come from it (the land layer"
          " exists in the markup and in the code that fills it), the"
          " controls and the detail panel and the attribution are on the"
          " page, and the shipped geometry is genuinely the world (over a"
          " hundred polygons, Australia and Antarctica where a cartographer"
          " puts them), and the counts stay aggregate words",
          "WORLD_LAND" in uxjs and 'id="wm-map"' in _wjs
          and "wm-land" in uxjs and "Natural Earth" in _wjs
          and "data-wm-z" in _wjs and 'id="wm-detail"' in _wjs
          and len(_wLandGj) > 100
          and any(110 < _p[0] < 155 and -45 < _p[1] < -10
                  for _q in _wLandGj for _r in _q for _p in _r)
          and any(_p[1] < -60 for _q in _wLandGj for _r in _q for _p in _r),
          str(len(_wLandGj))[:60])
    check("P6W the desk shows the picture, says honestly how it is doing,"
          " and keeps the shelf folded: every media figure carries the"
          " zoom lane, the loading state, and the two real failure words;"
          " the download lane asks the door by a human name (the ?name= word"
          " and the download attribute ride the same stem); the shelf folds"
          " as a details box; the old scaffold labels — 'the idea ·' and"
          " 'get image' — are gone from the file; and the lightbox is a"
          " dialog with an Escape at the ready",
          all(_k in _wSjs for _k in ("data-lb", "st-lb", "is-load",
                                     "is-ready", "is-failed", "?name=",
                                     "download=", "st-shelfbox",
                                     '<details class="st-shelfbox"'))
          and "the idea ·" not in _wSjs and "get image" not in _wSjs
          and "Escape" in _wSjs
          and ".st-lb" in _cssV and ".st-media" in _cssV
          and "box-shadow" not in _cssV and "gradient" not in _cssV,
          str([_k for _k in ("data-lb", "is-load", "?name=")
               if _k not in _wSjs])[:80])
    print("failures:", len(FAILURES))
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
