"""WorldMap — where the paper comes from (feature two).

members name their own seat in their own words: a country, and optionally a
region and a city. no addresses, no coordinates — the precision the feature
asks for is the precision it keeps. one row per member, upserted, and leaving
deletes it. the public face is aggregate only: counts by country — never an
account id, never an email, never one member's seat shown where a crowd's
shape belongs.
"""

import time


class WorldMap:
    """the ledger of seats: set, drop, view, and the crowd's shape."""

    def __init__(self, db, settings):
        wm = settings.pb.get("worldmap", {})
        self.db = db
        self.country_max = int(wm.get("countryMax", 60))
        self.region_max = int(wm.get("regionMax", 60))
        self.city_max = int(wm.get("cityMax", 80))
        self.list_max = int(wm.get("listMax", 40))

    # -------------------------------------------------------------------- set
    def set(self, account_id, body, at=None):
        """one seat per member — an update replaces, never appends.
        returns (row|None, error-key|None)."""
        t = time.time() if at is None else at
        country = (body.get("country") or "").strip()
        region = (body.get("region") or "").strip()
        city = (body.get("city") or "").strip()
        if not country or len(country) > self.country_max:
            return None, "bad_country"
        if len(region) > self.region_max:
            return None, "bad_region"
        if len(city) > self.city_max:
            return None, "bad_city"
        if self.db.conn.execute("SELECT 1 FROM accounts WHERE id=?",
                                (account_id,)).fetchone() is None:
            return None, "account_absent"
        self.db.conn.execute(
            "INSERT OR REPLACE INTO member_locations(account_id,country,region,city,updated_at)"
            " VALUES(?,?,?,?,?)", (account_id, country, region, city, t))
        self.db.conn.commit()
        return self.view(account_id), None

    # ------------------------------------------------------------------- remove
    def remove(self, account_id):
        """dropping a seat that was never set is no_location, not an error
        dressed up — the answer is still the truth."""
        cur = self.db.conn.execute(
            "DELETE FROM member_locations WHERE account_id=?", (account_id,))
        self.db.conn.commit()
        return "removed" if cur.rowcount else "no_location"

    # -------------------------------------------------------------------- view
    def view(self, account_id):
        r = self.db.conn.execute(
            "SELECT country, region, city, updated_at FROM member_locations"
            " WHERE account_id=?", (account_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------- the public face
    def aggregate(self, limit=None):
        """counts by country — the only two words it ever speaks are
        country and count. the counting forgives the twins of the case
        (iceland and Iceland are one place); the name it prints is the
        group's deterministic canon (its alphabetically-first spelling).
        most-crowded first, ties alphabetical."""
        lim = self.list_max if limit is None else min(int(limit), self.list_max)
        rows = self.db.conn.execute(
            "SELECT MIN(country) AS country, COUNT(*) AS count"
            " FROM member_locations"
            " GROUP BY country COLLATE NOCASE"
            " ORDER BY count DESC, country LIMIT ?", (lim,))
        return [{"country": r[0], "count": r[1]} for r in rows]

    def total(self):
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM member_locations").fetchone()[0]
