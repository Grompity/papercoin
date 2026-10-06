"""Service layer — the only code that touches both the DB and X.

Everything the frontend sees comes out of here already computed, so the
client never has to (and never gets to) decide what a score means.
"""

import json
import time

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
    def scan_user(self, user, competition_id):
        """Server-side discovery + scoring of the user's recent posts."""
        uid = user["x_user_id"]
        try:
            follows = bool(self.x.follows_paper(uid))
        except XError:
            return dict(ok=False, reason="x_api_unavailable", found=0, qualified=0)

        t = now()
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

        comp = self.active_competition()
        since = _iso(comp["starts_at"]) if comp else now() - 30 * 86400
        try:
            posts = self.x.recent_posts(uid, since)
        except XError:
            return dict(ok=False, reason="x_api_unavailable", found=0, qualified=0)

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
                                  eligible,reason,duplicate_of,scored_at,score_json,points)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(x_post_id) DO UPDATE SET
                  text=excluded.text, url=excluded.url, posted_at=excluded.posted_at,
                  impressions=excluded.impressions, likes=excluded.likes,
                  replies=excluded.replies, reposts=excluded.reposts, quotes=excluded.quotes,
                  eligible=excluded.eligible, reason=excluded.reason,
                  duplicate_of=excluded.duplicate_of, scored_at=excluded.scored_at,
                  score_json=excluded.score_json, points=excluded.points
            """, (p["id"], uid, competition_id, p["text"], p["url"],
                  _iso(p["created_at"]), metrics["impressions"], metrics["likes"],
                  metrics["replies"], metrics["reposts"], metrics["quotes"],
                  int(eligible), reason,
                  ctx.get("duplicate_of"),
                  _iso(now()), json.dumps(audit), points))
        self.db.conn.execute(
            "INSERT INTO scans(x_user_id,scanned_at,found,qualified) VALUES(?,?,?,?)",
            (uid, now(), len(window_rows), sum(1 for r in window_rows if r[2])))
        self.db.conn.commit()
        return dict(ok=True, found=len(window_rows),
                    qualified=sum(1 for r in window_rows if r[2] and r[5] > 0),
                    follows_paper=follows)

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
            scanned=bool(pos) or (agg["points"] or 0) > 0,
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
    from datetime import datetime
    return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
