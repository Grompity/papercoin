"""Wallet proof for PAPERBOARD identity; reward addresses never grant access."""
import base64
import hashlib
import os
import secrets
import sqlite3
import time

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
except ImportError:
    Ed25519PublicKey = None

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RESERVED_DOMAIN = "wallet.paperboard.invalid"


def public_key(wallet):
    if not isinstance(wallet, str) or not 32 <= len(wallet) <= 44:
        raise ValueError("invalid_wallet")
    n = 0
    for c in wallet:
        if c not in ALPHABET:
            raise ValueError("invalid_wallet")
        n = n * 58 + ALPHABET.index(c)
    raw = b"\0" * (len(wallet) - len(wallet.lstrip("1")))
    raw += n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 32:
        raise ValueError("invalid_wallet")
    return raw


class WalletAuth:
    def __init__(self, service):
        self.s = service
        self.db = service.db.conn
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS wallet_logins (
          wallet TEXT PRIMARY KEY, account_id INTEGER UNIQUE NOT NULL,
          created_at REAL NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(id));
        CREATE TABLE IF NOT EXISTS wallet_login_challenges (
          nonce TEXT PRIMARY KEY, wallet TEXT NOT NULL, account_id INTEGER,
          intent TEXT NOT NULL, message TEXT NOT NULL, expires_at REAL NOT NULL,
          used_at REAL);
        """)

    @property
    def enabled(self):
        return Ed25519PublicKey is not None and os.environ.get("PAPER_WALLET_LOGIN") == "1"

    def challenge(self, wallet, origin, account=None, create_new=False, at=None):
        if not self.enabled:
            raise ValueError("wallet_auth_unavailable")
        public_key(wallet)
        t = time.time() if at is None else at
        existing = self.db.execute("SELECT account_id FROM wallet_logins WHERE wallet=?", (wallet,)).fetchone()
        aid = account["id"] if account else (existing["account_id"] if existing else None)
        if account and account.get("status") != "active":
            raise ValueError("account_closed")
        if account and existing and existing["account_id"] != aid:
            raise ValueError("wallet_already_linked")
        if not account and not existing and not create_new:
            raise ValueError("link_existing_or_create_new")
        intent = "link" if account else ("login" if existing else "create")
        nonce = secrets.token_urlsafe(32)
        message = ("PAPERBOARD wallet sign-in\nOrigin: " + origin +
                   "\nWallet: " + wallet + "\nAction: " + intent +
                   "\nAccount: " + (str(aid) if aid else "new") +
                   "\nNonce: " + nonce + "\nExpires: " + str(int(t + 300)) +
                   "\nThis message grants no transfer or spending permission.")
        self.db.execute("DELETE FROM wallet_login_challenges WHERE expires_at<?", (t - 3600,))
        count = self.db.execute("SELECT COUNT(*) AS n FROM wallet_login_challenges WHERE wallet=? AND expires_at>?", (wallet, t)).fetchone()["n"]
        if count >= 5:
            raise ValueError("wallet_rate_limited")
        self.db.execute("INSERT INTO wallet_login_challenges VALUES(?,?,?,?,?,?,NULL)",
                        (nonce, wallet, aid, intent, message, t + 300))
        self.db.commit()
        return dict(nonce=nonce, message=message, expiresAt=t + 300, intent=intent)

    def confirm(self, nonce, signature, account=None, at=None):
        if not self.enabled:
            raise ValueError("wallet_auth_unavailable")
        t = time.time() if at is None else at
        if not isinstance(nonce, str) or len(nonce) != 43:
            raise ValueError("invalid_wallet_proof")
        row = self.db.execute("SELECT * FROM wallet_login_challenges WHERE nonce=?", (nonce,)).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] <= t:
            raise ValueError("expired_wallet_proof")
        if row["intent"] == "link" and (not account or account["id"] != row["account_id"]):
            raise ValueError("account_session_required")
        try:
            if not isinstance(signature, str) or len(signature) > 100:
                raise ValueError()
            sig = base64.b64decode(signature, validate=True)
            if len(sig) != 64:
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(public_key(row["wallet"])).verify(sig, row["message"].encode())
        except (ValueError, InvalidSignature):
            raise ValueError("invalid_wallet_proof") from None
        try:
            self.db.execute("BEGIN IMMEDIATE")
            updated = self.db.execute("UPDATE wallet_login_challenges SET used_at=? WHERE nonce=? AND used_at IS NULL AND expires_at>?", (t, nonce, t))
            if updated.rowcount != 1:
                raise ValueError("expired_wallet_proof")
            aid = row["account_id"]
            if row["intent"] == "create":
                # Internal identifier only: never a deliverable/recovery email.
                email = "wallet-" + hashlib.sha256(row["wallet"].encode()).hexdigest() + "@" + RESERVED_DOMAIN
                cur = self.db.execute("INSERT INTO accounts(email,created_at,updated_at) VALUES(?,?,?)", (email, t, t))
                aid = cur.lastrowid
            acct = self.s.account_by_id(aid)
            if not acct or acct["status"] != "active":
                raise ValueError("account_closed")
            if row["intent"] in ("link", "create"):
                bound = self.db.execute("SELECT account_id FROM wallet_logins WHERE wallet=?", (row["wallet"],)).fetchone()
                if bound is None:
                    self.db.execute("INSERT INTO wallet_logins VALUES(?,?,?)", (row["wallet"], aid, t))
                elif bound["account_id"] != aid:
                    raise ValueError("wallet_already_linked")
            else:
                bound = self.db.execute("SELECT account_id FROM wallet_logins WHERE wallet=?", (row["wallet"],)).fetchone()
                if not bound or bound["account_id"] != aid:
                    raise ValueError("invalid_wallet_proof")
            self.db.execute("UPDATE accounts SET last_login_at=?,updated_at=? WHERE id=?", (t, t, aid))
            self.db.commit()
        except (ValueError, sqlite3.IntegrityError):
            self.db.rollback()
            raise ValueError("wallet_link_conflict") from None
        return self.s.account_by_id(aid)
