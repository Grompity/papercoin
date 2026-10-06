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

CREATE TABLE IF NOT EXISTS sessions (
  token         TEXT PRIMARY KEY,
  user_id       INTEGER NOT NULL,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
  state         TEXT PRIMARY KEY,
  code_verifier TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

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
            ("scans", "kind", "TEXT NOT NULL DEFAULT 'delta'")):
        if table in {"users", "posts", "scans"} and col not in cols(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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
