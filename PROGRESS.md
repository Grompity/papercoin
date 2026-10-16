# PAPERBOARD — progress log

(the word the phases end on, kept honest: what shipped, what proved it, and
where the tree stands at each mile. batteries run on BOTH interpreters:
`../.venv/bin/python3` and the framework `Python` binary — exit 0 means shipped.)

---

## Milestone E — Arcade, the shell (today, commit d9976b4)

**The floor before the games.** `arcade.py` + config's `arcade` block: three
named games (Press Run, Paper Tactics, Inkfall) with honest `repo pending`
states and no invented links — the component carries not one href. One score
table (`game_scores`) whose `UNIQUE(game, account, week)` wall is the dedupe
the points-week would lean on. Two read lanes: GET `/api/arcade` (the config's
own words + the week + the boundary) and GET `/api/arcade/leaderboard` (unknown
game answers `unknown_game` 404; empty boards are the truth of the shell).
`week_key()` names the next UTC-Friday midnight that closes the week — the same
Friday the payouts keep — and today being a Friday, it correctly named the NEXT
one: the boundary always lies ahead.

**Caught by the browser, not the eye:** a triple-paren'd `repeat()` had been
folding the tile grid to a single column (both the artboard's and the arcade's,
since their births). Mended same breath; two tracks computed at pane width.
**Exact inputs still missing per game (non-blocking):** the repo/URL to link
the tile to; who posts the score (the game client direct, or the game's own
server); and any per-game score ceiling worth pinning under the config's
`scoreMax`. Everything else of the integration is already cut to receive them.
Battery: P6E section (four checks: the Friday truth, the config's words, the
tie-naming ladder, the dedupe wall, branch-relative lanes). Screenshots
`/var/tmp/p6e-{desktop,mobile}.jpg`.

## Milestone F — the points-week (today, this commit)

**The lane:** POST `/api/arcade/score` behind the account door, its own bucket
(`scorePerTenMinutes` 12/10min).

**The doctrine, spoken and proven live:**
- The WEEK is the server's naming — a client cannot gild its week (the rollover
  is the passage of time, not a cron: boundary−60s rides the closing week, the
  same word at +60s books FRESH in the next; the keys are 604800s apart by the
  calendar's own arithmetic, proven in the battery).
- Validation refuses, never trusts (a browser is a witness, not an authority):
  a digit-STRING is `bad_score` (the ledger will not launder strings), as are
  the fractional, the negative, one over config's `scoreMax`; a stranger game
  is `unknown_game`; none of them wrote a row.
- The law is **best-per-week**: a first word books (201), a better word replaces
  (200 `replaced:true`), and a lesser OR EQUAL word DEFERS (200 `deferred:true`
  — the ledger untouched, proven: `submitted_at` identical before and after).
  The deferral is now the platform's own voice in three lanes: the magic-link
  send, the Artboard re-vote, the score re-word.
- Boards name ties honestly, live-proven: two hands at 55 both stand at rank 1 —
  no fake silver.
- The points do not touch the purse: the scoring snapshot and the prize math
  are untouched files. **For the owner (non-blocking, no token invented):**
  points earn no token yet — when points should cash, say the word and the
  payout day gets its arcade line; until then the ledger stays honest by
  having nothing to pay.

**Battery lesson (the battery gets debugged too):** two initial "failures" were
the battery's own misclaims — a count that also counted the rollover's own
fresh rows, and a claim that the new week's board stood empty when its own
fresh booking filled it. A standalone probe held the module innocent before a
line of it was touched. P6F: five checks, all green both interpreters
(`failures: 0`, exit 0 ×2).

**Front:** the shell gained the score desk (select born empty and filled by the
floor's own words, never an invented game); the doorless submit answered the
door-copy on the page; the boards drew the tied rows. Screenshots
`/var/tmp/p6f-{desktop,mobile}.jpg`.

## Milestone D — World Map, the crowd's shape (today)

**Local commit:** 56853c5 (the pair with C's two still stand as told).

**Backend**
- one additive table (`member_locations`, PK is the account — one seat per
  member, upserted): country required, region/city optional, never an address
  and never a coordinate — the precision asked for is the precision kept.
- `worldmap.py`: set/remove/view/aggregate; the public face speaks exactly two
  words per row (`country`, `count`) — no account id, no email, no session
  crosses it. The counting forgives the case-twins (GROUP BY COLLATE NOCASE);
  the name it prints is the group's deterministic canon (MIN of the spellings).
- routes: GET `/api/worldmap` (aggregate + total, rate-bucketed) and GET
  `/api/account/location` (own seat, slim door) in the GET branch; POST
  `/api/account/location` (upsert, `acct_gate`, its own bucket) and POST
  `/api/account/location/remove` in the POST branch — the day's route lesson
  applied before the fact, and the battery now pins it branch-relatively.
- config: `worldmap` block (60/60/80/40) + two rate keys; validation keys
  bad_country/bad_region/bad_city/account_absent (422/404) and no_location
  answers a bare-handed removal with an honest 404.

**Front half**
- `js/components/worldmap.js` in the section idiom + a hand-drawn hairline SVG
  world (six stroked lands, a grid — zero assets, zero dependencies); the
  runtime in `paperboard.js` (chips from the aggregate, a seat form with set +
  drop doors, every server key with house words); mast gains "World map".

**Route-level evidence (live server, curl + real Chrome):**
- empty map `{countries: [], total: 0}` 200; the write door 401s before state.
- a seat with a leading space comes back trimmed; own-seat GET answers it.
- two members spelled Iceland two ways — the aggregate answered
  `[{"country": "Iceland", "count": 2}], total: 2` — one place, one canon.
- remove twice: `{removed: true}` 200 then `{error: "no_location"}` 404.
- the DOM after a reload: chips `Iceland 1`, the slot reads its truth, six
  lands drawn, the cookie-less submit put the door-copy on the page.
- the mast's overflow at pane width is the P6B design (scrollable index,
  scrollbar hidden), not a break; the plural "1 seat" was caught and mended.

**Battery:** P6D section (five checks incl. the case-twin truth and
branch-relative lane pins) + the front pins — **failures: 0, exit 0, both
interpreters**. A pin that died honestly when the boot-import gained its third
name proves pins must evolve with the shape, not around it.

**State:** local only; VPS still waits for the owner's word (one more table,
one more first-boot nothing — no new dirs needed for the map).

## Milestone C — Artboard, the community wall (today)

**Local commits:** `c03000b` (backend) + the front-half commit this one lands with.

**Backend (shipped, committed `c03000b`)**
- two tables (`artworks`, `artwork_votes` + two indexes) appended to the SCHEMA —
  the X-era `submissions` keeps its meaning, nothing overloaded; `UNIQUE
  (artwork_id, account_id)` is the one-vote wall at the DB, not at the courtesy.
- `artboard.py`: files on disk under the data dir (`PAPER_UPLOADS_DIR`, else
  beside the DB), base64-in-JSON payloads, content-sniffed types (the form's
  claim is decoration), the author's cap rides a UTC day (the same clock the
  Friday boundary keeps), a re-vote is DEFERRED — 200 + `deferred:true` in the
  mail lane's own doctrine.
- four lanes: GET `/api/artworks` (public read, sort new|liked), POST
  `/api/artwork/start|vote|remove` (all behind `acct_gate()`), plus one guarded
  file branch `/artworks/<name>` (name-shape regex, single dot, real extension —
  the traversal attempts answered 404).
- config: one `artboard` block (cap 3, 5MB, title 80, credit 60, note 500,
  list 24 — every battery figure read from it, not from constants) and three
  ratelimit keys.
- battery: 8 new P6C checks + the front pins; **failures: 0, exit 0 on both
  interpreters**.

**Front half (this commit)**
- `js/components/artboard.js` — the section in the press-sheet idiom (ruled
  head, `board__title`, `data-set` slots, an app-injected `#pb-art-wall`).
- the runtime rides in `js/paperboard.js` with the rest of the app: one fetch,
  a delegated click lane, a form that speaks base64; a copy map speaks every
  server error key in house words.
- `main.js` mounts it between the front page and the scoring; the mast gains
  its fifth link; the CSS block is hairlines, mono micro-labels, tabular
  counts — zero box-shadow in the whole artboard block (pinned).

**Live evidence (dev server :5399, scratch DB, both browsers):**
- gallery anonymous → `{"artworks": []}` 200; after a hang, the liked sort
  returns the row.
- hang (real PNG bytes) → 201 with the full row + href; file lane serves the
  bytes 200; `bogus.txt` and traversal shapes → 404.
- the doctrine over the wire: a re-vote answered `{"voted": true,
  "deferred": true, "count": 1}` 200 — no double count.
- remove by the author → `{"removed": true}`; gallery back to empty.
- live DOM (Chrome tab): the card renders from the API (title/credit/count
  true), the state slot reads `the wall: 1 shown`, the mast order is
  `#board,#frontpage,#artboard,#scoring,#bill`; a cookie-less like click put
  the honest door-copy on screen and left the count un-lied at 1.
- screenshots: `/var/tmp/p6c-desktop.jpg`, `/var/tmp/p6c-mobile.jpg`.

**A defect the live box caught that the battery did not:** the gallery lane
first landed inside the POST branch (`/api/me/wallet` is a POST route — the
X-era tail reads mislead). The battery's source-pins passed over it; curl did
not. The lesson stands: **route placement is proved by dispatch, not by
source-shape** — the two dev-server ghosts (a zombie on :5399 from the first
boot era) made the hunt longer than the bug.

**State:** local only — nothing pushed, nothing deployed. VPS gets Artboard
only with the owner's word: rsync + restart, and a first-boot `mkdir`+`chown`
for `/opt/paperboard/data/uploads` (deploy note below).

## Milestone B — the voice becomes law (`b09d35f`)

type/spacing/motion/corner scales in the tokens; the mast index survives at
phone width; skip link + thumb targets; the five honest states got their
vocabulary names (`.state`, `.field`, `.p-defer`) for the lanes to come.
Battery green on both interpreters at that commit.

## Milestone A — the signup email (closed earlier, `49ae617` deployed)

TLS portability (`os.path.exists(CA_BUNDLE)` gate) + the deferral contract
(`{sent:false, deferred:true}` + honest copy); witnessed live, owner-confirmed
in the inbox.

---

## Deploy notes standing (for the owner's word)

1. `rsync` (excludes `.git .cache *sqlite3* .DS_Store`) →
   `chown -R paper:paper /opt/paperboard/app` → ONE `systemctl restart
   paperboard` → health via `api.seyruna.com` (`"ok": true, "scheduler": true`).
2. Artboard adds a first-boot data dir: `mkdir -p /opt/paperboard/data/uploads`
   + `chown paper:paper` it (the app also makedirs at boot; the chown matters).
3. Additive-only migration: the two CREATEs ride the SCHEMA string at boot;
   zero touching of the authoritative rows.
4. The upload rate lane (8/10min per account) and the daily cap (3) are
   config-driven — retune in `config.json`, the battery reads them, not
   constants.
