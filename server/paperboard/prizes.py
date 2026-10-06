"""Prize architecture — distribution models, server-computed.

  PROPORTIONAL: share = points / total_points * poolShare
  TOP_N:        fixed rank weights (e.g. 50/30/20 of the top_n slice)
  MIXED:        splits of the pool, each part computed by its own model

"Estimated reward/share" on the board is the user's current proportional
estimate (what their points-percent would convert to at snapshot time), plus
the tier flags the other slices add on top. Nothing here moves money — it
estimates; custody/distribution stays a later, server-side decision.
"""


def estimate(rows, prize_cfg):
    """rows: [{user_id, points}] in rank order. Returns per-user estimates.

    Returns dict: {user_id: {share_pct, share_est}} — share_est in pool units.
    """
    total = sum(max(0, r["points"]) for r in rows) or 0.0
    models = prize_cfg["models"]
    model = prize_cfg.get("model", "proportional")
    pool = prize_cfg.get("pool", 0)

    out = {r["user_id"]: dict(share_pct=0.0, share_est=0.0) for r in rows}
    if total <= 0 or pool <= 0:
        return out

    # every participant's share of the points, always — regardless of model
    for r in rows:
        out[r["user_id"]]["share_pct"] = round(max(0, r["points"]) / total * 100, 4)

    if model == "proportional":
        share = models["proportional"]["ofPool"]
        for r in rows:
            pct = max(0, r["points"]) / total
            out[r["user_id"]]["share_est"] = round(pct * pool * share, 2)
    elif model == "top_n":
        cfg = models["top_n"]
        slice_pool = pool * cfg["ofPool"]
        for i, r in enumerate(rows[:cfg["n"]]):
            w = cfg["weights"][i] if i < len(cfg["weights"]) else 0
            out[r["user_id"]]["share_est"] = round(slice_pool * w, 2)
        # everyone still shows their proportional share for context
        for r in rows:
            out[r["user_id"]]["share_pct"] = round(
                max(0, r["points"]) / total * 100, 4)
    elif model == "mixed":
        for part, spec in models["mixed"]["splits"].items():
            if part == "proportional":
                slice_pool = pool * spec
                for r in rows:
                    pct = max(0, r["points"]) / total
                    out[r["user_id"]]["share_est"] += round(pct * slice_pool, 2)
            elif part == "top_n":
                slice_pool = pool * spec["share"]
                for i, r in enumerate(rows[:spec["n"]]):
                    w = spec["weights"][i] if i < len(spec["weights"]) else 0
                    out[r["user_id"]]["share_est"] += round(slice_pool * w, 2)
            elif part == "community":
                pass  # TBD by the community jury at payout time
    else:
        raise ValueError(f"unknown prize model: {model}")

    for v in out.values():
        v["share_est"] = round(v["share_est"], 2)
    return out


def tier_for(rank, tiers_cfg):
    for t in tiers_cfg:
        if t["from"] <= rank <= t["to"]:
            return t["label"]
    return None
