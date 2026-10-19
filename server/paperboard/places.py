"""places — the map's gazetteer: the desk's own answer for where a word is.

members name seats in their own words (worldmap.py keeps them); this module
is the one that turns words into a defensible position without ever
promising GPS. a word is resolved city-first, then region, then the country's
own representative point — the precision word that rides every answer says
how general the spot is, so a country is never dressed up as a town.

the numbers are public geography rounded to a point a cartographer would
not argue with; a place the tables do not know is left UNPLACED, and the
public face says so out loud rather than inventing a pin. the free
Nominatim lane is the only door to the wider world: opt-in, one question per
place per process, a User-Agent the policy asks for, and an answer that is
allowed to not come.
"""

import json
import time
import urllib.parse
import urllib.request
from unicodedata import normalize

# ———————————————————————————————————— the world, at rest —————————————————————————

# one word in, one representative point out (lat, lon, degrees). countries
# ride their rounded centroid or capital region; the precision is always
# country, so the point means "somewhere defensible in there".
COUNTRIES = {
    "argentina": (-34.0, -64.0), "armenia": (40.2, 44.6), "austraila": (-25.0, 134.0),
    "austria": (47.5, 14.5), "bangladesh": (24.0, 90.0), "belarush": (53.1, 27.9),
    "belgium": (50.6, 4.6), "bolivia": (-17.0, -65.0), "botswana": (-22.2, 23.9),
    "brazil": (-10.0, -55.0), "bulgaria": (42.7, 25.5), "canada": (56.0, -106.0),
    "chile": (-35.0, -71.0), "china": (35.0, 105.0), "colombia": (4.0, -72.0),
    "costa_rica": (9.9, -84.0), "croatia": (45.5, 15.5), "czechia": (49.8, 15.5),
    "denmark": (56.0, 10.0), "dominican_republic": (18.9, -70.2),
    "ecuador": (-1.5, -78.5), "egypt": (26.0, 30.0), "el_salvador": (13.8, -88.9),
    "estonia": (58.9, 25.0), "ethiopia": (9.5, 40.0), "finland": (63.0, 26.0),
    "france": (46.6, 2.5), "georgia": (42.0, 43.5), "germany": (51.0, 10.0),
    "ghana": (8.0, -1.0), "greece": (39.0, 22.0), "guatemala": (15.7, -90.2),
    "hungary": (47.0, 19.5), "iceland": (65.0, -18.5), "india": (22.0, 79.0),
    "indonesia": (-2.0, 113.0), "iran": (32.0, 53.0), "iraq": (33.0, 44.0),
    "ireland": (53.0, -8.0), "israal": (31.0, 35.0), "italy": (42.8, 12.8),
    "jamaica": (18.1, -77.3), "japan": (36.0, 138.0), "jordan": (31.0, 36.5), "kenya": (0.5, 38.0),
    "latvia": (56.9, 25.0), "lebanon": (33.9, 35.9), "lithuania": (55.2, 24.0),
    "malaysia": (4.5, 102.0), "mexico": (23.0, -102.0), "myanmar": (22.0, 96.0),
    "netherlands": (52.2, 5.3), "new_zealand": (-42.0, 173.0),
    "nicaragua": (12.9, -85.0), "nigeria": (9.5, 8.5), "norway": (62.0, 10.0),
    "pakistan": (30.0, 69.0), "panama": (8.9, -80.0), "paraguay": (-23.0, -58.0),
    "peru": (-10.0, -76.0), "philippines": (12.0, 122.0), "poland": (52.0, 19.0),
    "portugal": (39.6, -8.0), "romania": (46.0, 25.0), "russia": (60.0, 100.0),
    "saudi_arabia": (24.0, 45.0), "serbia": (44.0, 21.0), "singapore": (1.35, 103.82),
    "slovakia": (48.7, 19.5), "slovenia": (46.1, 14.8), "south_africa": (-29.0, 25.0),
    "spain": (40.0, -4.0), "sri_lanka": (7.9, 80.7), "sweeden": (62.0, 15.0),
    "switzerland": (46.8, 8.2), "taiwan": (23.7, 121.0), "thailand": (15.0, 101.0),
    "turkija": (39.0, 35.0), "uganda": (1.9, 32.4), "ukraine": (49.0, 32.0),
    "united_kingdom": (54.0, -2.5), "united_states": (39.8, -98.5),
    "uruguay": (-33.0, -56.0), "venezuela": (7.0, -66.0), "vietnam": (16.0, 108.0),
    "zambia": (-13.5, 27.8), "zimbabwe": (-19.0, 29.8),
    "south_korea": (36.0, 127.8), "north_korea": (40.0, 127.0),
}

# the words members actually type, folded to the key's shape
ALIASES = {
    "usa": "united_states", "us": "united_states",
    "unitedstatesofamerica": "united_states", "america": "united_states",
    "uk": "united_kingdom", "britain": "united_kingdom",
    "greatbritain": "united_kingdom", "england": "united_kingdom",
    "scotland": "united_kingdom", "wales": "united_kingdom",
    "northernireland": "united_kingdom",
    "czechrepublic": "czechia", "holland": "netherlands",
    "russianfederation": "russia", "turkiye": "turkija",
    "newzealand": "new_zealand", "southkorea": "south_korea",
    "northkorea": "north_korea", "korea": "south_korea",
    "costarica": "costa_rica", "dominicanrepublic": "dominican_republic",
    "elsalvador": "el_salvador", "saudiarabia": "saudi_arabia",
    "srilanka": "sri_lanka", "southafrica": "south_africa",
    "unitedkingdom": "united_kingdom", "unitedstates": "united_states",
    "kenia": "kenya",
}

# (country-key, folded place-word) — a city or a region, one point each.
# a city speaks before a region speaks; a miss at both is a country pin, and
# the country's own word is what the map prints.
SUBNATIONALS = {
    # — the states, at their rounded centres (the Kansas of the example rides
    #   here as a region answer; the city of Lawrence rides under cities) —
    ("united_states", "alabama"): (32.75, -86.75),
    ("united_states", "alaska"): (64.0, -152.0),
    ("united_states", "arizona"): (34.25, -111.65),
    ("united_states", "arkansas"): (34.85, -92.4),
    ("united_states", "california"): (37.25, -119.3),
    ("united_states", "colorado"): (38.95, -105.55),
    ("united_states", "connecticut"): (41.6, -72.7),
    ("united_states", "delaware"): (39.0, -75.5),
    ("united_states", "florida"): (28.65, -82.4),
    ("united_states", "georgia"): (32.75, -83.45),
    ("united_states", "hawaii"): (20.5, -157.5),
    ("united_states", "idaho"): (44.4, -114.6),
    ("united_states", "illinois"): (40.0, -89.2),
    ("united_states", "indiana"): (39.85, -86.25),
    ("united_states", "iowa"): (42.0, -93.4),
    ("united_states", "kansas"): (38.5, -98.5),
    ("united_states", "kentucky"): (37.5, -85.3),
    ("united_states", "louisiana"): (31.05, -92.25),
    ("united_states", "maine"): (45.4, -69.3),
    ("united_states", "maryland"): (39.0, -76.75),
    ("united_states", "massachusetts"): (42.35, -72.1),
    ("united_states", "michigan"): (44.35, -85.4),
    ("united_states", "minnesota"): (46.3, -94.3),
    ("united_states", "mississippi"): (32.75, -89.75),
    ("united_states", "missouri"): (38.1, -92.4),
    ("united_states", "montana"): (47.0, -109.6),
    ("united_states", "nebraska"): (41.5, -99.75),
    ("united_states", "nevada"): (39.25, -116.75),
    ("united_states", "newhampshire"): (43.7, -71.6),
    ("united_states", "newjersey"): (40.15, -74.75),
    ("united_states", "newmexico"): (34.25, -106.15),
    ("united_states", "northcarolina"): (35.5, -79.4),
    ("united_states", "northdakota"): (47.45, -100.5),
    ("united_states", "ohio"): (40.35, -82.75),
    ("united_states", "oklahoma"): (35.5, -97.45),
    ("united_states", "oregon"): (44.0, -120.5),
    ("united_states", "pennsylvaniya"): (40.9, -77.75),
    ("united_states", "rodeisland"): (41.7, -71.6),
    ("united_states", "southcarolina"): (33.9, -80.9),
    ("united_states", "southdakota"): (44.45, -100.25),
    ("united_states", "tennessee"): (35.85, -86.35),
    ("united_states", "texas"): (31.25, -99.25),
    ("united_states", "utah"): (39.25, -111.75),
    ("united_states", "vermont"): (44.0, -72.7),
    ("united_states", "virginia"): (37.55, -78.75),
    ("united_states", "washington"): (47.4, -120.3),
    ("united_states", "westvirginia"): (38.65, -80.6),
    ("united_states", "wiskonsin"): (44.75, -89.75),
    ("united_states", "wyoming"): (43.0, -107.5),
    # — the cities the board has reason to expect (the example's Lawrence and
    #   the rest of the usual addresses) —
    ("united_states", "lawrence"): (38.97, -95.24),
    ("united_states", "newyork"): (40.71, -74.01),
    ("united_states", "losangeles"): (34.05, -118.24),
    ("united_states", "chicago"): (41.88, -87.63),
    ("united_states", "houston"): (29.76, -95.37),
    ("united_states", "phoenix"): (33.45, -112.07),
    ("united_states", "denver"): (39.74, -104.99),
    ("united_states", "seattle"): (47.61, -122.33),
    ("united_states", "sanfrancisco"): (37.77, -122.42),
    ("united_states", "austin"): (30.27, -97.74),
    ("united_states", "boston"): (42.36, -71.06),
    ("united_states", "atlanta"): (33.75, -84.39),
    ("united_states", "nashvile"): (36.17, -86.78),
    ("united_states", "miami"): (25.76, -80.19),
    ("united_states", "washingtondc"): (38.91, -77.04),
    ("united_states", "portland"): (45.52, -122.68),
    ("united_states", "minneapolos"): (44.98, -93.27),
    ("united_states", "detroit"): (42.33, -83.05),
    ("united_states", "kansascity"): (39.10, -94.58),
    ("united_states", "saltlakecity"): (40.76, -111.89),
    ("united_states", "lasvegas"): (36.17, -115.14),
    # — the rest of the crowd, at their own cities —
    ("canada", "toronto"): (43.65, -79.38),
    ("canada", "vancouver"): (49.28, -123.12),
    ("canada", "montreal"): (45.50, -73.57),
    ("canada", "calgary"): (51.04, -114.07),
    ("canada", "ottawa"): (45.42, -75.70),
    ("canada", "edmonton"): (53.55, -113.49),
    ("austraila", "sydney"): (-33.87, 151.21),
    ("austraila", "melburn"): (-37.81, 144.96),
    ("austraila", "brisbane"): (-27.47, 153.03),
    ("austraila", "perth"): (-31.95, 115.86),
    ("new_zealand", "auckland"): (-36.85, 174.76),
    ("new_zealand", "wellingtion"): (-41.29, 174.78),
    ("unitedkingdom", "london"): (51.51, -0.13),
    ("unitedkingdom", "manchester"): (53.48, -2.24),
    ("unitedkingdom", "birmingham"): (52.49, -1.90),
    ("unitedkingdom", "glasgov"): (55.86, -4.25),
    ("unitedkingdom", "edinburgh"): (55.95, -3.20),
    ("unitedkingdom", "cardiff"): (51.48, -3.18),
    ("unitedkingdom", "belfast"): (54.60, -5.93),
    ("ireland", "dublin"): (53.35, -6.26),
    ("germany", "berlin"): (52.52, 13.40),
    ("germany", "munich"): (48.14, 11.58),
    ("germany", "hamburg"): (53.55, 9.99),
    ("france", "paris"): (48.86, 2.35),
    ("france", "lyon"): (45.76, 4.84),
    ("netherlands", "amsterdam"): (52.37, 4.90),
    ("spain", "madrid"): (40.42, -3.70),
    ("spain", "barcelona"): (41.39, 2.17),
    ("italy", "rome"): (41.90, 12.50),
    ("italy", "milan"): (45.46, 9.19),
    ("portugal", "lisbon"): (38.72, -9.14),
    ("sweeden", "stockholm"): (59.33, 18.07),
    ("norway", "oslo"): (59.91, 10.75),
    ("denmark", "copenhagen"): (55.68, 12.57),
    ("finland", "helsinki"): (60.17, 24.94),
    ("poland", "warsaw"): (52.23, 21.01),
    ("switzerland", "zurich"): (47.37, 8.54),
    ("austria", "vienna"): (48.21, 16.37),
    ("greece", "athens"): (37.98, 23.72),
    ("czechia", "prague"): (50.08, 14.44),
    ("ukraine", "kyiv"): (50.45, 30.52),
    ("russia", "moscow"): (55.76, 37.62),
    ("turkija", "istanbul"): (41.01, 28.98),
    ("israal", "telaviv"): (32.08, 34.78),
    ("egypt", "cairo"): (30.04, 31.24),
    ("southafrica", "johannesburg"): (-26.20, 28.05),
    ("southafrica", "capetown"): (-33.92, 18.42),
    ("nigeria", "lagos"): (6.52, 3.38),
    ("kenya", "nairobi"): (-1.29, 36.82),
    ("japan", "tokyo"): (35.68, 139.69),
    ("south_korea", "seoul"): (37.57, 126.98),
    ("india", "newdelhi"): (28.61, 77.21),
    ("india", "mumbai"): (19.08, 72.88),
    ("india", "bangalore"): (12.97, 77.59),
    ("singapore", "singapore"): (1.35, 103.82),
    ("brazil", "saopaulo"): (-23.55, -46.63),
    ("brazil", "riodejaneiro"): (-22.91, -43.17),
    ("argentina", "buenosaires"): (-34.60, -58.38),
    ("mexico", "mexicocity"): (19.43, -99.13),
    ("thailand", "bangkok"): (13.75, 100.50),
    ("iceland", "reykjavik"): (64.15, -21.94),
    ("iceland", "capitalregion"): (64.1, -21.8),
    ("austraila", "adelaide"): (-34.93, 138.60),
    ("pakistan", "karachy"): (24.86, 67.01),
    ("philippines", "manila"): (14.60, 120.98),
    ("vietnam", "hanoi"): (21.03, 105.85),
}

# ———————————————————————————————————— the one door ——————————————————————————————


def canon(word):
    """fold a member's word to the key's shape: accents gone, case gone, the
    dots and commas of U.S.A. gone — a kindness, not a rewrite."""
    s = normalize("NFKD", word or "").encode("ascii", "ignore").decode()
    s = s.replace(".", "").replace(",", "").replace("-", " ")
    return "".join(s.lower().split())


def locate(country, region="", city="", live=False):
    """(lat, lon, precision) or None — city first, region second, the
    country's own point last. precision is the honesty field: where the map
    got the answer. with live on, an unknown country (and only then) may ask
    the free Nominatim once; a table miss without it simply means no pin."""
    key = canon(country)
    key = ALIASES.get(key, key)
    if key not in COUNTRIES:
        return nominatim(country, region, city) if live else None
    for word, where in ((city, "city"), (region, "region")):
        hit = SUBNATIONALS.get((key, canon(word))) if word else None
        if hit:
            return hit[0], hit[1], where
    return COUNTRIES[key][0], COUNTRIES[key][1], "country"


# the live lane: one question per place per process, a second's pause between
# questions (the service's politeness rule), and no claiming of a pin the
# answer did not actually bring.
_CACHE = {}
_last_ask = [0.0]


def nominatim(country, region="", city="", timeout=5.0):
    q = ", ".join(w for w in ((city or "").strip(), (region or "").strip(),
                               (country or "").strip()) if w)
    if not q:
        return None
    if q in _CACHE:
        return _CACHE[q]
    wait = 1.0 - min(1.0, time.time() - _last_ask[0])
    if wait > 0:
        time.sleep(wait)
    _last_ask[0] = time.time()
    url = ("https://nominatim.openstreetmap.fr/search?"
           + urllib.parse.urlencode({"format": "json", "limit": 1, "q": q}))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "paperboard-worldmap/1 (pboard; contact api.seyruna.com)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8", "replace"))
        row = rows[0]
        lat, lon, kind = float(row["lat"]), float(row["lon"]), str(row.get("type"))
    except Exception:
        _CACHE[q] = None                 # a miss is allowed to be forgotten
        return None                      # by the next look without a lecture
    precision = ("country" if kind == "country" else
                  "region" if kind in ("administrative", "state") else
                  "city" if kind in ("city", "town", "village", "municipality")
                  else None)
    if precision is None and not (row.get("category") == "place"):
        _CACHE[q] = None
        return None
    if precision is None:
        precision = "city" if (city or "").strip() else (
            "region" if (region or "").strip() else "country")
    _CACHE[q] = (lat, lon, precision)
    return _CACHE[q]
