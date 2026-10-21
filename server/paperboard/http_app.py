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

from .accounts import normalize_email
from .artboard import Artboard
from .worldmap import WorldMap
from .arcade import Arcade, week_key
from .studio import Studio
from .settings import SITE_DIR
from .xapi import make_pkce, XError

_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg",
             "image/gif": "gif", "image/webp": "webp"}

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
    ".gif": "image/gif",
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
        self.art = Artboard(db, settings)
        self.wm = WorldMap(db, settings)
        self.ar = Arcade(db, settings)
        self.stud = Studio(db, settings)
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
        # the account layer resolves its own session from the same cookie —
        # X-legacy and account rows are told apart by the session's kind, so
        # neither flow can borrow the other's seat.
        account, session = s.account_session(cookies.get("PBSD"))
        mode = "mock" if settings.mock else "live"

        if path.startswith("/api/"):
            ip = headers.get("x-forwarded-for") or headers.get("x-real-ip") or "?"
            if not self._allow(f"api:{ip}",
                               settings.pb["ratelimit"]["apiPerMinute"], 60):
                return 429, {"error": "rate_limited"}
            return self._api(method, path, query, headers, cookies, body,
                             user, comp, cid, now, mode, account, session, ip)
        if path.startswith("/artworks/"):
            # the wall's bytes ride beside the DB, not beside the site: the
            # data dir is the authority for the files, the DB row is their
            # ledger, and this single branch is the only door between them.
            got = self.art.served(path[len("/artworks/"):])
            if got is None:
                return 404, {"error": "art_absent"}
            return 200, got[0], got[1]
        if path.startswith("/studio/assets/"):
            # the studio's cards obey the same law as the wall's hangings:
            # one named door, the ledger's own name-shape is the wall, and a
            # stranger's path never reaches the disk to be argued with.
            # a ?name= word (the desk's own human naming) rides out as a real
            # download name — sanitised to a shape no quote or slash can hide
            # in, with the extension taken from the served bytes' own type.
            got = self.stud.served(path[len("/studio/assets/"):])
            if got is None:
                return 404, {"error": "file_absent"}
            nm = query.get("name") or ""
            if nm and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", nm):
                return 200, got[0], {
                    "Content-Type": got[1],
                    "Content-Disposition": 'attachment; filename="%s.%s"'
                                           % (nm, _MIME_EXT[got[1]])}
            return 200, got[0], got[1]
        if path == "/":
            return self._static("index.html")
        if path == "/admin":
            # the admin shell is a public file; the DATA behind it is not.
            # a stranger who opens /admin sees an empty room and a 401, not
            # the queue — the gate lives server-side, where it belongs.
            return self._static("admin.html")
        if path == "/studio":
            return self._static("studio.html")
        return self._static(path.lstrip("/"))

    # ------------------------------------------------------------------ api
    def _api(self, method, path, query, headers, cookies, body, user, comp, cid, now,
              mode, account=None, session=None, ip="?"):
        s, settings, pb = self.s, self.settings, self.settings.pb
        rl = pb["ratelimit"]
        ok = lambda **kw: (200, dict(**kw), None)
        fail = lambda code, err: (code, {"error": err}, None)

        def cookie(token, samesite="Lax"):
            flags = "Path=/; HttpOnly; SameSite=" + samesite
            if settings.secure_cookies:
                flags += "; Secure"
            days = int(s.acfg.get("sessionDays", 7))
            return (f"PBSD={token}; {flags}; Max-Age={days * 86400}")

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
            if path == "/api/usage":
                if not settings.admin_token and not settings.mock:
                    return fail(503, "admin_token_not_configured")
                if (headers.get("x-paper-token") or "") != (settings.admin_token or "dev"):
                    return fail(401, "bad_admin_token")
                return ok(usage=s.usage_view())

            # ---- the account layer (identity = account id, never an X handle)
            if path == "/api/auth/magic":
                signed_in, why = s.consume_magic_link((query.get("token") or "").strip())
                if signed_in is None:
                    return fail(401, why)
                seat = s.account_new_session(signed_in["id"])
                return (302, "See you at the board.",
                        {"Location": settings.magic_landing(), "Set-Cookie": cookie(seat),
                         "Cache-Control": "no-store"})
            if path == "/api/account":
                if account is None:
                    return fail(401, "not_authenticated")
                if account.get("status") != "active":
                    return fail(403, "account_closed")
                # p0: one word of its own — the same server-side freshness the
                # wallet door gates on, spoken in the account view, so a board
                # can watch for the click instead of telling the member to go
                # rediscover login rituals. additive; the dashboard stays pure.
                return 200, dict(s.dashboard(account, cid),
                                 sessionFresh=bool(session and session.get("fresh"))), None
            if path == "/api/account/submissions":
                if account is None:
                    return fail(401, "not_authenticated")
                if cid is None:
                    return fail(404, "no_active_competition")
                return ok(submissions=s.submissions_view(account, cid), mode=mode)

            if path.startswith("/api/admin/") and path != "/api/admin/refresh":
                # the account-era admin doors. the gate is server-side every
                # time: a signed-in account whose email sits on the explicit
                # allowlist (env PAPER_ADMINS). no token, no role in the DB,
                # no client say-so. an empty allowlist locks EVERYONE out —
                # fail-closed is the only fail direction an admin board has.
                def admin_gate():
                    """None means 'walk right in'; anything else is the answer."""
                    if account is None:
                        return fail(401, "not_authenticated")
                    if not settings.admset:
                        return fail(403, "no_admins_configured")
                    if (account.get("email") or "").lower() not in settings.admset:
                        return fail(403, "admin_only")
                    if account.get("status") != "active":
                        return fail(403, "account_closed")
                    return None

                gate = admin_gate()
                if gate is not None:
                    return gate

                if path == "/api/admin/whoami":
                    return 200, dict(is_admin=True,
                                     email=(account.get("email") or "").lower()), None
                if path == "/api/admin/queue":
                    if not self._allow(f"admin:{ip}", rl["apiPerMinute"], 60):
                        return fail(429, "rate_limited")
                    status = (query.get("status") or "pending").strip().lower()
                    if status not in ("pending", "verified", "disputed", "corrected"):
                        return fail(400, "bad_status")
                    return ok(status_filter=status, mode=mode,
                              rows=s.admin_queue(cid, status))
                if path.startswith("/api/admin/payout"):
                    if not self._allow(f"payout:{account['id']}", rl["apiPerMinute"], 60):
                        return fail(429, "rate_limited")
                    want = (query.get("batch") or "").strip()
                    if want != "":
                        try:
                            bid = int(want)
                        except (TypeError, ValueError):
                            return fail(400, "bad_batch")
                        view = s.payout_view(bid)
                    else:
                        if cid is None:
                            return fail(404, "no_active_competition")
                        view = s.payout_latest(cid)
                    if not view.get("ok"):
                        return fail(404, view.get("reason", "payout_batch_not_found"))
                    return ok(**view)
                return fail(404, "unknown_api_route")
            if path == "/api/arcade":
                # the floor is config's own words — no bucket of its own
                # beyond the api door; a week rides the Friday the payouts
                # already keep, and the key travels so the sheet can echo it.
                return ok(**self.ar.floor())

            if path == "/api/arcade/leaderboard":
                wk = query.get("week") or week_key()
                rows = self.ar.board(query.get("game") or "", wk)
                if rows is None:
                    return fail(404, "unknown_game")
                return ok(game=query.get("game"), week=wk, rows=rows)

            if path == "/api/worldmap":
                if not self._allow(f"worldmap:{ip}",
                                   rl["worldmapPerMinute"], 60):
                    return fail(429, "rate_limited")
                return ok(**self.wm.public_view())

            if path == "/api/account/location":
                # the member's own seat, for their own eyes: no aggregate
                # pretence here — one row or none, spoken by a session that
                # passed the door (the content-type wall is a write wall, so
                # the seat's own check is the slim one: an account or a 401).
                if account is None:
                    return fail(401, "not_authenticated")
                return ok(location=self.wm.view(account["id"]))

            if path == "/api/artworks":
                if not self._allow(f"artworks:{ip}",
                                   rl["artworksPerMinute"], 60):
                    return fail(429, "rate_limited")
                return ok(artworks=self.art.list(query.get("sort") or "new"))

            # ---- the studio (feature four): the campaign desk, anonymous by
            # design — a campaign is a thing you make before it is a thing you
            # own, and the rate bucket rides the address, not a seat.
            if path == "/api/studio":
                return 200, self.stud.capabilities(), None
            if path == "/api/studio/campaigns":
                return ok(campaigns=self.stud.list())
            if path == "/api/studio/campaign":
                try:
                    scid = int(query.get("id") or "")
                except ValueError:
                    return fail(400, "bad_id")
                seen = self.stud.view(scid)
                if seen is None:
                    return fail(404, "campaign_absent")
                return ok(campaign=seen)
            if path == "/api/studio/export":
                if not self._allow(f"studio:{ip}",
                                   rl.get("studioPerMinute", 60), 60):
                    return fail(429, "studio_rate_limited")
                try:
                    scid = int(query.get("id") or "")
                except ValueError:
                    return fail(400, "bad_id")
                blob, fname, _names = self.stud.export(scid)
                if blob is None:
                    return fail(404, "campaign_absent")
                return (200, blob,
                        {"Content-Type": "application/zip",
                         "Content-Disposition":
                             'attachment; filename="%s"' % fname})
            return fail(404, "unknown_api_route")

        if method == "POST":
            # CSRF shape-guard: SameSite=Lax carries the wall, and a JSON-only
            # door keeps a plain form post from moving account state.
            ctype = headers.get("content-type") or ""

            def acct_gate():
                """Shared door-check for the authenticated account routes:
                session present, JSON-shaped body, account in good standing.
                None means 'walk right in'; anything else is the answer."""
                if account is None:
                    return fail(401, "not_authenticated")
                if ctype and not ctype.startswith("application/json"):
                    return fail(415, "unsupported_media_type")
                if account.get("status") != "active":
                    return fail(403, "account_closed")
                return None

            if path.startswith("/api/admin/payout/"):
                # the friday hand: a human moves the batch, the server decides
                # whether the move is legal — states, ids, and rights are all
                # checked behind this door, never in the browser.
                gate = acct_gate()
                if gate is not None:
                    return gate
                email = (account.get("email") or "").lower()
                if not settings.admset or email not in settings.admset:
                    return fail(403, "admin_only")
                if not self._allow(f"payout:{account['id']}", 60, 60):
                    return fail(429, "payout_rate_limited")
                action = path[len("/api/admin/payout/"):]
                b = body or {}
                try:
                    bid = int(b.get("batch") or 0)   # prepare needs none; 0 = no such batch
                except (TypeError, ValueError):
                    return fail(400, "bad_batch")
                faults = b.get("faults") or []
                if action == "prepare":
                    extra = b.get("poolBase")
                    out = s.payout_prepare(cid, email,
                                            int(extra) if extra is not None else None)
                elif action == "freeze":
                    out = s.payout_freeze(bid, email)
                elif action == "approve":
                    out = s.payout_approve(bid, email, b.get("confirmTotal"))
                elif action == "simulate":
                    out = s.payout_simulate(bid, email, faults)
                elif action == "retry":
                    out = s.payout_retry(bid, email, faults)
                elif action == "reconcile":
                    out = s.payout_reconcile(bid, email)
                elif action == "cancel":
                    out = s.payout_cancel(bid, email, str(b.get("reason") or ""))
                else:
                    return fail(400, "bad_payout_action")
                if not out.get("ok"):
                    reason = out.get("reason", "payout_failed")
                    codes = {"payout_batch_not_found": 404,
                             "payout_already_executed": 409,
                             "payout_open_batch": 409}
                    return fail(codes.get(reason, 400), reason)
                return ok(**out)

            if path == "/api/admin/verify":
                # the account-era manual verification. the gate is the whole
                # point: server-side, every time. a browser may SUGGEST a
                # count; it never supplies a score, and a non-admin never
                # gets the door open at all (403, not a polite 200).
                gate = acct_gate()
                if gate is not None:
                    return gate
                email = (account.get("email") or "").lower()
                if not settings.admset or email not in settings.admset:
                    return fail(403, "admin_only")
                if not self._allow(f"adminverify:{account['id']}", 60, 60):
                    return fail(429, "verify_rate_limited")
                b = body or {}
                try:
                    sub_id = int(b.get("id"))
                except (TypeError, ValueError):
                    return fail(400, "bad_id")
                status = (b.get("status") or "verify").strip().lower()
                if status not in ("verify", "dispute"):
                    return fail(400, "bad_status")
                raw = b.get("likes")
                count = None
                if status == "verify":
                    if raw is None or isinstance(raw, bool):
                        return fail(400, "bad_likes")
                    try:
                        count = int(raw)
                    except (TypeError, ValueError):
                        return fail(400, "bad_likes")
                    if count < 0:
                        return fail(400, "bad_likes")
                res = s.verify_like(
                    sub_id, email, count, b.get("measuredAt"),
                    (b.get("reason") or "")[:400],
                    (b.get("method") or "manual")[:60])
                if not res.get("ok"):
                    code = {"submission_not_found": 404,
                            "nothing_to_dispute": 409,
                            "bad_count": 400,
                            "suspicious_count": 400}.get(res.get("reason"), 500)
                    return fail(code, res.get("reason", "verify_failed"))
                return ok(**res)

            if path == "/api/account/start":
                # the one anonymous door: no session yet, so no gate — only
                # the two rate buckets and the email shape.
                # generous per-IP (a NAT full of readers), tight per-inbox —
                # the email bucket is the one that guards a single victim.
                if not self._allow(f"start:{ip}", 30, 600):
                    return fail(429, "magic_rate_limited")
                email = normalize_email((body or {}).get("email"))
                if email is None:
                    return fail(422, "invalid_email")
                if not self._allow(f"magic:{email}", rl["magicSendPerTenMinutes"], 600):
                    return fail(429, "magic_rate_limited")
                acct, _created, err = s.create_or_touch_account(email)
                if acct is None:
                    return fail(403, err)
                token, mailed = s.issue_magic_link(acct["id"])
                if mailed:
                    try:    # an inbox cannot click a relative link: base it out
                        s.mail.send_magic_link(
                            acct["email"],
                            settings.public_base + f"/api/auth/magic?token={token}")
                    except (RuntimeError, OSError) as e:
                        # the wire gets the one clean key, like every other
                        # refusal; the smtp reply (or the socket's own words)
                        # stays server-side. a connect failure that raises
                        # OSError instead of RuntimeError is still a mail
                        # delivery failure — it must not fall to a bare 500.
                        print(f"[mail] send refused: {e}")
                        return fail(502, "mail_failed")
                else:  # p6p: a deferral is visible server-side, never silent
                    print("[mail] deferred (inside the 45 s resend window)")
                out = (dict(sent=True) if mailed else
                       dict(sent=False, deferred=True))  # a deferral says so
                if s.mail.mode == "mock":               # dev-only echo — the
                    echo = (token if mailed else        # plaintext survives
                            s.mail.last_token_for(acct["email"]))  # in the mailer
                    if echo:
                        out["link"] = f"/api/auth/magic?token={echo}"
                return ok(**out)

            if path == "/api/account/onboard":
                denied = acct_gate()
                if denied:
                    return denied
                err = s.onboard_account(account, (body or {}).get("username"),
                                        (body or {}).get("wallet"),
                                        (body or {}).get("xhandle"))
                if err:
                    return fail(422, err)
                return 200, s.dashboard(s.account_by_id(account["id"]), cid), None

            if path == "/api/account/wallet":
                # the money door: gated like its siblings, with its own bucket
                # (the pattern every account-state POST keeps), and only then
                # handed to the service, whose fresh gate answers last.
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"wallet:{account['id']}",
                                   rl["walletPerTenMinutes"], 600):
                    return fail(429, "wallet_rate_limited")
                addr, err = s.change_wallet(
                    account, (body or {}).get("wallet"),
                    bool(session and session.get("fresh")))
                if err:
                    return fail({"fresh_auth_required": 428,
                                 "invalid_solana_address": 422,
                                 "wallet_unchanged": 422}.get(err, 422), err)
                fresh_row = s.account_by_id(account["id"])
                return ok(wallet=addr,
                          wallet_effective_at=fresh_row["wallet_effective_at"])

            if path == "/api/account/x/verify":
                # an ownership proof, not a login: the post itself is read
                # server-side — author, wording, existence — and nothing the
                # browser claims is taken at face value. one wall per post,
                # one per account, one identity per account.
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"xverify:{account['id']}",
                                   rl.get("xverifyPerTenMinutes", 10), 600):
                    return fail(429, "xverify_rate_limited")
                res = s.verify_x(account, (body or {}).get("url"),
                                 (body or {}).get("handle"))
                if not res.get("ok"):
                    return {"bad_post_url": 400, "post_not_found": 404,
                            "provider_unavailable": 502,
                            "author_mismatch": 403,
                            "x_handle_taken": 409,
                            "verification_post_used": 409,
                            "verification_exists": 409,
                            "verify_wording": 422,
                            "verification_pending": 424,
                            "bad_x_handle": 422}.get(
                                res.get("reason", "verify_failed"), 400), \
                        {"error": res.get("reason", "verify_failed")}, None
                return ok(**res)

            if path == "/api/account/posts":
                denied = acct_gate()
                if denied:
                    return denied
                if cid is None:
                    return fail(404, "no_active_competition")
                if not self._allow(f"submit:{account['id']}",
                                   rl["submitPerTenMinutes"], 600):
                    return fail(429, "submit_rate_limited")
                res = s.submit_post(account, (body or {}).get("url"), cid)
                if not res.get("ok"):
                    reason = res.get("reason", "submit_failed")
                    payload = {"error": reason}
                    if reason == "duplicate_submission":
                        payload["owner"] = res.get("owner")
                        payload["points"] = res.get("points")
                    return {"bad_post_url": 400, "post_not_found": 404,
                            "duplicate_submission": 409,
                            "submission_limit": 429,
                            "provider_unavailable": 502,
                            "not_verified": 403, "wrong_author": 403}.get(
                                reason, 400), payload, None
                return ok(submission=res["submission"], mode=mode)

            if path == "/api/artwork/start":
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"upload:{account['id']}",
                                   rl["uploadPerTenMinutes"], 600):
                    return fail(429, "upload_rate_limited")
                row, why = self.art.add(account["id"], body or {})
                if row is None:
                    return fail({"bad_title": 422, "bad_credit": 422,
                                 "bad_note": 422, "too_large": 422,
                                 "bad_image_data": 422, "bad_image_type": 422,
                                 "daily_cap": 429, "account_absent": 404,
                                 "store_failed": 503}.get(why, 400), why)
                return 201, {"artwork": row}, None

            if path == "/api/artwork/vote":
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"vote:{account['id']}",
                                   rl["votePerTenMinutes"], 600):
                    return fail(429, "vote_rate_limited")
                state, count = self.art.vote((body or {}).get("art_id"),
                                            account["id"])
                if state == "art_absent":
                    return fail(404, "art_absent")
                if state == "duplicate":
                    # the doctrine again: a re-vote changed nothing, so the
                    # answer is a truthful 200 that defers — not an error.
                    return ok(voted=True, deferred=True, count=count)
                return ok(voted=True, count=count)

            if path == "/api/artwork/remove":
                denied = acct_gate()
                if denied:
                    return denied
                out = self.art.remove((body or {}).get("art_id"),
                                       account["id"])
                if out == "art_absent":
                    return fail(404, "art_absent")
                if out == "not_owner":
                    return fail(403, "not_owner")
                return ok(removed=True)

            if path == "/api/account/location":
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"location:{account['id']}",
                                   rl["locationPerTenMinutes"], 600):
                    return fail(429, "location_rate_limited")
                row, why = self.wm.set(account["id"], body or {})
                if row is None:
                    return fail({"bad_country": 422, "bad_region": 422,
                                 "bad_city": 422,
                                 "account_absent": 404}.get(why, 400), why)
                return ok(location=row)

            if path == "/api/account/location/remove":
                denied = acct_gate()
                if denied:
                    return denied
                if self.wm.remove(account["id"]) == "no_location":
                    return fail(404, "no_location")
                return ok(removed=True)

            if path == "/api/arcade/score":
                # the word of a hand on the week's board. the week is named
                # by the server (a client cannot gild its week), the score is
                # validated here (a browser is a witness, not an authority),
                # and the law is best-per-week: first books at 201, better
                # replaces, and a lesser or equal word DEFERS — 200 with the
                # deferred word, the same doctrine the re-vote speaks.
                denied = acct_gate()
                if denied:
                    return denied
                if not self._allow(f"score:{account['id']}",
                                   rl["scorePerTenMinutes"], 600):
                    return fail(429, "score_rate_limited")
                out, row = self.ar.submit(account["id"],
                                          (body or {}).get("game", ""),
                                          (body or {}).get("score"))
                if out == "unknown_game":
                    return fail(404, "unknown_game")
                if out == "bad_score":
                    return fail(422, "bad_score")
                if out == "no_improvement":
                    return ok(recorded=row, deferred=True)
                if out == "first":
                    return (201, {"recorded": row})
                return ok(recorded=row, replaced=True)

            if path == "/api/studio/campaigns":
                # the one studio door that speaks: a brief in, a campaign and
                # its rendered cards out. no seat needed — the IP bucket is
                # the wall, and the JSON vow holds like every other write.
                if ctype and not ctype.startswith("application/json"):
                    return fail(415, "unsupported_media_type")
                if not self._allow(f"studiowrite:{ip}",
                                   rl.get("studioWritePerTenMinutes", 10), 600):
                    return fail(429, "studio_write_rate_limited")
                made, why = self.stud.create(body or {})
                if made is None:
                    return fail({"bad_project": 422, "bad_brief": 422,
                                 "bad_days": 422, "bad_types": 422,
                                 "bad_image_data": 422,
                                 "bad_image_type": 422, "too_large": 422,
                                 "unsupported_png": 422,
                                 "studio_store_failed": 503}.get(why, 400), why)
                return (201, {"campaign": made}, None)

            if path == "/api/studio/regenerate":
                if ctype and not ctype.startswith("application/json"):
                    return fail(415, "unsupported_media_type")
                if not self._allow(f"studiowrite:{ip}",
                                   rl.get("studioWritePerTenMinutes", 10), 600):
                    return fail(429, "studio_write_rate_limited")
                b = body or {}
                try:
                    scid, said = int(b.get("campaign")), int(b.get("asset"))
                except (TypeError, ValueError):
                    return fail(400, "bad_id")
                fresh, var, why = self.stud.regenerate(scid, said)
                if fresh is None:
                    return fail({"asset_absent": 404,
                                 "campaign_absent": 404}.get(why, 400), why)
                return ok(campaign=fresh, variant=var)

            if path == "/api/auth/logout":
                s.end_session(cookies.get("PBSD"))
                return ok(loggedOut=True)

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
                s.record_x(dict(kind="users", resources=1, label="/users/me (login)"))
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
                res = s.scan_user(user, cid, force=bool(query.get("force")))
                if not res.get("ok"):
                    reason = res.get("reason", "scan_failed")
                    body = {"error": reason}
                    if res.get("retry_after") is not None:
                        body["retryAfter"] = res["retry_after"]
                    code = {"scan_cooldown": 429, "scan_inflight": 429,
                            "scan_busy": 503,
                            "scan_budget_exhausted": 503}.get(reason, 502)
                    return code, body, None
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
