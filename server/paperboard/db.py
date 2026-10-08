"""SQLite persistence. One file, explicit SQL, no ORM theater.

The schema is the audit trail: posts keep their raw metrics plus a scoring
snapshot (JSON) so any future score can be recomputed and explained.
"""

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions (
  id            INTEGER PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  description   TEXT DEFAULT '',
  starts_at     TEXT NOT NULL,
  ends_at       TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active',
  config_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  x_user_id     TEXT UNIQUE NOT NULL,
  x_username    TEXT NOT NULL,
  display_name  TEXT DEFAULT '',
  avatar_url    TEXT DEFAULT '',
  wallet        TEXT,
  follows_paper INTEGER NOT NULL DEFAULT 0,
  follows_since REAL,
  follows_checked_at REAL,
  scan_watermark REAL,
  last_scan_at  REAL,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
  id            INTEGER PRIMARY KEY,
  x_post_id     TEXT UNIQUE NOT NULL,
  x_author_id   TEXT NOT NULL,
  competition_id INTEGER NOT NULL,
  text          TEXT NOT NULL DEFAULT '',
  url           TEXT DEFAULT '',
  posted_at     REAL NOT NULL,
  impressions   INTEGER NOT NULL DEFAULT 0,
  likes         INTEGER NOT NULL DEFAULT 0,
  replies       INTEGER NOT NULL DEFAULT 0,
  reposts       INTEGER NOT NULL DEFAULT 0,
  quotes        INTEGER NOT NULL DEFAULT 0,
  eligible      INTEGER NOT NULL DEFAULT 0,
  reason        TEXT DEFAULT '',
  duplicate_of  TEXT,
  scored_at     REAL,
  score_json    TEXT,
  points        REAL NOT NULL DEFAULT 0,
  metrics_refreshed_at REAL,
  metrics_frozen INTEGER NOT NULL DEFAULT 0,
  UNIQUE (competition_id, x_post_id),
  FOREIGN KEY (competition_id) REFERENCES competitions(id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  id            INTEGER PRIMARY KEY,
  competition_id INTEGER NOT NULL,
  period        REAL NOT NULL,
  generated_at  REAL NOT NULL,
  total_points  REAL NOT NULL DEFAULT 0,
  participants  INTEGER NOT NULL DEFAULT 0,
  scoring_version TEXT DEFAULT '',
  FOREIGN KEY (competition_id) REFERENCES competitions(id)
);

CREATE TABLE IF NOT EXISTS snapshot_rows (
  id            INTEGER PRIMARY KEY,
  snapshot_id   INTEGER NOT NULL,
  user_id       INTEGER NOT NULL,
  rank          INTEGER NOT NULL,
  points        REAL NOT NULL DEFAULT 0,
  posts_count   INTEGER NOT NULL DEFAULT 0,
  engagement    INTEGER NOT NULL DEFAULT 0,
  share_pct     REAL NOT NULL DEFAULT 0,
  share_est     REAL NOT NULL DEFAULT 0,
  tier          TEXT,
  movement      INTEGER,
  FOREIGN KEY (snapshot_id) REFERENCES snapshots(id),
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Sessions carry two kinds of seat in one table: a Connect-X-era seat (X
-- user_id, account_id NULL) and a paperboard account seat (account_id set,
-- user_id NULL). A null that can never join beats a number that happens to
-- collide — so user_id is deliberately nullable here.
CREATE TABLE IF NOT EXISTS sessions (
  token         TEXT PRIMARY KEY,
  user_id       INTEGER,
  created_at    TEXT NOT NULL,
  account_id    INTEGER,
  expires_at    REAL,
  last_seen_at  REAL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
  state         TEXT PRIMARY KEY,
  code_verifier TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- accounts: the permanent PAPERBOARD identity. the account id is the parent
-- of submissions, scores, leaderboard records, and the reward ledger.
-- x identity lives in the posts/submissions row (author_handle), never here.
CREATE TABLE IF NOT EXISTS accounts (
  id                  INTEGER PRIMARY KEY,
  email               TEXT UNIQUE NOT NULL,
  email_verified      INTEGER NOT NULL DEFAULT 0,
  username            TEXT UNIQUE,
  status              TEXT NOT NULL DEFAULT 'active',
  wallet              TEXT,
  wallet_effective_at REAL,
  onboarded_at        REAL,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL,
  last_login_at       REAL
);

-- magic links: short-lived single-use auth tokens. the sha256 hash is the
-- only stored form of the secret — the plaintext lives long enough to mail,
-- then only in the dev mailer's memory (never persisted after creation).
CREATE TABLE IF NOT EXISTS magic_links (
  token_hash   TEXT PRIMARY KEY,
  account_id   INTEGER NOT NULL,
  created_at   REAL NOT NULL,
  expires_at   REAL NOT NULL,
  used_at      REAL,
  FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- submissions: the pasted X post, verified server-side and parented to the
-- account. UNIQUE(competition_id, x_post_id) is the duplicate wall — one post
-- may be submitted once per issue, by anybody, checked before the insert.
CREATE TABLE IF NOT EXISTS submissions (
  id             INTEGER PRIMARY KEY,
  account_id     INTEGER NOT NULL,
  competition_id INTEGER NOT NULL,
  x_post_id      TEXT NOT NULL,
  author_handle  TEXT NOT NULL DEFAULT '',
  url            TEXT NOT NULL,
  text           TEXT NOT NULL DEFAULT '',
  posted_at      REAL,
  submitted_at   REAL NOT NULL,
  verified       INTEGER NOT NULL DEFAULT 0,
  eligible       INTEGER NOT NULL DEFAULT 0,
  reason         TEXT NOT NULL DEFAULT '',
  points         REAL NOT NULL DEFAULT 0,
  metrics_json   TEXT NOT NULL DEFAULT '{}',
  score_json     TEXT,
  UNIQUE (competition_id, x_post_id),
  FOREIGN KEY (account_id) REFERENCES accounts(id),
  FOREIGN KEY (competition_id) REFERENCES competitions(id)
);

-- rewards: the accounting ledger. wallet_address is snapshotted at entry
-- (a wallet change must never rewrite history). no money moves here yet.
CREATE TABLE IF NOT EXISTS rewards (
  id             INTEGER PRIMARY KEY,
  account_id     INTEGER NOT NULL,
  competition_id INTEGER NOT NULL,
  wallet_address TEXT NOT NULL,
  amount         REAL NOT NULL,
  asset          TEXT NOT NULL DEFAULT 'PAPER',
  status         TEXT NOT NULL DEFAULT 'pending',
  created_at     REAL NOT NULL,
  paid_at        REAL,
  reference      TEXT,
  FOREIGN KEY (account_id) REFERENCES accounts(id),
  FOREIGN KEY (competition_id) REFERENCES competitions(id)
);

-- wallet_audit: who moved the reward wallet, from what to what, and when
-- the new address became reward-eligible (the cooldown lands in effective_at).
CREATE TABLE IF NOT EXISTS wallet_audit (
  id           INTEGER PRIMARY KEY,
  account_id   INTEGER NOT NULL,
  old_wallet   TEXT,
  new_wallet   TEXT,
  changed_at   REAL NOT NULL,
  effective_at REAL,
  FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS ix_submissions_account ON submissions(account_id, competition_id);
CREATE INDEX IF NOT EXISTS ix_magic_account       ON magic_links(account_id);
CREATE INDEX IF NOT EXISTS ix_rewards_account     ON rewards(account_id);

CREATE TABLE IF NOT EXISTS scans (
  id            INTEGER PRIMARY KEY,
  x_user_id     TEXT NOT NULL,
  scanned_at    REAL NOT NULL,
  found         INTEGER NOT NULL DEFAULT 0,
  qualified     INTEGER NOT NULL DEFAULT 0,
  kind          TEXT NOT NULL DEFAULT 'delta'
);

CREATE TABLE IF NOT EXISTS x_usage (
  day                 TEXT PRIMARY KEY,
  requests            INTEGER NOT NULL DEFAULT 0,
  post_resources      INTEGER NOT NULL DEFAULT 0,
  user_resources      INTEGER NOT NULL DEFAULT 0,
  following_resources INTEGER NOT NULL DEFAULT 0,
  estimated           REAL NOT NULL DEFAULT 0,
  scans               INTEGER NOT NULL DEFAULT 0,
  follow_checks       INTEGER NOT NULL DEFAULT 0,
  follow_cache_hits   INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(conn):
    """Additive column migration for pre-shipping dev databases."""
    def cols(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for table, col, ddl in (
            ("users", "scan_watermark", "REAL"),
            ("users", "last_scan_at", "REAL"),
            ("posts", "metrics_refreshed_at", "REAL"),
            ("posts", "metrics_frozen", "INTEGER NOT NULL DEFAULT 0"),
            ("scans", "kind", "TEXT NOT NULL DEFAULT 'delta'"),
            # account era (additive — old X-auth columns stay, data is never
            # destroyed; a legacy session simply has no expiry set):
            ("sessions", "account_id", "INTEGER"),
            ("sessions", "expires_at", "REAL"),
            ("sessions", "last_seen_at", "REAL"),
            ("posts", "account_id", "INTEGER")):
        if table in {"users", "posts", "scans", "sessions"} and col not in cols(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # one real migration for pre-account databases: the X-era sessions table
    # declared user_id NOT NULL; an account seat needs it NULL. Rebuild, copy,
    # rename — the data rides along, nothing is destroyed.
    nn = {r[1]: bool(r[3]) for r in conn.execute("PRAGMA table_info(sessions)")}
    if nn.get("user_id"):
        conn.execute("""CREATE TABLE sessions_new(
                          token         TEXT PRIMARY KEY,
                          user_id       INTEGER,
                          created_at    TEXT NOT NULL,
                          account_id    INTEGER,
                          expires_at    REAL,
                          last_seen_at  REAL,
                          FOREIGN KEY (user_id) REFERENCES users(id))""")
        conn.execute("""INSERT INTO sessions_new(token,user_id,created_at,account_id,
                        expires_at,last_seen_at)
                        SELECT token,user_id,created_at,account_id,expires_at,
                               last_seen_at FROM sessions""")
        conn.execute("DROP TABLE sessions")
        conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
    # magic_links once kept a plaintext convenience copy of the token. the
    # hash is the authority and the plaintext now lives only in the mailer's
    # memory, so legacy databases lose the column (rebuild, not drop-column:
    # old sqlite carries no ALTER ... DROP COLUMN).
    if "token" in cols("magic_links"):
        conn.execute("""CREATE TABLE magic_links_new(
                          token_hash   TEXT PRIMARY KEY,
                          account_id   INTEGER NOT NULL,
                          created_at   REAL NOT NULL,
                          expires_at   REAL NOT NULL,
                          used_at      REAL,
                          FOREIGN KEY (account_id) REFERENCES accounts(id))""")
        conn.execute("""INSERT INTO magic_links_new
                        SELECT token_hash,account_id,created_at,expires_at,used_at
                        FROM magic_links""")
        conn.execute("DROP TABLE magic_links")
        conn.execute("ALTER TABLE magic_links_new RENAME TO magic_links")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_magic_account"
                     " ON magic_links(account_id)")
    conn.commit()


class DB:
    def __init__(self, path, verbose=True):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        self.conn.executescript(SCHEMA)
        _migrate(self.conn)
        self.conn.commit()

    def close(self):
        self.conn.close()
