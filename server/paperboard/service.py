"""Service layer — the only code that touches both the DB and X.

Everything the frontend sees comes out of here already computed, so the
client never has to (and never gets to) decide what a score means.
"""

import json
import threading
import time
from datetime import date as _date, datetime as _dt, time as _time

from .accounts import (hash_magic_token, new_magic_token, normalize_email,
                       normalize_username, parse_post_url)
from .eligibility import Eligibility, normalize_wallet
from .mail import make_mailer
from .prizes import estimate, tier_for
from .scoring import normalize_text, score_post
from .xapi import make_pkce, XError, XPostGone


def now():
    return time.time()


class Service:
    def __init__(self, db, x, settings):
        self.db = db
        self.x = x
        self.settings = settings
        cfg = settings.pb
        self.scoring = dict(cfg["scoring"])
        # single knob: minImpressions lives with eligibility, the engine reads it too
        self.scoring.setdefault("minImpressions", cfg["eligibility"].get("minImpressions", 10))
        self.elig = Eligibility(cfg)
        self.eligcfg = dict(cfg.get("eligibility", {}))
        self.prize = cfg["prize"]
        self.tiers = cfg["tiers"]
        self.refresh_seconds = cfg["refreshMinutes"] * 60
        self.scan_cfg = dict(cfg.get("scan", {}))
        self.metrics_cfg = dict(cfg.get("metrics", {}))
        self.budget_cfg = dict(cfg.get("budget", {}))
        self.costs = dict(cfg.get("apiCosts", {}))
        self.acfg = dict(cfg.get("account", {}))     # the account layer's timing rules
        self.mail = make_mailer(settings)            # provider boundary, mock by default
        self._inflight = set()
        self._scan_lock = threading.Lock()
        self._active_scans = 0

    # ------------------------------------------------------------------ boot
    def ensure_competition(self):
        c = self.settings.pb["competition"]
        row = self.db.conn.execute(
            "SELECT id FROM competitions WHERE slug=?", (c["slug"],)).fetchone()
        if row:
            return row["id"]
        self.db.conn.execute(
            "INSERT INTO competitions(slug,name,description,starts_at,ends_at,status,config_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (c["slug"], c["name"], c["description"], c["startsAt"], c["endsAt"],
             c["status"], json.dumps({k: v for k, v in self.settings.pb.items()
                                      if k != "competition"})))
        self.db.conn.commit()
        return self.db.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    def active_competition(self):
        row = self.db.conn.execute(
            "SELECT * FROM competitions WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ users
    def upsert_user(self, user):
        """user: X payload {id, username, name, profile_image_url}"""
        t = now()
        cur = self.db.conn.execute("SELECT id, wallet FROM users WHERE x_user_id=?",
                                    (user["id"],))
        row = cur.fetchone()
        if row:
            self.db.conn.execute(
                "UPDATE users SET x_username=?, display_name=?, avatar_url=?, updated_at=?"
                " WHERE id=?",
                (user["username"], user.get("name") or "", user.get("profile_image_url") or "",
                 t, row["id"]))
            uid = row["id"]
        else:
            self.db.conn.execute(
                "INSERT INTO users(x_user_id,x_username,display_name,avatar_url,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (user["id"], user["username"], user.get("name") or "",
                 user.get("profile_image_url") or "", t, t))
            uid = self.db.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        self.db.conn.commit()
        return uid

    def session_user(self, token):
        """Connect-X-era seat: only rows without the account marker count —
        an account session and an X session must never blur into each
        other, whichever door is being knocked."""
        if not token:
            return None
        row = self.db.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token=? AND s.account_id IS NULL", (token,)).fetchone()
        return dict(row) if row else None

    def new_session(self, user_id):
        """Connect-X-era session (users table). The account layer has its
        own helpers below; created_at is the epoch as float — the freshness
        gate and the legacy rows both ride that one column."""
        import secrets
        token = secrets.token_hex(24)
        self.db.conn.execute(
            "INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
            (token, user_id, float(now())))
        self.db.conn.commit()
        return token

    def set_wallet(self, user_id, wallet):
        addr = normalize_wallet(wallet or "")
        if addr is None:
            return None, "invalid_solana_address"
        self.db.conn.execute("UPDATE users SET wallet=?, updated_at=? WHERE id=?",
                             (addr, now(), user_id))
        self.db.conn.commit()
        return addr, None

    # ---------------------------------------------------------------- accounts
    # The PAPERBOARD account — not the X user — is the permanent identity.
    # Everything below keys on the immutable account id; X rides along as the
    # author printed on a submission, never as the account itself.

    def _last_id(self):
        return self.db.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]

    def account_by_id(self, account_id):
        row = self.db.conn.execute("SELECT * FROM accounts WHERE id=?",
                                    (account_id,)).fetchone()
        return dict(row) if row else None

    def account_by_email(self, email):
        row = self.db.conn.execute("SELECT * FROM accounts WHERE email=?",
                                    (email,)).fetchone()
        return dict(row) if row else None

    def create_or_touch_account(self, raw_email):
        """Returns (row, created, err). Same response shape whether the
        email is new or known — the endpoint never says which emails
        already have an account (no enumeration through this door)."""
        email = normalize_email(raw_email)
        if email is None:
            return None, False, "invalid_email"
        row = self.account_by_email(email)
        t = now()
        if row:
            if row["status"] != "active":
                return None, False, "account_closed"
            self.db.conn.execute("UPDATE accounts SET updated_at=? WHERE id=?",
                                 (t, row["id"]))
            self.db.conn.commit()
            return self.account_by_id(row["id"]), False, None
        self.db.conn.execute(
            "INSERT INTO accounts(email,created_at,updated_at) VALUES(?,?,?)",
            (email, t, t))
        self.db.conn.commit()
        return self.account_by_id(self._last_id()), True, None

    def issue_magic_link(self, account_id, at=None):
        """Short-lived + single-use. The sha256 hash is the stored form; the
        plaintext rides out in the email link and is never read back — a
        resend inside the echo window defers to the mailer, which remembers
        what it mailed. link spam is an attack like any other.
        Returns (token or None, mailed)."""
        t = now() if at is None else float(at)
        window = float(self.acfg.get("magicResendSeconds", 45))
        recent = self.db.conn.execute(
            "SELECT 1 FROM magic_links WHERE account_id=? AND used_at IS NULL"
            " AND expires_at>? AND created_at>? ORDER BY created_at DESC LIMIT 1",
            (account_id, t, t - window)).fetchone()
        if recent:
            return None, False
        ttl = float(self.acfg.get("magicLinkMinutes", 15))
        token = new_magic_token()
        self.db.conn.execute(
            "INSERT INTO magic_links(token_hash,account_id,created_at,expires_at)"
            " VALUES(?,?,?,?)",
            (hash_magic_token(token), account_id, t, t + ttl * 60))
        self.db.conn.commit()
        return token, True

    def consume_magic_link(self, token, at=None):
        """Returns (account, reason). Unknown, already-burned, and stale
        links are three distinct reasons behind one coarse answer; the
        reason itself is audit, not a user-facing detail."""
        t = now() if at is None else float(at)
        if not token or not isinstance(token, str):
            return None, "bad_link"
        row = self.db.conn.execute(
            "SELECT m.used_at, m.expires_at, m.account_id, a.status"
            " FROM magic_links m JOIN accounts a ON a.id=m.account_id"
            " WHERE m.token_hash=?", (hash_magic_token(token),)).fetchone()
        if row is None:
            return None, "bad_link"
        if row["used_at"] is not None:
            return None, "used_link"
        if row["expires_at"] <= t:
            return None, "expired_link"
        if row["status"] != "active":
            return None, "account_closed"
        self.db.conn.execute("UPDATE magic_links SET used_at=? WHERE token_hash=?",
                             (t, hash_magic_token(token)))
        self.db.conn.execute(
            "UPDATE accounts SET email_verified=1, last_login_at=?, updated_at=?"
            " WHERE id=?", (t, t, row["account_id"]))
        self.db.conn.commit()
        return self.account_by_id(row["account_id"]), None

    # ------------------------------------------------- account sessions
    def account_new_session(self, account_id):
        """An account seat leaves user_id NULL — the users FK is not a joke
        to be winked at, and a null that can never join is a stronger
        statement than a number that happens to collide with one."""
        import secrets
        token = secrets.token_hex(24)
        t = now()
        days = float(self.acfg.get("sessionDays", 30))
        self.db.conn.execute(
            "INSERT INTO sessions(token,account_id,created_at,expires_at,"
            "last_seen_at) VALUES(?,?,?,?,?)",
            (token, account_id, float(t), t + days * 86400, t))
        self.db.conn.commit()
        return token

    def account_session(self, token, at=None):
        """Resolves an account session, then keeps it honest: expired rows
        are deleted on the spot (invalidation), a seen session slides its
        window (renewal). Returns (account, meta) — meta.created rides the
        login instant so the fresh-auth gate can ask 'did you just click a
        magic link' and get a server-side answer, never a browser's."""
        if not token:
            return None, None
        t = now() if at is None else float(at)
        row = self.db.conn.execute(
            "SELECT account_id, created_at, expires_at FROM sessions"
            " WHERE token=? AND account_id IS NOT NULL", (token,)).fetchone()
        if row is None:
            return None, None
        if row["expires_at"] is not None and row["expires_at"] <= t:
            self.db.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self.db.conn.commit()
            return None, None
        account = self.account_by_id(row["account_id"])
        if account is None or account["status"] != "active":
            return None, None                       # closed account: no ghost seat
        days = float(self.acfg.get("sessionDays", 30))
        expires = row["expires_at"] or (t + days * 86400)
        if expires - t < days * 86400 / 2:          # slide, don't spam writes
            expires = t + days * 86400
        self.db.conn.execute("UPDATE sessions SET expires_at=?, last_seen_at=?"
                             " WHERE token=?", (expires, t, token))
        self.db.conn.commit()
        created = float(row["created_at"])
        fresh_min = float(self.acfg.get("freshAuthMinutes", 15))
        return account, dict(created=created, fresh=(t - created) <= fresh_min * 60)

    def end_session(self, token):
        if token:
            self.db.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self.db.conn.commit()

    # ---------------------------------------------------- onboarding + wallet
    def onboard_account(self, account, username, wallet, at=None):
        """The first-completion gate: a byline and the reward address, both
        server-validated. No signature is ever asked for — an address, not
        a permission."""
        t = now() if at is None else float(at)
        name = normalize_username(username)
        if name is None:
            return "bad_username"
        clash = self.db.conn.execute(
            "SELECT id FROM accounts WHERE lower(username)=lower(?) AND id<>?",
            (name, account["id"])).fetchone()
        if clash:
            return "username_taken"
        addr = normalize_wallet(wallet or "")
        if addr is None:
            return "invalid_solana_address"
        self.db.conn.execute(
            "UPDATE accounts SET username=?, wallet=?, wallet_effective_at=?,"
            " onboarded_at=?, updated_at=? WHERE id=?",
            (name, addr, t, t, t, account["id"]))
        self.db.conn.execute(
            "INSERT INTO wallet_audit(account_id,old_wallet,new_wallet,changed_at,"
            "effective_at) VALUES(?,?,?,?,?)", (account["id"], None, addr, t, t))
        self.db.conn.commit()
        return None

    def change_wallet(self, account, wallet, fresh, at=None):
        """Account-settings wallet swap: authenticated (route) + freshly
        authenticated (this gate — an attacker holding a warm session still
        has to click a new magic link to move the money address). The new
        address becomes reward-eligible only after the cooldown, so a
        temporary account thief cannot instantly reroute a reward."""
        t = now() if at is None else float(at)
        if not fresh:
            return None, "fresh_auth_required"
        addr = normalize_wallet(wallet or "")
        if addr is None:
            return None, "invalid_solana_address"
        if account.get("wallet") == addr:
            return addr, None                       # same address: no audit churn
        cool = float(self.acfg.get("walletCooldownMinutes", 60))
        eff = t + cool * 60
        self.db.conn.execute(
            "UPDATE accounts SET wallet=?, wallet_effective_at=?, updated_at=?"
            " WHERE id=?", (addr, eff, t, account["id"]))
        self.db.conn.execute(
            "INSERT INTO wallet_audit(account_id,old_wallet,new_wallet,changed_at,"
            "effective_at) VALUES(?,?,?,?,?)",
            (account["id"], account.get("wallet"), addr, t, eff))
        self.db.conn.commit()
        return addr, None

    # ------------------------------------------------------------- submission
    def submit_post(self, account, raw_url, competition_id, at=None):
        """The paste-the-URL pipeline: parse → provider resolve → duplicate
        wall → eligibility → server-side score → stored audit. The provider
        (not the browser, not the pasted byline) says who wrote the post;
        the account owns the submission whoever that author is — an
        ownership claim can ride the same rows later without a rebuild."""
        t = now() if at is None else float(at)
        parsed = parse_post_url(raw_url)
        if parsed is None:
            return dict(ok=False, reason="bad_post_url")
        post_id, _pasted_byline, fallback_url = parsed
        midnight = _dt.combine(_date.today(), _time.min).timestamp()
        cap = float(self.acfg.get("maxSubmissionsPerDay", 30))
        today = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM submissions"
            " WHERE account_id=? AND submitted_at>=?", (account["id"], midnight)
        ).fetchone()["n"]
        if today >= cap:
            return dict(ok=False, reason="submission_limit")
        dup = self.db.conn.execute(
            "SELECT account_id, points FROM submissions"
            " WHERE competition_id=? AND x_post_id=?",
            (competition_id, post_id)).fetchone()
        if dup:
            return dict(ok=False, reason="duplicate_submission",
                        owner="self" if dup["account_id"] == account["id"] else "other",
                        points=dup["points"])
        try:
            post = self.x.resolve_post(post_id, raw_url)
        except XPostGone:
            return dict(ok=False, reason="post_not_found")
        except XError as e:
            return dict(ok=False, reason="provider_unavailable", detail=str(e))
        if post is None:
            return dict(ok=False, reason="post_not_found")
        handle = (post.get("author") or "").lower()
        url = post.get("url") or fallback_url
        if not handle:
            url = fallback_url
        # the follow gate left the automatic path (2026-10 rules): no free
        # provider can vouch for it. when the rules DO demand it (config),
        # an unknown gate defers rather than guessing, and a gate that cannot
        # even be asked stays honest: the audit says 'advisory', not 'yes'.
        require_follow = bool(self.eligcfg.get("followRequired", False))
        follows = "unchecked"
        if require_follow and handle and hasattr(self.x, "follows_username"):
            try:
                follows = self.x.follows_username(handle)
            except XError:
                follows = None                # a gate that cannot answer defers
        text = post.get("text") or ""
        matched = self.elig.matched_identifier(text)
        verification = post.get("verification") or (
            "engagement" if (post.get("provided") or []) else "presence")
        if matched is None:
            eligible, reason = False, "no_identifier"
        elif require_follow and follows is False:
            eligible, reason = False, "no_follow"
        elif require_follow and follows is None:
            eligible, reason = True, "follow_deferred"
        elif verification == "presence":
            eligible, reason = True, "presence_verified"
        else:
            eligible, reason = True, "ok"
        metrics = dict(post.get("metrics") or {})
        if eligible:
            points, audit = score_post(metrics, self.scoring, {})
        else:
            points, audit = 0.0, dict(applied=["not_eligible"], contributions={})
        audit["matched"] = matched
        audit["follow"] = ("advisory" if follows == "unchecked"
                           else "yes" if follows
                           else "deferred" if follows is None else "no")
        audit["provided"] = list(post.get("provided") or [])
        audit["missing"] = [k for k in ("likes", "replies", "reposts", "quotes",
                                        "impressions") if metrics.get(k) is None]
        audit["verification"] = verification
        audit["provider"] = post.get("provider") or "unknown"
        audit["fetched_at"] = post.get("fetched_at")
        # the like-count truth machine, recorded at insert time (pb-v3):
        # an automatic observation is a 'reported' candidate (it becomes
        # authoritative only when an admin verifies it); absence is
        # 'pending' — the review queue's entrance. never a zero.
        likes_obs = metrics.get("likes")
        like_status = "reported" if likes_obs is not None else "pending"
        official_likes = likes_obs
        like_measured_at = t if likes_obs is not None else None
        like_source = (post.get("provider") if likes_obs is not None else None)
        self.db.conn.execute(
            "INSERT INTO submissions(account_id,competition_id,x_post_id,author_handle,"
            "url,text,posted_at,submitted_at,verified,eligible,reason,points,"
            "metrics_json,score_json,verification,like_status,official_likes,"
            "like_measured_at,like_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account["id"], competition_id, post_id, handle, url,
             text, post.get("posted_at"), t, 1, int(eligible), reason, points,
             json.dumps(metrics), json.dumps(audit), verification,
             like_status, official_likes, like_measured_at, like_source))
        self.db.conn.commit()
        return dict(ok=True, reason=reason, submission=dict(
            id=self._last_id(), x_post_id=post_id, author=handle, url=url,
            posted_at=post.get("posted_at"), submitted_at=t, eligible=eligible,
            reason=reason, points=points, provided=audit["provided"],
            missing=audit["missing"], matched=matched, text=text,
            verification=verification, provider=audit["provider"],
            like_status=like_status))

    # ------------------------------------------------------- manual verification
    def verify_like(self, submission_id, admin_email, likes=None,
                    measured_at=None, reason="", method="manual",
                    competition_id=None):
        """An admin's hand on the official like count — the only door that
        writes one (the submit path records observations; it never crowns).

        action is 'verify' (a count rides along) or 'dispute' (the admin
        looked and the count cannot stand; no count given). the row state
        machine is: pending -> verified (via 'verify') with disputed as a
        flag an admin sets through 'dispute' — never from the browser at
        large, and never silently.

        points are RECOMPUTED from the stored snapshot with the count
        substituted in — a browser never supplies a score. a repeat of the
        same verification is therefore idempotent by construction: the audit
        gains an event row, and the points are the very same number.
        """
        row = self.db.conn.execute(
            "SELECT id, account_id, submitted_at, points, metrics_json,"
            " score_json, like_status, official_likes, like_measured_at, eligible"
            " FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row is None:
            return dict(ok=False, reason="submission_not_found")
        action = "dispute" if likes is None else "verify"
        if likes is not None and (isinstance(likes, bool)
                                  or not isinstance(likes, int)
                                  or likes < 0):
            return dict(ok=False, reason="bad_count")
        if action == "verify" and likes > 10_000_000:
            return dict(ok=False, reason="suspicious_count")
        if action == "dispute" and row["like_status"] not in ("pending", "reported",
                                                              "verified", "disputed"):
            return dict(ok=False, reason="nothing_to_dispute")

        metrics = json.loads(row["metrics_json"] or "{}")
        post = dict(metrics)                    # the snapshot, as one post view
        prev_score = json.loads(row["score_json"] or "{}")
        prev_points = row["points"]
        prev_count = row["official_likes"]
        t = time.time()
        if action == "dispute":
            # the count stands (it is the last official word) but wears the
            # flag; the snapshot stays the authority for the recomputation.
            like_status = "disputed"
            official = row["official_likes"]
            measured = row["like_measured_at"] or t
            post["likes"] = official
            ctx = {"disputed": True}
        else:
            like_status = "verified"
            official = likes
            measured = float(measured_at) if measured_at else row["submitted_at"]
            post["likes"] = likes
            post.pop("replies", None)           # v3 ignores them in scoring;
            post.pop("reposts", None)           #   the stored snapshot still
            post.pop("quotes", None)            #   carries the untouched
            post.pop("impressions", None)       #   audit evidence
            ctx = {}
        points, audit = score_post(post, self.scoring, ctx)
        audit["rescored_from"] = {
            "points": prev_points, "status": row["like_status"],
            "scoring_version": (prev_score.get("scoring_version")
                                or "unversioned")}
        if action == "verify":
            audit["provider"] = "manual:" + (method or "manual")
        new_score_json = json.dumps(audit)

        # the two statements move together or not at all: a database failure
        # after the UPDATE must not strand a rescore without its audit event.
        try:
            self.db.conn.execute(
                "UPDATE submissions SET points=?, score_json=?, like_status=?,"
                " official_likes=?, like_measured_at=?, like_source=?"
                " WHERE id=?",
                (points, new_score_json, like_status, official, measured,
                 method or ("manual" if action == "verify" else None),
                 submission_id))
            self.db.conn.execute(
                "INSERT INTO admin_events(submission_id,admin,action,prev_count,"
                "new_count,prev_points,new_points,measured_at,acted_at,reason,"
                "method,scoring_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (submission_id, admin_email, action, prev_count, official,
                 prev_points, points, measured, t,
                 (reason or "")[:200] or ("manual dispute" if action == "dispute"
                                          else "manual verification"),
                 method or ("manual verification" if action == "verify" else "manual dispute"),
                 self.scoring.get("version", "unversioned")))
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return dict(ok=True, submission_id=submission_id, action=action,
                    points=points, like_status=like_status,
                    official_likes=official, measured_at=measured,
                    audit=audit)

    def admin_queue(self, competition_id, status="pending"):
        """the admin review queue — current rows plus each row's event
        history. status filter: pending | verified | disputed | corrected
        ('corrected' = a verified row whose history shows a later count that
        actually MOVED the number — a double-click re-verify is history,
        not a correction)."""
        where = {"pending": "s.like_status = 'pending'",
                 "verified": "s.like_status = 'verified'",
                 "disputed": "s.like_status = 'disputed'",
                 "corrected": ("s.like_status = 'verified' AND EXISTS"
                               "(SELECT 1 FROM admin_events e"
                               " WHERE e.submission_id = s.id"
                               " AND e.action = 'verify'"
                               " AND e.prev_count IS NOT NULL"
                               " AND e.prev_count <> e.new_count)")}[status]
        rows = self.db.conn.execute(
            "SELECT s.id, s.account_id, s.x_post_id, s.url, s.submitted_at, s.points,"
            " s.official_likes, s.like_status, s.like_measured_at, s.like_source,"
            " COALESCE(a.username, a.email) AS submitter, a.email AS submitter_email"
            " FROM submissions s JOIN accounts a ON a.id = s.account_id"
            " WHERE s.competition_id = ? AND s.eligible = 1 AND " + where +
            " ORDER BY s.id", (competition_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            events = self.db.conn.execute(
                "SELECT admin, action, prev_count, new_count, prev_points,"
                " new_points, measured_at, acted_at, reason, method,"
                " scoring_version FROM admin_events WHERE submission_id = ?"
                " ORDER BY id", (r["id"],)).fetchall()
            d["events"] = [dict(e) for e in events]
            d["base"] = float(self.scoring.get("basePerPost", 0))
            d["like_bonus"] = round(max(0.0, (r["points"] or 0.0) - d["base"]), 2)
            out.append(d)
        return out

    # --------------------------------------------------------------- dashboard
    def dashboard(self, account, competition_id=None):
        """Everything the account's own page prints. Every row is keyed by
        the account id resolved from the session — never by an id the
        client could have typed (no IDOR through this door)."""
        aid = account["id"]
        agg = self.db.conn.execute(
            "SELECT COUNT(*) AS submitted,"
            " SUM(CASE WHEN eligible THEN 1 ELSE 0 END) AS eligible_posts,"
            " COALESCE(SUM(points),0) AS points"
            " FROM submissions WHERE account_id=? AND competition_id=?",
            (aid, competition_id)).fetchone()
        grouped = [dict(user_id=r["account_id"], points=r["pts"])
                   for r in self.db.conn.execute(
                       "SELECT account_id, SUM(points) AS pts FROM submissions"
                       " WHERE competition_id=? GROUP BY account_id",
                       (competition_id,))]
        ranked = sorted(grouped, key=lambda r: (-r["points"], r["user_id"]))
        my_rank, prev_pts, rank = None, None, 0
        for i, r in enumerate(ranked):
            if r["points"] != prev_pts:
                rank = i + 1
                prev_pts = r["points"]
            if r["user_id"] == aid:
                my_rank = rank
        est = estimate(ranked, self.prize).get(aid, dict(share_pct=0.0, share_est=0.0))
        base = float(self.scoring.get("basePerPost", 0))
        recent = [dict(x_post_id=r["x_post_id"], author=r["author_handle"],
                       points=r["points"], eligible=bool(r["eligible"]),
                       reason=r["reason"], submitted_at=r["submitted_at"],
                       like_status=r["like_status"],
                       like_bonus=(round(max(0.0, (r["points"] or 0.0) - base), 2)
                                   if r["eligible"] else None))
                  for r in self.db.conn.execute(
                      "SELECT x_post_id,author_handle,points,eligible,reason,"
                      "submitted_at,like_status FROM submissions"
                      " WHERE account_id=? AND competition_id=?"
                      " ORDER BY submitted_at DESC LIMIT 5", (aid, competition_id))]
        rewards = [dict(r) for r in self.db.conn.execute(
            "SELECT competition_id,wallet_address,amount,asset,status,created_at,"
            "paid_at,reference FROM rewards WHERE account_id=?"
            " ORDER BY created_at DESC", (aid,))]
        audits = [dict(r) for r in self.db.conn.execute(
            "SELECT old_wallet,new_wallet,changed_at,effective_at FROM wallet_audit"
            " WHERE account_id=? ORDER BY changed_at DESC LIMIT 3", (aid,))]
        return dict(
            account=dict(id=aid, email=account["email"],
                         email_verified=bool(account["email_verified"]),
                         username=account["username"], status=account["status"],
                         wallet=account["wallet"],
                         wallet_effective_at=account["wallet_effective_at"],
                         onboarded=account["onboarded_at"] is not None,
                         created_at=account["created_at"],
                         last_login_at=account["last_login_at"]),
            points=agg["points"] or 0.0,
            submitted=agg["submitted"] or 0,
            verified_posts=agg["submitted"] or 0,
            eligible_posts=agg["eligible_posts"] or 0,
            rank=my_rank, share_pct=est["share_pct"], share_est=est["share_est"],
            recent=recent, rewards=rewards, wallet_audit=audits,
            competition_state=(self.competition_state(competition_id)
                               if competition_id else "none"))

    def submissions_view(self, account, competition_id):
        """The account's own ledger of pasted posts — every row selected by
        the account id (authorization is the WHERE clause; there is no route
        where a client-supplied id gets trusted)."""
        out = []
        for r in self.db.conn.execute(
                "SELECT * FROM submissions WHERE account_id=? AND competition_id=?"
                " ORDER BY submitted_at DESC", (account["id"], competition_id)):
            audit = json.loads(r["score_json"]) if r["score_json"] else {}
            out.append(dict(
                id=r["id"], x_post_id=r["x_post_id"], author=r["author_handle"],
                url=r["url"], text=r["text"], posted_at=r["posted_at"],
                submitted_at=r["submitted_at"], verified=bool(r["verified"]),
                eligible=bool(r["eligible"]), reason=r["reason"],
                points=r["points"], metrics=json.loads(r["metrics_json"] or "{}"),
                provided=audit.get("provided", []), missing=audit.get("missing", []),
                matched=audit.get("matched"), follow_gate=audit.get("follow"),
                audit=dict(contributions=audit.get("contributions"),
                           applied=audit.get("applied", []),
                           version=audit.get("scoring_version"))))
        return out

    # ------------------------------------------------------------------ scan
    def scan_user(self, user, competition_id, at=None, force=False):
        """Server-side discovery + scoring. Guards first (P4): cooldown,
        in-flight, concurrency, budget. `at` pins the clock for tests."""
        t = now() if at is None else float(at)
        uid = user["x_user_id"]
        sc = self.scan_cfg
        last = user.get("last_scan_at")
        cool = float(sc.get("cooldownMinutes", 360))
        if not force and last is not None and t - last < cool * 60:
            return dict(ok=False, reason="scan_cooldown", found=0, qualified=0,
                        retry_after=int(last + cool * 60 - t))
        with self._scan_lock:
            if uid in self._inflight:
                return dict(ok=False, reason="scan_inflight", found=0, qualified=0)
            if self._active_scans >= int(sc.get("maxConcurrent", 8)):
                return dict(ok=False, reason="scan_busy", found=0, qualified=0)
            self._inflight.add(uid)
            self._active_scans += 1
        try:
            return self._scan_body(user, competition_id, t)
        finally:
            with self._scan_lock:
                self._inflight.discard(uid)
                self._active_scans -= 1

    def _scan_body(self, user, competition_id, t):
        """The guarded body: follow gate (P2) → delta pages (P1) → scoring
        (unchanged pipeline) → metric sweep (P3). X is touched at most once
        per stage; everything else is SQL."""
        uid = user["x_user_id"]
        blocked = self._budget_blocked()
        if blocked:
            return dict(ok=False, reason=blocked, found=0, qualified=0)
        before = self._usage_today()

        # ---- P2: follow gate, TTL-cached via follows_checked_at ----
        cache_min = float(self.scan_cfg.get("followCacheHours", 24)) * 60
        checked_at = user.get("follows_checked_at")
        if checked_at is not None and t - checked_at < cache_min * 60:
            follows = bool(user["follows_paper"])       # cached + or -
            self.record_x(dict(kind="followings", resources=0, requests=0,
                               checked=True, checked_at=checked_at,
                               label="follow-cache hit"))
        else:
            try:
                follows = bool(self.x.follows_paper(uid))
            except XError:
                return dict(ok=False, reason="x_api_unavailable", found=0, qualified=0)
            self.record_x(dict(kind="followings", resources=1 if follows else 0,
                               checked=True, checked_at=checked_at,
                               label="follow-check"))
            self.db.conn.execute(
                "UPDATE users SET follows_paper=?, follows_checked_at=?, updated_at=?"
                " WHERE id=?", (int(follows), t, t, user["id"]))
            if follows:
                # first observed follow time anchors the "from_follow_date" effect
                row = self.db.conn.execute(
                    "SELECT follows_since FROM users WHERE id=?", (user["id"],)).fetchone()
                if row and row["follows_since"] is None:
                    self.db.conn.execute("UPDATE users SET follows_since=? WHERE id=?",
                                         (t, user["id"]))

        # ---- P1: delta discovery — server-side start_time, never the full page ----
        comp = self.active_competition()
        since = _iso(comp["starts_at"]) if comp else t - 30 * 86400
        wm = user.get("scan_watermark")
        first_scan = wm is None
        overlap = float(self.scan_cfg.get("overlapSeconds", 300))
        start_time = None if first_scan else max(since, wm - overlap)
        max_pages = 1 if first_scan else max(1, int(self.scan_cfg.get("deltaMaxPages", 3)))

        pages, token, truncated = [], None, False
        try:
            for _ in range(max_pages):
                page, meta = self.x.recent_posts(uid, since, start_time=start_time,
                                                 token=token, max_results=50)
                pages.append((page, meta))
                token = meta.get("next_token")
                if not meta.get("more") or not token:
                    break
            else:
                truncated = bool(pages and pages[-1][1].get("more") and token)
        except XError:
            return dict(ok=False, reason="x_api_unavailable", found=0, qualified=0)
        if truncated:
            print(f"[scan] {uid}: delta capped at {max_pages} pages — watermark "
                  "rewinds to the oldest observed post (logged, not silent)")
        self.record_x(dict(kind="posts", label="delta" if not first_scan else "first-page",
                           resources=sum(m.get("returned", 0) for _, m in pages)))

        # overlap windows repeat boundary posts: dedup by post id (P1)
        seen_ids, posts = set(), []
        for page, _meta in pages:
            for p in page:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    posts.append(p)
        created = [p["created_at"] for p in posts if p.get("created_at")]

        known = {r["x_post_id"] for r in self.db.conn.execute(
            "SELECT x_post_id FROM posts WHERE x_author_id=? AND competition_id=?",
            (uid, competition_id))}
        discovered = sum(1 for p in posts if p["id"] not in known)

        # newest-first window context for duplicates + frequency caps
        posts_sorted = sorted(posts, key=lambda p: p["created_at"], reverse=True)
        seen = set()
        window_rows = []
        for idx, p in enumerate(posts_sorted):
            metrics = dict(
                impressions=(p.get("non_public_metrics") or {}).get("impression_count", 0),
                likes=(p.get("public_metrics") or {}).get("like_count", 0),
                replies=(p.get("public_metrics") or {}).get("reply_count", 0),
                reposts=(p.get("public_metrics") or {}).get("retweet_count", 0),
                quotes=(p.get("public_metrics") or {}).get("quote_count", 0),
            )
            eligible, reason, matched = self.elig.verdict(p["text"], follows)
            if eligible and self.elig.effect == "from_follow_date":
                since_follow = self.db.conn.execute(
                    "SELECT follows_since FROM users WHERE id=?",
                    (user["id"],)).fetchone()["follows_since"]
                if since_follow and p["created_at"] < since_follow:
                    eligible, reason = False, "follow_too_late"
            ctx = {}
            if eligible:
                key = normalize_text(p["text"])
                if key in seen:
                    ctx["duplicate_of"] = p["id"]
                else:
                    seen.add(key)
                if idx >= self.scoring["maxScoredPostsPerWindow"]:
                    ctx["over_frequency"] = True
            points, audit = score_post(metrics, self.scoring, ctx)
            audit["matched"] = matched          # which identifier cleared the gate
            audit["followed"] = int(follows)    # the gate itself, per-post
            window_rows.append((p, metrics, eligible, reason, matched, points, audit, ctx))

        for p, metrics, eligible, reason, matched, points, audit, ctx in window_rows:
            self.db.conn.execute("""
                INSERT INTO posts(x_post_id,x_author_id,competition_id,text,url,posted_at,
                                  impressions,likes,replies,reposts,quotes,
                                  eligible,reason,duplicate_of,scored_at,score_json,points,
                                  metrics_refreshed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(x_post_id) DO UPDATE SET
                  text=excluded.text, url=excluded.url, posted_at=excluded.posted_at,
                  impressions=excluded.impressions, likes=excluded.likes,
                  replies=excluded.replies, reposts=excluded.reposts, quotes=excluded.quotes,
                  eligible=excluded.eligible, reason=excluded.reason,
                  duplicate_of=excluded.duplicate_of, scored_at=excluded.scored_at,
                  score_json=excluded.score_json, points=excluded.points,
                  metrics_refreshed_at=excluded.metrics_refreshed_at
            """, (p["id"], uid, competition_id, p["text"], p["url"],
                  _iso(p["created_at"]), metrics["impressions"], metrics["likes"],
                  metrics["replies"], metrics["reposts"], metrics["quotes"],
                  int(eligible), reason,
                  ctx.get("duplicate_of"),
                  _iso(t), json.dumps(audit), points, t))
        self.db.conn.execute(
            "INSERT INTO scans(x_user_id,scanned_at,found,qualified,kind) VALUES(?,?,?,?,?)",
            (uid, t, len(window_rows), sum(1 for r in window_rows if r[2]),
             "first" if first_scan else "delta"))

        # ---- watermark: server clock, overlap-biased; never client-provided ----
        if truncated and created:
            wm_new = min(created)
        else:
            wm_new = max(t, max(created, default=t))
        self.db.conn.execute(
            "UPDATE users SET scan_watermark=?, last_scan_at=?, updated_at=?"
            " WHERE id=?", (wm_new, t, t, user["id"]))
        self.db.conn.commit()

        # ---- P3: age-tiered metric sweep over known eligible posts ----
        refreshed, frozen = 0, 0
        try:
            refreshed, frozen = self._metrics_sweep(user, competition_id, follows, t)
        except XError:
            pass                                  # graceful: discovered posts stand
        if refreshed or frozen:
            self.db.conn.commit()

        after = self._usage_today()
        res_delta = after["resources"] - before["resources"]
        est_delta = after["estimated"] - before["estimated"]
        print(f"[scan {self.settings.mock and 'mock' or 'live'}] {uid} "
              f"{'first' if first_scan else 'delta'} found={len(window_rows)} "
              f"discovered={discovered} refreshed={refreshed} frozen={frozen} "
              f"resources={res_delta} est=${est_delta:.4f}")
        self.record_x(dict(kind="scan", resources=0, requests=0, label=None))
        return dict(ok=True, found=len(window_rows),
                    qualified=sum(1 for r in window_rows if r[2] and r[5] > 0),
                    follows_paper=follows, first_scan=first_scan, pages=len(pages),
                    truncated=truncated, discovered=discovered, refreshed=refreshed,
                    resources=res_delta, estimated=round(est_delta, 5))

    # ----------------------------------------------------------------- usage
    def record_x(self, ev):
        """Bookkeeping for one X interaction (or cache hit). The meter is the
        single place that turns resources into money: rates are config."""
        n = int(ev.get("resources") or 0)
        kind = ev.get("kind")
        rate = {"posts": self.costs.get("postsRead", 0.005),
                "users": self.costs.get("usersRead", 0.010),
                "followings": self.costs.get("followingsRead", 0.010)}.get(kind)
        est = n * (rate or 0.0)
        reqs = int(ev.get("requests", 1))
        p_n = n if kind == "posts" else 0
        u_n = n if kind == "users" else 0
        f_n = n if kind == "followings" else 0
        scans = 1 if kind == "scan" else 0
        checks = 1 if (kind == "followings" and reqs == 1) else 0
        hits = 1 if (kind == "followings" and reqs == 0) else 0
        self.db.conn.execute(
            "INSERT INTO x_usage(day,requests,post_resources,user_resources,"
            " following_resources,estimated,scans,follow_checks,follow_cache_hits)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET"
            " requests=requests+excluded.requests,"
            " post_resources=post_resources+excluded.post_resources,"
            " user_resources=user_resources+excluded.user_resources,"
            " following_resources=following_resources+excluded.following_resources,"
            " estimated=estimated+excluded.estimated, scans=scans+excluded.scans,"
            " follow_checks=follow_checks+excluded.follow_checks,"
            " follow_cache_hits=follow_cache_hits+excluded.follow_cache_hits",
            (_date.today().isoformat(), reqs, p_n, u_n, f_n, est, scans, checks, hits))
        self.db.conn.commit()
        if ev.get("label") and kind != "scan":
            print(f"[xapi {self.settings.mock and 'mock' or 'live'}] {ev['label']} "
                  f"resources={n} est=${est:.4f}")

    def _usage_today(self):
        r = self.db.conn.execute("SELECT * FROM x_usage WHERE day=?",
                                 (_date.today().isoformat(),)).fetchone()
        if not r:
            return dict(resources=0, estimated=0.0)
        return dict(resources=(r["post_resources"] + r["user_resources"]
                               + r["following_resources"]),
                    estimated=r["estimated"])

    def _budget_blocked(self):
        """Daily resource wall + monthly money wall (P4). Returns a reason
        string or None. A blocked scan is graceful, never an error."""
        row = self._usage_today()
        if self.budget_cfg.get("dailyXResources") and \
                row["resources"] >= self.budget_cfg["dailyXResources"]:
            return "scan_budget_exhausted"
        if self.budget_cfg.get("monthlyEstimatedUsd"):
            month = self.db.conn.execute(
                "SELECT COALESCE(SUM(estimated),0) AS e FROM x_usage WHERE day >= ?",
                (_date.today().isoformat()[:7] + "-01",)).fetchone()["e"]
            if month >= self.budget_cfg["monthlyEstimatedUsd"]:
                return "scan_budget_exhausted"
        return None

    def usage_view(self):
        """P6 cost observability. Numbers only — never credentials."""
        today = _date.today().isoformat()
        r = self.db.conn.execute("SELECT * FROM x_usage WHERE day=?",
                                 (today,)).fetchone()
        r = dict(r) if r else dict(requests=0, post_resources=0, user_resources=0,
                                   following_resources=0, estimated=0.0, scans=0,
                                   follow_checks=0, follow_cache_hits=0)
        res = r["post_resources"] + r["user_resources"] + r["following_resources"]
        checks = r["follow_checks"] + r["follow_cache_hits"]
        month = self.db.conn.execute(
            "SELECT COALESCE(SUM(estimated),0) AS est, COALESCE(SUM(requests),0) AS req"
            " FROM x_usage WHERE day >= ?", (today[:7] + "-01",)).fetchone()
        scans_today = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM scans WHERE scanned_at >= ?",
            (_dt.combine(_date.today(), _time.min).timestamp(),)).fetchone()["n"]
        users_scanned = self.db.conn.execute(
            "SELECT COUNT(DISTINCT x_user_id) AS n FROM scans").fetchone()["n"]
        return dict(
            mode="mock" if self.settings.mock else "live",
            requests_today=r["requests"],
            post_resources_today=r["post_resources"],
            user_resources_today=r["user_resources"],
            following_resources_today=r["following_resources"],
            resources_today=res, estimated_today=round(r["estimated"], 5),
            requests_month=month["req"], estimated_month=round(month["est"], 5),
            scans_recorded=r["scans"], scans_today=scans_today,
            avg_resources_per_scan=(round(res / scans_today, 2) if scans_today else 0),
            avg_cost_per_scan=(round(r["estimated"] / scans_today, 6)
                               if scans_today else 0),
            follow_checks_today=r["follow_checks"],
            follow_cache_hits_today=r["follow_cache_hits"],
            follow_cache_hit_rate=(round(r["follow_cache_hits"] / checks, 3)
                                   if checks else None),
            users_scanned=users_scanned)

    # --------------------------------------------------------- metric policy
    def _metrics_sweep(self, user, competition_id, follows, t):
        """P3: known eligible posts age through three tiers — recent posts
        refresh often, older scored posts rarely, ancient posts freeze. A
        ended competition gets one final authoritative snapshot, then freezes.
        Only this and delta discovery touch X; the leaderboard itself never
        does (refresh_standings is pure SQL)."""
        mc = self.metrics_cfg
        if not mc:
            return 0, 0
        uid = user["x_user_id"]
        state = self.competition_state(competition_id)
        final = state in ("ended", "closed") and mc.get("finalSnapshot", True)
        if not (final or mc.get("sweepOnScan", True)):
            return 0, 0
        recent_cut = t - float(mc.get("recentMinutes", 120)) * 60
        aged_cut = t - float(mc.get("agedMinutes", 720)) * 60
        recent_win = t - float(mc.get("recentHours", 48)) * 3600
        freeze_cut = t - float(mc.get("freezeDays", 30)) * 86400
        cap = int(mc.get("refreshPerScanCap", 100))

        stale = []
        for r in self.db.conn.execute(
                "SELECT x_post_id, posted_at, points, metrics_refreshed_at FROM posts"
                " WHERE x_author_id=? AND competition_id=? AND eligible=1"
                " AND metrics_frozen=0", (uid, competition_id)):
            if r["posted_at"] < freeze_cut and not final:
                continue                            # ancient: frozen, not stale
            if final:
                stale.append(r)
            elif (r["posted_at"] >= recent_win
                  and (r["metrics_refreshed_at"] or 0) < recent_cut):
                stale.append(r)                     # recent: frequent tier
            elif (r["posted_at"] < recent_win and r["points"] > 0
                  and (r["metrics_refreshed_at"] or 0) < aged_cut):
                stale.append(r)                     # older: only if it scores
            if len(stale) >= cap:
                break

        frozen = self.db.conn.execute(
            "UPDATE posts SET metrics_frozen=1 WHERE x_author_id=? AND competition_id=?"
            " AND metrics_frozen=0 AND eligible=1 AND posted_at < ?",
            (uid, competition_id, freeze_cut)).rowcount

        refreshed = 0
        if stale:
            fresh, got = {}, 0
            ids = [r["x_post_id"] for r in stale]
            for i in range(0, len(ids), 50):        # same 50/request cap, chunked
                rows, meta = self.x.metrics_for(ids[i:i + 50])
                got += meta.get("returned", len(rows))
                fresh.update({p["id"]: p for p in rows})
            self.record_x(dict(kind="posts", resources=got, label="metrics"))
            rank_pos = {r["x_post_id"]: i for i, r in enumerate(self.db.conn.execute(
                "SELECT x_post_id FROM posts WHERE x_author_id=? AND competition_id=?"
                " AND eligible=1 ORDER BY posted_at DESC", (uid, competition_id)))}
            for pid, p in fresh.items():
                pm = p.get("public_metrics") or {}
                npm = p.get("non_public_metrics") or {}
                metrics = dict(impressions=npm.get("impression_count", 0),
                               likes=pm.get("like_count", 0),
                               replies=pm.get("reply_count", 0),
                               reposts=pm.get("retweet_count", 0),
                               quotes=pm.get("quote_count", 0))
                eligible, reason, matched = self.elig.verdict(p["text"], follows)
                if eligible and self.elig.effect == "from_follow_date":
                    sf = self.db.conn.execute(
                        "SELECT follows_since FROM users WHERE id=?",
                        (user["id"],)).fetchone()["follows_since"]
                    if sf and p["created_at"] < sf:
                        eligible, reason = False, "follow_too_late"
                ctx = {}
                dup = self.db.conn.execute(
                    "SELECT duplicate_of FROM posts WHERE x_post_id=?", (pid,)).fetchone()
                if dup and dup["duplicate_of"]:
                    ctx["duplicate_of"] = dup["duplicate_of"]
                if eligible and rank_pos.get(pid, 0) >= \
                        self.scoring["maxScoredPostsPerWindow"]:
                    ctx["over_frequency"] = True
                points, audit = score_post(metrics, self.scoring, ctx)
                audit["matched"] = matched
                audit["followed"] = int(follows)
                self.db.conn.execute(
                    "UPDATE posts SET text=?, url=?, posted_at=?, impressions=?, likes=?"
                    ", replies=?, reposts=?, quotes=?, eligible=?, reason=?, points=?"
                    ", scored_at=?, score_json=?, metrics_refreshed_at=?"
                    " WHERE x_post_id=?",
                    (p["text"], p["url"], p["created_at"], metrics["impressions"],
                     metrics["likes"], metrics["replies"], metrics["reposts"],
                     metrics["quotes"], int(eligible), reason, points,
                     _iso(t), json.dumps(audit), t, pid))
                refreshed += 1
        if final:
            frozen += self.db.conn.execute(
                "UPDATE posts SET metrics_frozen=1 WHERE x_author_id=? AND"
                " competition_id=? AND eligible=1 AND metrics_frozen=0",
                (uid, competition_id)).rowcount
        return refreshed, frozen

    # ------------------------------------------------------------------ standings
    def refresh_standings(self, competition_id, movement_from_prev=True):
        """Recompute per-user points from stored posts, rank, snapshot."""
        rows = self.db.conn.execute("""
            SELECT u.id AS user_id, u.x_username, u.display_name, u.avatar_url,
                   COALESCE(SUM(p.points),0)        AS points,
                   SUM(CASE WHEN p.eligible AND p.points>0 THEN 1 ELSE 0 END) AS posts_count,
                   COALESCE(SUM(p.likes+p.replies+p.reposts+p.quotes),0) AS engagement
            FROM posts p
            JOIN users u ON u.x_user_id = p.x_author_id
            WHERE p.competition_id=? AND p.eligible=1
            GROUP BY u.id
        """, (competition_id,)).fetchall()

        ranked = sorted(rows, key=lambda r: (-r["points"], r["user_id"]))
        total = sum(max(0, r["points"]) for r in ranked)
        shares = estimate(
            [dict(user_id=r["user_id"], points=r["points"]) for r in ranked],
            self.prize)

        t = now()
        prev = self.db.conn.execute(
            "SELECT id FROM snapshots WHERE competition_id=? ORDER BY id DESC LIMIT 1",
            (competition_id,)).fetchone()
        prev_ranks = {}
        if prev and movement_from_prev:
            for r in self.db.conn.execute(
                    "SELECT user_id, rank FROM snapshot_rows WHERE snapshot_id=?",
                    (prev["id"],)):
                prev_ranks[r["user_id"]] = r["rank"]

        cur = self.db.conn.execute(
            "INSERT INTO snapshots(competition_id,period,generated_at,total_points,"
            "participants,scoring_version) VALUES(?,?,?,?,?,?)",
            (competition_id, t, t, total, len(ranked),
             self.settings.pb["scoring"]["version"]))
        snap_id = cur.lastrowid
        rank = 0
        prev_pts = None
        for i, r in enumerate(ranked):
            if r["points"] != prev_pts:
                rank = i + 1
                prev_pts = r["points"]
            est = shares.get(r["user_id"], dict(share_pct=0, share_est=0))
            movement = None
            if r["user_id"] in prev_ranks and prev_ranks[r["user_id"]] is not None:
                movement = prev_ranks[r["user_id"]] - rank
            self.db.conn.execute(
                "INSERT INTO snapshot_rows(snapshot_id,user_id,rank,points,posts_count,"
                "engagement,share_pct,share_est,tier,movement) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (snap_id, r["user_id"], rank, r["points"], r["posts_count"] or 0,
                 r["engagement"] or 0, est["share_pct"], est["share_est"],
                 tier_for(rank, self.tiers), movement))
        self.db.conn.commit()
        return snap_id

    def leaderboard(self, competition_id):
        """The public board in account-era clothes: ranks come from the live
        submission ledger, and the points are the frozen per-row totals —
        scored at submit time, nothing here recomputes them. Legacy scans
        (posts/users) ride their own tables and cannot vote here: only real
        submitters appear, ties share the rank exactly as the dashboard
        already promises, engagement reports only what a provider truly
        reported (a presence row adds nothing — no invented zero), and the
        share is an estimate: the ledger moves money, not the scheduler."""
        t = now()
        base_each = float(self.scoring.get("basePerPost", 0))
        grouped = self.db.conn.execute(
            "SELECT s.account_id AS user_id,"
            " COALESCE(a.username, a.email) AS handle,"
            " SUM(s.points) AS points,"
            " SUM(CASE WHEN s.eligible THEN 1 ELSE 0 END) AS posts_count,"
            " SUM(CASE WHEN s.eligible AND s.like_status='pending'"
            "     THEN 1 ELSE 0 END) AS pending"
            " FROM submissions s JOIN accounts a ON a.id = s.account_id"
            " WHERE s.competition_id=? GROUP BY s.account_id"
            " ORDER BY points DESC, s.account_id", (competition_id,)).fetchall()
        if not grouped:
            return dict(snapshot_id=None, generated_at=t, total_points=0.0,
                        participants=0, next_refresh_at=t + self.refresh_seconds,
                        rows=[])
        engaged = {}
        for r in self.db.conn.execute(
                "SELECT account_id, metrics_json FROM submissions"
                " WHERE competition_id=?", (competition_id,)).fetchall():
            m = json.loads(r["metrics_json"]) or {}
            engaged[r["account_id"]] = (engaged.get(r["account_id"], 0)
                                        + sum(m.get(k) or 0 for k in
                                              ("likes", "replies", "reposts",
                                               "quotes")))
        shares = estimate([dict(user_id=r["user_id"], points=r["points"])
                            for r in grouped], self.prize)
        total = sum(max(0.0, r["points"]) for r in grouped)
        rows, prev_pts, rank = [], None, 0
        for i, r in enumerate(grouped):        # SQL ordered; rank mirrors dashboard
            if r["points"] != prev_pts:
                rank = i + 1
                prev_pts = r["points"]
            est = shares.get(r["user_id"], dict(share_pct=0.0, share_est=0.0))
            rows.append(dict(
                rank=rank, tier=tier_for(rank, self.tiers), movement=None,
                user=dict(id=r["user_id"], handle=r["handle"], name=r["handle"],
                          avatar=None),
                points=r["points"], posts=r["posts_count"],
                pending=r["pending"] or 0,
                base_points=round(base_each * (r["posts_count"] or 0), 2),
                bonus_points=round(max(0.0, (r["points"] or 0.0))
                                   - base_each * (r["posts_count"] or 0), 2),
                engagement=engaged.get(r["user_id"], 0),
                share_pct=est["share_pct"], share_est=est["share_est"]))
        return dict(snapshot_id=None, generated_at=t, total_points=total,
                    participants=len(grouped),
                    next_refresh_at=t + self.refresh_seconds, rows=rows)

    # ------------------------------------------------------------------ me
    def me_view(self, user, competition_id):
        agg = self.db.conn.execute("""
            SELECT COALESCE(SUM(points),0) AS points,
                   SUM(CASE WHEN eligible AND points>0 THEN 1 ELSE 0 END) AS posts,
                   COALESCE(SUM(likes+replies+reposts+quotes),0) AS engagement
            FROM posts WHERE x_author_id=? AND competition_id=? AND eligible=1
        """, (user["x_user_id"], competition_id)).fetchone()
        pos = self.db.conn.execute(
            "SELECT rank, points, posts_count, share_pct, share_est, movement "
            "FROM snapshot_rows s JOIN snapshots sn ON sn.id=s.snapshot_id "
            "WHERE s.user_id=? AND sn.competition_id=? "
            "ORDER BY sn.id DESC LIMIT 1", (user["id"], competition_id)).fetchone()
        status = self.competition_state(competition_id)
        return dict(
            connected=True, handle=user["x_username"], name=user["display_name"],
            wallet=user.get("wallet"), points=agg["points"] or 0.0,
            qualifying_posts=agg["posts"] or 0, engagement=agg["engagement"] or 0,
            rank=(pos["rank"] if pos else None),
            share_pct=(pos["share_pct"] if pos else 0.0),
            share_est=(pos["share_est"] if pos else 0.0),
            follows_paper=bool(user["follows_paper"]),
            scanned=bool(self.db.conn.execute(
                "SELECT 1 FROM scans WHERE x_user_id=? LIMIT 1",
                (user["x_user_id"],)).fetchone()),
            competition_state=status,
        )

    def posts_view(self, user, competition_id):
        out = []
        for r in self.db.conn.execute(
                "SELECT * FROM posts WHERE x_author_id=? AND competition_id=?"
                " ORDER BY posted_at DESC", (user["x_user_id"], competition_id)):
            audit = json.loads(r["score_json"]) if r["score_json"] else {}
            out.append(dict(
                id=r["x_post_id"], text=r["text"], url=r["url"], posted_at=r["posted_at"],
                metrics=dict(impressions=r["impressions"], likes=r["likes"],
                             replies=r["replies"], reposts=r["reposts"], quotes=r["quotes"]),
                eligible=bool(r["eligible"]), reason=r["reason"], points=r["points"],
                matched=audit.get("matched"), followed=bool(audit.get("followed")),
                audit=dict(contributions=audit.get("contributions"),
                           applied=audit.get("applied", []),
                           version=audit.get("scoring_version")),
            ))
        return out

    def competition_state(self, competition_id):
        c = self.db.conn.execute(
            "SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
        if not c:
            return "unknown"
        t = now()
        start = _iso(c["starts_at"]); end = _iso(c["ends_at"])
        if c["status"] != "active":
            return "closed"
        if t < start:
            return "upcoming"
        if t > end:
            return "ended"
        return "live"


def _iso(val):
    """epoch seconds <-> ISO strings are both accepted in config; normalize to epoch."""
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)                      # REAL stored in a TEXT column
    except ValueError:
        from datetime import datetime
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
