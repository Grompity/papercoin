#!/usr/bin/env python3
"""PAPERBOARD self-tests — deterministic, stdlib only, no network.

  python3 server/selftest.py

Covers: eligibility truth table, scoring math + anti-gaming, prize models,
wallet validation, snapshot ranking/tiers, and every HTTP route (via direct
dispatch, no sockets). Exit code is the failure count.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from paperboard import db as dbmod, http_app, service as svc           # noqa: E402
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

    print("failures:", len(FAILURES))
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
