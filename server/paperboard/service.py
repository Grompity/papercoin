"""Service layer — the only code that touches both the DB and X.

Everything the frontend sees comes out of here already computed, so the
client never has to (and never gets to) decide what a score means.
"""

import json
import threading
import time
from datetime import date as _date, datetime as _dt, time as _time

from .eligibility import Eligibility, normalize_wallet
from .prizes import estimate, tier_for
from .scoring import normalize_text, score_post
from .xapi import make_pkce, XError


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
        self.prize = cfg["prize"]
        self.tiers = cfg["tiers"]
        self.refresh_seconds = cfg["refreshMinutes"] * 60
        self.scan_cfg = dict(cfg.get("scan", {}))
        self.metrics_cfg = dict(cfg.get("metrics", {}))
        self.budget_cfg = dict(cfg.get("budget", {}))
        self.costs = dict(cfg.get("apiCosts", {}))
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
        if not token:
            return None
        row = self.db.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        return dict(row) if row else None

    def new_session(self, user_id):
        import secrets
        token = secrets.token_hex(24)
        self.db.conn.execute("INSERT INTO sessions VALUES(?,?,?)",
                             (token, user_id, now()))
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
        snap = self.db.conn.execute(
            "SELECT * FROM snapshots WHERE competition_id=? ORDER BY id DESC LIMIT 1",
            (competition_id,)).fetchone()
        if not snap:
            return None
        rows = self.db.conn.execute("""
            SELECT s.*, u.x_username, u.display_name, u.avatar_url, u.wallet
            FROM snapshot_rows s JOIN users u ON u.id=s.user_id
            WHERE s.snapshot_id=? ORDER BY s.rank, s.user_id
        """, (snap["id"],)).fetchall()
        out_rows = []
        for r in rows:
            out_rows.append(dict(
                rank=r["rank"], tier=r["tier"], movement=r["movement"],
                user=dict(handle=r["x_username"], name=r["display_name"],
                          avatar=r["avatar_url"]),
                points=r["points"], posts=r["posts_count"], engagement=r["engagement"],
                share_pct=r["share_pct"], share_est=r["share_est"],
            ))
        return dict(
            snapshot_id=snap["id"], generated_at=snap["generated_at"],
            total_points=snap["total_points"], participants=snap["participants"],
            next_refresh_at=snap["generated_at"] + self.refresh_seconds,
            rows=out_rows,
        )

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
