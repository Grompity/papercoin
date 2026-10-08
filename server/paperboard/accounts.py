"""Account-domain helpers — validation + URL parsing, no DB, no side effects.

The PAPERBOARD account (not the X user) is the permanent identity. These
functions keep the gates that guard it honest: an email that parses, a
username that can be a byline, and a submitted URL that actually names an
X post id. Everything here is pure — the Service owns every write.
"""

import hashlib
import re
import secrets
import urllib.parse

# deliberately loose on the local part, strict on the dot + tld: the mail
# standard allows far more than a memecoin should, and a typo'd address
# should fail at the input, not in the delivery log.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# byline rules: starts with a letter, letters/digits/underscore, 2–24 total.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,23}$")

# the one URL shape we trust: (twitter|x).com — any www/mobile/old alias —
# /handle/status/id/... with query and fragment stripped first.
POST_HOST = r"(?:[\w-]+\.)*(?:twitter|x)\.com"
# the id segment is permissive on purpose — a junk tail earns an honest 404
# from the provider; the shape gate only guards the URL, never existence.
POST_ID = r"[A-Za-z0-9_-]{2,25}"
POST_URL_RE = re.compile(
    r"^https?://" + POST_HOST + r"/@?([A-Za-z0-9_]{1,15})/status(?:es)?/("
    + POST_ID + r")(?:[/?#].*)?$",
    re.IGNORECASE,
)


def normalize_email(raw):
    """Trim + case-fold, or None. One canonical form or the dedup wall leaks."""
    if not raw or not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > 254 or "@" not in email:
        return None
    return email if EMAIL_RE.match(email) else None


def normalize_username(raw):
    """Trim + shape-check the byline, or None. Case is preserved; uniqueness
    is checked case-insensitively by the Service."""
    if not raw or not isinstance(raw, str):
        return None
    name = raw.strip()
    return name if USERNAME_RE.match(name) else None


def parse_post_url(raw):
    """(post_id, author, normalized_url) from any reasonable X post URL, or
    None. The post id — never the handle in the URL — is the real key; the
    handle is the printed byline (the author is whatever the provider
    reports server-side)."""
    if not raw or not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url                       # allow the bare paste
    url = url.split("#", 1)[0]                       # fragment is never a path
    base = url.split("?", 1)[0]
    m = POST_URL_RE.match(base)
    if not m:
        return None
    author, post_id = m.group(1), m.group(2)
    return post_id, author.lower(), f"https://x.com/{author.lower()}/status/{post_id}"


def new_magic_token():
    """Cryptographically random, url-safe, opaque. This string is the only
    auth secret that ever travels through a URL — and only as a link."""
    return secrets.token_urlsafe(32)                 # 43 chars, 256 bits


def hash_magic_token(token):
    """Server-side storage form: the link is checked by its sha256, so a
    peeked-at token store never yields a usable key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:64]
