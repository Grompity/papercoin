"""Scoring engine — pure functions, config-driven, nothing hardcoded in the UI.

v1 ("pb-v1") per qualifying post:

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


def score_post(post, cfg, ctx):
    """Score one already-eligible post.

    post: metrics dict (impressions/likes/replies/reposts/quotes)
    ctx : dict(window_seen_texts set, index_in_window int, author_following bool)
    returns (points, audit dict)
    """
    s = cfg
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

    if imp < s.get("minImpressions", 10) and engagement == 0:
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
