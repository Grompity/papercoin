"""Eligibility — the gate every post must pass before scoring.

QUALIFIES IF:
    the author follows @paperusdc
AND the post text contains at least ONE identifier:
    the CA  OR  @paperusdc  OR  $paper   (case rules below, config-driven)

The follow check is a server-side API call — never a frontend boolean.
`followEffect` decides whether posts written before the user started
following become eligible on the next scan ("retroactive_on_next_scan")
or only posts created after the follow date ("from_follow_date").
"""

import re

CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58, loose shape check


def normalize_wallet(addr):
    """Server-side validation of the PRIZE-DELIVERY address. No signing, no
    adapter — a public base58 string or nothing."""
    if not addr:
        return None
    addr = addr.strip()
    if CA_RE.match(addr):
        return addr
    return None


class Eligibility:
    def __init__(self, cfg):
        e = cfg["eligibility"]
        self.identifiers = e["identifiers"]
        self.follow_account = e["followAccount"].lower()
        self.effect = e["followEffect"]
        self.min_impressions = e.get("minImpressions", 10)

    def matched_identifier(self, text):
        """Return the first identifier present in the text, else None.
        CA is case-strict (base58 matters); @handle and $ticker are
        case-insensitive."""
        for ident in self.identifiers:
            if len(ident) >= 20:                                   # CA — case-strict base58
                if ident in text:
                    return ident
            elif ident.lower() in text.lower():                   # @handle / $ticker
                return ident
        return None

    def verdict(self, text, follows_paper):
        """Full gate: follow first (no points if you don't follow), then one
        identifier. Returns (eligible, reason, matched)."""
        if not follows_paper:
            return False, "no_follow", None
        matched = self.matched_identifier(text)
        if matched is None:
            return False, "no_identifier", None
        return True, "ok", matched
