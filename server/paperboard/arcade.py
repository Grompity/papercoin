"""Arcade — the games floor (feature three, the shell).

the shell ships ahead of the games: the arcade's shape is known even while the
repos are still coming — which games exist, what they are called, what a score
means, and where a week ends. the games array is CONFIG truth (one list, one
place), the leaderboards read from the one score table (a score belongs to a
game, an account, and a week — nothing else), and the week boundary rides the
same Friday the payouts already ride: game points count toward the board, and
the board is not the prize purse (the points are not money, and no token pays
on them from this file).
"""

import time

DAY_SECONDS = 86400


def week_key(t=None):
    """the key of the week a score rides: the next UTC Friday midnight that
    CLOSES the week (if today's Friday midnight has already passed, the key is
    the following one). a score can never land in a week that has shut."""
    t = time.time() if t is None else t
    day = int(t) // DAY_SECONDS
    dow = (day + 3) % 7                      # Monday=0 … Friday=4 … Sunday=6
    days = (4 - dow) % 7
    if days == 0:
        days = 7                             # the boundary must lie ahead
    gt = time.gmtime((day + days) * DAY_SECONDS)
    return "%04d-%02d-%02d" % (gt.tm_year, gt.tm_mon, gt.tm_mday)


class Arcade:
    """the floor's whole service: the games list (config's) and the boards
    (the table's). empty boards are the truth of the shell, not a stub."""

    def __init__(self, db, settings):
        ar = settings.pb.get("arcade", {})
        self.db = db
        self.games = [dict(g) for g in ar.get("games", [])]
        self.board_max = int(ar.get("boardMax", 10))
        self.boundary = ar.get("boundary", "friday")

    def known(self, game_id):
        return any(g["id"] == game_id for g in self.games)

    def floor(self, at=None):
        """what GET /api/arcade says: the games as config names them, the
        week they ride, and where the week shuts."""
        return {"games": self.games, "week": week_key(at),
                "boundary": self.boundary}

    def board(self, game_id, week=None, limit=None):
        """one game's board for one week (now by default), best scores
        first; ties are ranked equal — the ladder does not punish a tie."""
        if not self.known(game_id):
            return None
        wk = week if week else week_key()
        lim = self.board_max if limit is None else min(int(limit), self.board_max)
        rows = self.db.conn.execute(
            "SELECT a.username, g.score FROM game_scores g"
            " LEFT JOIN accounts a ON a.id = g.account_id"
            " WHERE g.game_id = ? AND g.week = ?"
            " ORDER BY g.score DESC, g.submitted_at ASC, g.id ASC LIMIT ?",
            (game_id, wk, lim))
        board = []
        rank = 0
        prev = None
        for r in rows:
            if r[1] != prev:
                rank += 1
                prev = r[1]
            board.append({"rank": rank, "handle": r[0] or "unnamed",
                          "score": r[1]})
        return board
