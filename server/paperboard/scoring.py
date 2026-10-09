"""Scoring engine — pure functions, config-driven, nothing hardcoded in the UI.

v1 ("pb-v1", amended "pb-v1.1" — see anti-gaming note) per qualifying post:

    raw = basePerPost
          + likes      * perLike
          + replies    * perReply
          + reposts    * perRepost
          + quotes     * perQuote
          + impPoints  * (1 - 2 ** (-impressions / halfLife))   # diminishing curve

    if impressions < minImpressions and engagement == 0:  raw *= thinSignalMultiplier
    points = min(postCap, raw)

Anti-gaming lives next to the math:

  * duplicate detection  — same author + same normalized text (first 60 chars,
    case/space-folded) inside the duplicate window scores once; later copies
    keep points 0 with reason "duplicate".
  * excessive frequency  — maxScoredPostsPerWindow newest posts score; the
    rest are stamped "frequency_capped" (spam wall posts can't farm).
  * impressions use a saturating curve (never linear), so a bot post with
    100k views can't outscore the whole front page.
  * (pb-v1.1, from the 2026-10 live feed probe) thin_signal is a statement
    about ZERO engagement, never about UNKNOWN engagement: the penalty
    applies only when the provider reported something; a post whose metrics
    all came back missing keeps the full base and carries the stamp
    "metrics_unreported". Missing never earns points, and missing is never
    punished as if it were zero.

v3 ("pb-v3.0" — hybrid like scoring, the official-count era) per accepted post:

    likes_counted = like_bonus(official_likes)   # official count only
    raw = basePerPost + likes_counted
    points = min(postCap, raw)

  * every eligible submission earns basePerPost, no matter what engagement
    is visible — a like count that cannot be obtained holds its bonus
    PENDING, it is never converted into an observed zero;
  * only likes score. replies, reposts, quotes, impressions and bookmarks
    ride along in the stored snapshot as audit evidence and contribute
    nothing;
  * the like bonus is one consistent curve — one point per like to the knee,
    then a reduced tail rate, hard-capped — so an automatically retrieved
    count and an admin-verified count of the same number score IDENTICALLY;
  * the official like count is a submit-time snapshot (the policy lives in
    the config as likeMeasurement); a later, authorized manual correction
    rereads it explicitly — corrections are audited, never silent.

Every stored point carries an audit blob (contributions + applied rules), so
the board can always explain itself.
"""

import json
import re

NORM = re.compile(r"\s+")


def normalize_text(text, n=60):
    return NORM.sub(" ", (text or "").lower()).strip()[:n]


def impression_points(imp, cap, half_life):
    """Saturating curve: 0 at 0, asymptote at cap."""
    if half_life <= 0:
        return cap
    return cap * (1 - 2 ** (-imp / half_life))


def like_bonus(likes, s):
    """the pb-v3.0 like-bonus curve, from the official like count.

    one point per like up to likeKnee; past the knee the rate drops to
    likeTailRate per like across likeTailSpan likes (diminishing, still
    rising), and the bonus saturates hard at likeBonusCap — reached
    exactly at knee + tail_span likes.

    as shipped (knee 100, tail 400 at 0.25, cap 200):
        likes 0    -> bonus   0    likes 300  -> bonus 150
        likes 10   -> bonus  10    likes 500  -> bonus 200
        likes 100  -> bonus 100    likes 1000 -> bonus 200 (capped)
    """
    knee = float(s.get("likeKnee", 100))
    span = float(s.get("likeTailSpan", 400))
    rate = float(s.get("likeTailRate", 0.25))
    cap = float(s.get("likeBonusCap", 200))
    n = max(0, int(likes))
    raw = min(n, knee) + rate * min(max(n - knee, 0), span)
    return round(min(raw, cap), 2)


def _like_bonus_score(post, s, ctx):
    """pb-v3.0 — hybrid like scoring (see the module doctrine above).

    the like count in `post` IS the official count by construction: at
    submit time it is the automatic observation (or None), and a manual
    verification re-scores with the admin's count substituted in. a None
    count keeps the base and stamps 'like_bonus_pending' — the bonus is
    held back, never guessed.
    """
    likes = post.get("likes")
    contributions = {"base": float(s["basePerPost"])}
    applied = []
    if likes is None:
        bonus = 0.0
        applied.append("like_bonus_pending")
    else:
        bonus = like_bonus(likes, s)
        applied.append("like_bonus_verified")
    raw = contributions["base"] + bonus
    contributions["likes"] = round(bonus, 2)
    cap = s.get("postCap")
    if cap is not None and raw > cap:
        raw = float(cap)
        applied.append("post_capped")
    if ctx.get("duplicate_of"):
        raw = 0.0
        contributions = {"base": 0.0, "likes": 0.0}
        applied.append("duplicate")
    if ctx.get("over_frequency"):
        raw = 0.0
        contributions = {"base": 0.0, "likes": 0.0}
        applied.append("frequency_capped")
    if ctx.get("disputed"):
        applied.append("disputed")
    if likes is None and not [k for k in
                              ("likes", "replies", "reposts", "quotes",
                               "impressions") if post.get(k) is not None]:
        applied.append("metrics_unreported")
    audit = dict(scoring_version=s["version"],
                 contributions=contributions,
                 applied=applied,
                 metrics=post)
    return round(raw, 2), audit


def score_post(post, cfg, ctx):
    """Score one already-eligible post.

    post: metrics dict (impressions/likes/replies/reposts/quotes)
    ctx : dict(window_seen_texts set, index_in_window int, author_following bool)
    returns (points, audit dict)
    """
    s = cfg
    ctx = ctx or {}
    if str(s.get("version", "")).startswith("pb-v3"):
        return _like_bonus_score(post, s, ctx)
    imp = max(0, post.get("impressions") or 0)
    likes = max(0, post.get("likes") or 0)
    replies = max(0, post.get("replies") or 0)
    reposts = max(0, post.get("reposts") or 0)
    quotes = max(0, post.get("quotes") or 0)
    engagement = likes + replies + reposts + quotes

    contribs = {
        "base": s["basePerPost"],
        "likes": likes * s["perLike"],
        "replies": replies * s["perReply"],
        "reposts": reposts * s["perRepost"],
        "quotes": quotes * s["perQuote"],
        "impressions": round(impression_points(
            imp, s["impressions"]["points"], s["impressions"]["halfLife"]), 2),
    }
    applied = []
    raw = sum(contribs.values())

    # a metric the provider did not report arrives as None and contributes
    # zero points; a post with NOTHING reported carries the stamp
    # "metrics_unreported" and is spared the thin-signal penalty — that
    # penalty speaks about measured zeros, not about absence of measurement.
    reported = any(post.get(k) is not None
                    for k in ("likes", "replies", "reposts", "quotes",
                              "impressions"))
    if not reported:
        applied.append("metrics_unreported")

    if reported and imp < s.get("minImpressions", 10) and engagement == 0:
        raw *= s["thinSignalMultiplier"]
        applied.append("thin_signal")

    if ctx.get("duplicate_of"):
        raw = 0.0
        contribs = {k: 0 for k in contribs}
        applied.append("duplicate")

    if ctx.get("over_frequency"):
        raw = 0.0
        contribs = {k: 0 for k in contribs}
        applied.append("frequency_capped")

    points = round(min(s["postCap"], raw), 2)

    audit = dict(
        scoring_version=s["version"],
        contributions=contribs,
        applied=applied,
        metrics=post,
    )
    return points, audit
