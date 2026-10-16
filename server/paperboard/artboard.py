"""Artboard — the community wall (feature one).

members hang PAPER art; the wall lists newest-first or most-liked-first.
the row is the ledger, the bytes live on disk under the data directory —
the database stays the authority and small. no points move here by default:
likes count, they do not pay; any future economy is an explicit decision.

validation is content-first: file type is sniffed from magic bytes (the
client's claimed type is decoration), payloads ride base64 because every
lane in this house speaks small JSON, and the author's daily cap rides on
a UTC day because the VPS clock speaks UTC. a re-vote is DEFERRED, not an
error — the fifth state this house named in the mail lane.
"""

import binascii
import os
import re
import secrets
import time

# the file decides what it is; the form's claim is decoration
MAGICS = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
KINDS = {"png": "image/png", "jpg": "image/jpeg",
         "gif": "image/gif", "webp": "image/webp"}
_NAME_RE = re.compile(r"a[0-9]{1,14}-[0-9]{1,9}-[0-9a-f]{6,16}")


def sniff(raw):
    """kind-from-bytes-or-None. webp is checked by its container shape."""
    for magic, kind in MAGICS:
        if raw.startswith(magic):
            return kind
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


class Artboard:
    """the wall's whole service: validate, store, list, vote, own, remove."""

    def __init__(self, db, settings):
        ab = settings.pb.get("artboard", {})
        self.db = db
        self.cap = int(ab.get("maxPerDay", 3))
        self.max_bytes = int(ab.get("maxBytes", 5 * 1000 * 1000))
        self.title_max = int(ab.get("titleMax", 80))
        self.credit_max = int(ab.get("creditMax", 60))
        self.note_max = int(ab.get("noteMax", 500))
        self.list_max = int(ab.get("listMax", 24))
        env = (os.environ.get("PAPER_UPLOADS_DIR") or "").strip()
        self.root = env or os.path.join(os.path.dirname(settings.db_path),
                                        "uploads")
        os.makedirs(self.root, exist_ok=True)

    # ---------------------------------------------------------------- the day
    @staticmethod
    def _day_start(t):
        """UTC day boundary — the same clock the Friday payout rides on."""
        return (int(t) // 86400) * 86400

    def today_count(self, account_id, at=None):
        t = time.time() if at is None else at
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE account_id=? AND created_at>=?",
            (account_id, self._day_start(t))).fetchone()[0]

    # -------------------------------------------------------------------- add
    def add(self, account_id, body, at=None):
        """one hang on the wall. returns (row|None, error-key|None)."""
        t = time.time() if at is None else at
        title = (body.get("title") or "").strip()
        credit = (body.get("credit") or "").strip()
        note = (body.get("note") or "").strip()
        if not title or len(title) > self.title_max:
            return None, "bad_title"
        if len(credit) > self.credit_max:
            return None, "bad_credit"
        if len(note) > self.note_max:
            return None, "bad_note"
        if self.db.conn.execute("SELECT 1 FROM accounts WHERE id=?",
                                (account_id,)).fetchone() is None:
            return None, "account_absent"
        if self.today_count(account_id, t) >= self.cap:
            return None, "daily_cap"
        raw_b64 = (body.get("data_b64") or "").strip()
        payload = raw_b64.partition(",")[2] or raw_b64   # tolerate a data-url
        try:
            raw = binascii.a2b_base64(payload)
        except binascii.Error:
            return None, "bad_image_data"
        if len(raw) > self.max_bytes:
            return None, "too_large"
        kind = sniff(raw)
        if kind is None:
            return None, "bad_image_type"
        name = "a%d-%d-%s.%s" % (int(t), account_id, secrets.token_hex(8), kind)
        tmp = os.path.join(self.root, name + ".part")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.root, name))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None, "store_failed"
        cur = self.db.conn.execute(
            "INSERT INTO artworks(account_id,title,credit,note,file,bytes,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (account_id, title, credit, note, name, len(raw), t))
        self.db.conn.commit()
        row = self.row(cur.lastrowid)
        return row, None

    # -------------------------------------------------------------------- read
    def row(self, artwork_id):
        r = self.db.conn.execute(
            "SELECT id,title,credit,note,file,bytes,created_at,account_id"
            " FROM artworks WHERE id=?", (artwork_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["href"] = "/artworks/" + d["file"]
        return d

    def list(self, sort="new", limit=None):
        """the wall, honest counts by subquery (the votes table is truth)."""
        lim = self.list_max if limit is None else min(int(limit), self.list_max)
        order = ("likes DESC, a.created_at DESC, a.id DESC" if sort == "liked"
                 else "a.created_at DESC, a.id DESC")
        rows = self.db.conn.execute(
            "SELECT a.id, a.title, a.credit, a.note, a.file, a.bytes,"
            " a.created_at, a.account_id,"
            " (SELECT COUNT(*) FROM artwork_votes v WHERE v.artwork_id = a.id)"
            " AS likes FROM artworks a ORDER BY " + order + " LIMIT ?", (lim,))
        out = []
        for r in rows:
            d = dict(r)
            d["href"] = "/artworks/" + d["file"]
            out.append(d)
        return out

    # ------------------------------------------------------------------- vote
    def vote(self, artwork_id, account_id, at=None):
        """returns ('added'|'duplicate'|'art_absent', count)."""
        t = time.time() if at is None else at
        if self.db.conn.execute("SELECT 1 FROM artworks WHERE id=?",
                                (artwork_id,)).fetchone() is None:
            return "art_absent", 0
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO artwork_votes(artwork_id,account_id,created_at)"
            " VALUES(?,?,?)", (artwork_id, account_id, t))
        self.db.conn.commit()
        n = self.db.conn.execute(
            "SELECT COUNT(*) FROM artwork_votes WHERE artwork_id=?",
            (artwork_id,)).fetchone()[0]
        return ("added" if cur.rowcount else "duplicate"), n

    # ----------------------------------------------------------------- ownership
    def owner(self, artwork_id):
        r = self.db.conn.execute(
            "SELECT account_id, file FROM artworks WHERE id=?",
            (artwork_id,)).fetchone()
        return (r[0], r[1]) if r else (None, None)

    def remove(self, artwork_id, account_id):
        """only the author takes their own paper down; the file unlink is
        best-effort — the row's departure is the truth, a vanished file is
        not a failure worth a 500."""
        owner_id, fname = self.owner(artwork_id)
        if owner_id is None:
            return "art_absent"
        if owner_id != account_id:
            return "not_owner"
        self.db.conn.execute("DELETE FROM artwork_votes WHERE artwork_id=?",
                             (artwork_id,))
        self.db.conn.execute("DELETE FROM artworks WHERE id=?", (artwork_id,))
        self.db.conn.commit()
        try:
            os.remove(os.path.join(self.root, fname))
        except OSError:
            pass
        return "removed"

    # --------------------------------------------------------------- file lane
    def served(self, name):
        """(bytes, mime) for GET /artworks/<name>, walls first; None = 404."""
        if ("/" in name or "\\" in name or ".." in name
                or name.count(".") != 1 or not name):
            return None
        base, _, ext = name.rpartition(".")
        ext = ext.lower()
        if ext not in KINDS or _NAME_RE.match(base) is None:
            return None
        full = os.path.abspath(os.path.join(self.root, name))
        if not full.startswith(os.path.abspath(self.root) + os.sep):
            return None
        try:
            with open(full, "rb") as fh:
                return fh.read(), KINDS[ext]
        except OSError:
            return None

    def count(self):
        return self.db.conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
