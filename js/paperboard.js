/* ============================================================
   PAPERBOARD — the app.
   a small state machine on top of a server that holds all the
   authority. the frontend only ever sends: an email, a pasted
   post URL, a public wallet address, and its own impatience.
   identity is the PAPERBOARD account — X is a byline the server
   reads, never a login the user needs.
   ============================================================ */

import { CONFIG } from "./config.js";

const API = (p) => (CONFIG.board.base || "") + p;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const panel = () => $("#pb-panel");
const boardEl = () => $("#pb-board");

export let S = {                       /* app state (client-side facts only) */
  mode: null, cfg: null, dash: null, board: null, comps: null,
  submissions: null, submissionsLoaded: false,
};

/* — fetch helpers (JSON only; never sends scores upward) — */
async function get(path) {
  const r = await fetch(API(path), { credentials: "same-origin" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status, payload: j });
  return j;
}
async function post(path, body) {
  const r = await fetch(API(path), {
    method: "POST", credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status, payload: j });
  return j;
}

/* — formatting — */
const fmt = (n, digits = 0) => Number(n ?? 0).toLocaleString("en-US", {
  maximumFractionDigits: digits, minimumFractionDigits: 0,
});
const fmtTime = (epoch) => {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`;
};
const fmtDay = (epoch) => {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString("en-US",
    { month: "short", day: "numeric", timeZone: "UTC" });
};
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const esc = (s) => (s || "").replace(/[&<>"`]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "`": "&#96;" }[c]));
const shortAddr = (w) => (w ? `${w.slice(0, 4)}…${w.slice(-4)}` : "—");
const errSlot = (name, text) => {
  const el = $(`[data-slot="${name}"]`);
  if (el) { el.textContent = text || ""; if (text) { el.style.color = "var(--tan)"; } }
};

/* — the state machine — */
function setState(name) {
  const p = panel();
  if (!p) return;
  p.dataset.state = name;
  $$(".panel__state", p).forEach((s) =>
    s.classList.toggle("is-active", s.dataset.state === name));
}
function setMode(text, cls = "") {
  const slot = $('[data-slot="mode"]');
  if (slot) slot.innerHTML = `<b class="stamp ${cls}">${text}</b>`;
}

/* — boot: config → account → leaderboard (server is the source of truth) — */
export async function boot() {
  if (S.booting) return;                          /* a retry cannot stack on a running boot */
  S.booting = true;
  try {
    const cfg = await get("/api/config");
    S.cfg = cfg; S.mode = cfg.mode;
    paintConfig(cfg);
    get("/api/competitions").then((j) => { S.comps = j; paintCompetitions(); })
      .catch(() => {});
    const dash = await get("/api/account").catch((e) => (e.status === 401 ? null : null));
    S.dash = dash;
    const lb = await get("/api/leaderboard").catch(() => null);
    S.board = lb;
    paintBoard();
    resolveAccount();
  } catch (e) {
    console.warn('[pb] boot caught:', e && e.constructor && e.constructor.name, (e||{}).message, (e||{}).stack ? String(e.stack).split(String.fromCharCode(10))[1] : '');
    if (e instanceof TypeError) {            /* network down — nothing to fetch; the stamp says so */
      S.mode = "off"; setMode("off press", "stamp--rot");
      setState("idle"); paintBoard();
    } else {                                 /* server answered badly (401-class edges, deploy swaps,
                                                mobile relays) — recoverable. the email lane stays
                                                open for visitors: signing in needs no board config,
                                                and a failing send already speaks in the join lane. */
      const slot = $('[data-slot="err"]');
      if (slot) slot.textContent = "the server did not answer the board (" + ((e && e.message) || "error") + ")";
      setMode("bad answer", "stamp--rot"); setState("idle"); paintBoard();
    }
  } finally {
    S.booting = false;
  }
}

/* config paints the printed numbers so prose can't rot away from the engine */
function paintConfig(c) {
  const s = c.scoring, p = c.prize;
  const set = (k, v) => { const el = $(`[data-set="${k}"]`); if (el) el.textContent = v; };
  set("w-base", s.basePerPost); set("w-like", s.perLike); set("w-reply", s.perReply);
  set("w-repost", s.perRepost); set("w-quote", s.perQuote);
  set("w-imp", `≤ ${s.impressions.points} · curve`); set("w-cap", s.postCap);
  set("scoring-version", s.version);
  set("prize-model", p.model.replace("_", "-").toUpperCase());
  set("prize-pool", fmt(p.pool)); set("prize-unit", p.unit || p.type);
  setMode(c.mode === "mock" ? "mock feed" : "live feed",
          c.mode === "mock" ? "stamp--rot" : "");
}

/* the printed issue line (hero): the server owns the competition,
   the markup only carries the fallback text. graceful in every state:
   live / upcoming / ended / closed / none / api-down (fallback stands). */
const COMP_STATE_LABEL = {
  live: "entries open", upcoming: "opens soon",
  ended: "print closed", closed: "archived",
};
function paintCompetitions() {
  const line = $('[data-set="comp-line"]'), state = $('[data-set="comp-state"]');
  if (!line || !state) return;
  const rows = (S.comps && S.comps.competitions) || [];
  const cur = rows.find((c) => c.state === "live")
    || rows.find((c) => c.state === "upcoming")
    || rows[rows.length - 1];
  if (!cur) { line.textContent = "no issue on press"; state.textContent = "off"; return; }
  const issue = (cur.slug || "").split("-").filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
  line.textContent = `${issue} · Solana edition`;
  let note = COMP_STATE_LABEL[cur.state] || "—";
  if (cur.state === "live" && cur.ends_at) note += ` · ends ${fmtDay(Date.parse(cur.ends_at) / 1000)}`;
  if (cur.state === "upcoming" && cur.starts_at) note += ` · opens ${fmtDay(Date.parse(cur.starts_at) / 1000)}`;
  state.textContent = note;
}

/* — the front page table (X handles on the snapshot; the account rides the
     board via its posts — the snapshot decides who prints) — */
function paintBoard() {
  const mount = boardEl(); if (!mount) return;
  const b = S.board;
  if (!b) {
    const demo = CONFIG.demoBoard.map((r) => `
      <tr>
        <td class="cell-rank rk">${r.rank}</td>
        <td class="cell-name nm"><a class="nm__handle" href="https://x.com/${r.handle}" target="_blank" rel="noopener noreferrer">@${r.handle}</a></td>
        <td class="num" data-label="Points">${fmt(r.points)}</td>
        <td class="num" data-label="Posts">${r.posts}</td>
        <td class="num" data-label="Receipts">—</td>
        <td class="num" data-label="Share">—</td>
      </tr>`);
    mount.innerHTML = `
      <table class="board-table" aria-label="Paperboard standings (demo while the server is off)">
        <thead><tr><th>Rank</th><th>Name</th><th>Points</th><th>Posts</th><th>Receipts</th><th>Share est.</th></tr></thead>
        <tbody>${demo.join("")}</tbody>
      </table>
      <p class="board__lede">DEMO ROWS — start the server to print the real front page.</p>`;
    setFPFoot(b, true);
    return;
  }
  const rows = b.rows.map((r) => {
    const mv = r.movement == null || r.movement === 0 ? ""
      : `<span class="mv ${r.movement > 0 ? "up" : "down"}">${r.movement > 0 ? "▲" : "▼"}${Math.abs(r.movement)}</span>`;
    const you = ((S.dash && S.dash.account && S.dash.account.x_handle) || "").toLowerCase();
    const isYou = !!you && ((r.user && r.user.handle) || "").toLowerCase() === you;
    return `
    <tr class="${r.rank === 1 ? "tr-front" : (r.rank <= 3 ? "tr-head" : "")}${isYou ? " tr-you" : ""}">
      <td class="cell-rank rk">${r.rank}${mv}</td>
      <td class="cell-name nm">
        <a class="nm__handle" href="https://x.com/${r.user.handle}" target="_blank" rel="noopener noreferrer">@${r.user.handle}</a>${isYou ? ' <span class="dim">(you)</span>' : ""}
      </td>
      <td class="num num--pts" data-label="Points">${fmt(r.points, 1)}${r.bonus_points ? ` <span class="dim">(${fmt(r.base_points, 0)}+${fmt(r.bonus_points, 1)})</span>` : ""}${r.pending ? ` <span class="dim" title="official like count awaiting admin review">⏳${r.pending}</span>` : ""}</td>
      <td class="num" data-label="Posts">${r.posts}</td>
      <td class="num" data-label="Receipts">${fmt(r.engagement)}</td>
      <td class="num" data-label="Share">${r.share_pct ? r.share_pct.toFixed(1) + "%" : "—"}${r.share_est ? ` <span class="dim">≈${fmt(r.share_est)}</span>` : ""}</td>
    </tr>`;
  }).join("");
  mount.innerHTML = rows ? `
    <table class="board-table" aria-label="Paperboard standings">
      <thead><tr><th>Rank</th><th>Name</th><th>Points</th><th>Posts</th><th>Receipts</th><th>Share est.</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>` : `
    <div class="fp__empty">
      <div class="fp__empty-art"><img src="${CONFIG.poses.silly}" alt="Paper, looking confused at an empty front page" loading="lazy"></div>
      <p class="board__lede">The front page is empty. Print it.</p>
    </div>`;
  setFPFoot(b, false);
}

function setFPFoot(b, demo) {
  const gen = $('[data-set="fp-generated"]'), tot = $('[data-set="fp-totals"]'),
        st = $('[data-set="fp-state"]');
  if (gen) gen.textContent = demo
    ? "snapshot: — (server offline)"
    : `snapshot: generated ${fmtTime(b.generated_at)} · next ~${fmtTime(b.next_refresh_at)}`;
  if (tot && !demo) tot.textContent = `${fmt(b.participants)} participants · ${fmt(b.total_points)} points on the board`;
  if (st) st.textContent = `state: ${(b && b.state) ?? (demo ? "demo" : "—")}`;
}

/* — resolve account → decide the panel state — */
async function resolveAccount() {
  const out = $$("[data-action='logout']"); out.forEach((b) => (b.hidden = !S.dash));
  if (!S.dash) { setState("idle"); return; }
  if (!S.dash.account || !S.dash.account.onboarded) { setState("welcome"); return; }
  setState("dash");
  await refreshAccount(false);
}

/* pull the account view again (after onboarding / submitting) and paint */
async function refreshAccount(animate = true) {
  try { S.dash = await get("/api/account"); }
  catch { S.dash = null; setState("idle"); return; }
  S.submissionsLoaded = false;
  paintDash(animate);
}

/* — the dashboard render (typography, not cards) — */
function paintDash(animate = true) {
  const d = S.dash; if (!d) return;
  const acct = d.account || {};
  const name = $('[data-slot="acct-name"]');
  if (name) name.textContent = acct.username ? `@${acct.username}` : "PAPERBOARD";
  const rank = $('[data-slot="acct-rank"]');
  if (rank) rank.textContent = d.rank
    ? `#${d.rank} GLOBAL`
    : "unranked — the server hasn't seen a qualifying post yet";
  const comp = $('[data-slot="acct-comp"]');
  if (comp) comp.textContent = COMP_STATE_LABEL[d.competition_state] || "—";
  const pts = $('[data-slot="points"]');
  if (pts) {
    const to = d.points || 0;
    if (reduced() || !animate) pts.textContent = fmt(to, 1);
    else countUp(pts, to);
  }
  const unit = S.cfg?.prize?.unit || S.cfg?.prize?.type || "";
  const stats = $('[data-slot="acct-stats"]');
  /* — the onboarding gate, gate-only: every branch reads the account payload
     (server truth), so a refresh, a sign-in, and a reload each rebuild the
     same view from the record. A returning verified user skips the task, and
     an older backend with no x_verified concept never sees its desk close. */
  const xst = $('[data-slot="x-status"]');
  const xwrap = $("#pb-xstrip");
  const xKnown = "x_verified" in acct;
  if (xwrap) xwrap.hidden = !xKnown;         /* version gate: no field, no dead form */
  const xok = xKnown && !!acct.x_verified;
  const needsX = xKnown && !xok;             /* the one gate the submit desk obeys */
  const prompt = $('[data-slot="x-prompt"]');
  const stepTag = $('[data-slot="x-step"]');
  const stepBox = $("#pb-xverify-step");
  const subBox = $("#pb-submit-box");
  const gateNote = $('[data-slot="submit-gate"]');
  if (prompt) prompt.textContent = (S.cfg && S.cfg.xverify
    && S.cfg.xverify.example) || X_VERIFY_FALLBACK;
  if (stepTag) stepTag.textContent = "X verification required";
  if (xst) xst.textContent = xok
    ? `X account verified — @${acct.x_handle || "?"}`
    : "X verification required — one post is the deed; paste its link once you've published";
  if (stepBox) stepBox.hidden = xok;                   /* verified eyes skip the task */
  if (subBox) subBox.hidden = needsX;                  /* one primary verb at a time */
  if (gateNote) gateNote.hidden = !needsX;             /* explain the closed desk */
  if (stats) stats.innerHTML = `
    <div class="metric"><span class="metric__k">Submitted</span><span class="metric__v">${fmt(d.submitted)}</span></div>
    <div class="metric"><span class="metric__k">Verified</span><span class="metric__v">${fmt(d.verified_posts)}</span></div>
    <div class="metric"><span class="metric__k">Est. reward</span><span class="metric__v">${d.share_est ? `${fmt(d.share_est)} <span class="u">${unit}</span>` : '<span class="u">—</span>'}</span></div>
    <div class="metric"><span class="metric__k">Reward wallet</span><span class="metric__v">${esc(shortAddr(acct.wallet))}
      <button type="button" class="p-dash__swap" data-action="open-wallet">change</button></span></div>`;
  const recent = $('[data-slot="acct-recent"]');
  if (recent) recent.innerHTML = (d.recent && d.recent.length)
    ? d.recent.map((r) => `<p class="dash-line"><b class="${r.points > 0 ? "pos" : "dim"}">${r.points > 0 ? "+" : ""}${fmt(r.points, 1)}</b> pts
       · <a href="${esc(rUrl(r))}" target="_blank" rel="noopener noreferrer">@${esc(r.author)}</a>
       <span class="dim">${r.eligible ? "" : `· ${esc(r.reason.replace("_", " "))}`}</span>
       <span class="dim">${r.like_status === "pending" ? "· like count pending review" : (r.like_status === "disputed" ? "· likes disputed" : "")}</span></p>`).join("")
    : `<p class="dash-line dim">nothing yet — paste a post up top.</p>`;
  const rewards = $('[data-slot="acct-rewards"]');
  if (rewards) rewards.innerHTML = (d.rewards && d.rewards.length)
    ? d.rewards.map((w) => `<p class="dash-line"><b>${fmt(w.amount, 1)} ${esc(w.asset)}</b>
       · ${esc(w.status)} <span class="dim">to ${esc(shortAddr(w.wallet_address))}</span></p>`).join("")
    : `<p class="dash-line dim">no rewards on the ledger yet — the contest is still printing.</p>`;
}
const rUrl = (r) => (r.x_post_id ? `https://x.com/i/status/${r.x_post_id}` : "#");

function countUp(el, to) {
  const t0 = performance.now(), dur = 900;
  el.textContent = fmt(to, 1);          /* settled value first — rAF is decoration */
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur), e = 1 - (1 - k) ** 3;
    el.textContent = fmt(to * e, 1);
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* — the account's own ledger (server audit, rendered, never re-divined) — */
async function loadSubmissions(refresh = true) {
  if (!S.dash) return;
  if (S.submissionsLoaded && !refresh) return;
  try {
    const j = await get("/api/account/submissions");
    S.submissions = j.submissions; S.submissionsLoaded = true;
  } catch { S.submissions = []; }
}
function renderSubmissions() {
  const mount = $('[data-slot="submissions"]'); if (!mount) return;
  const posts = S.submissions || [];
  mount.innerHTML = posts.length ? posts.map((p) => {
    const c = p.audit?.contributions || {};
    const flags = [];
    if (p.matched) flags.push(`<span class="tag bd-flag">${p.matched === CONFIG.contractAddress ? "CA" : esc(p.matched)}</span>`);
    if (p.follow_gate) flags.push(`<span class="tag bd-flag">follow: ${esc(p.follow_gate)}</span>`);
    if (p.reason && p.reason !== "ok") flags.push(`<span class="tag tag--off bd-flag">${esc(p.reason.replace("_", " "))}</span>`);
    (p.audit?.applied || []).forEach((a) => flags.push(`<span class="tag tag--off bd-flag">${esc(a.replace("_", " "))}</span>`));
    const missing = (p.missing || []).length
      ? `<div class="bd-cell"><dt>not reported</dt><dd class="neg">${esc((p.missing || []).join(" · "))}</dd></div>` : "";
    return `
    <article class="bd-post ${p.points ? "" : "is-no"}">
      <div class="bd-post__head">
        <p class="bd-post__text"><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.text.slice(0, 90)) || p.x_post_id}</a></p>
        <span class="bd-post__pts">${fmt(p.points, 1)} pts</span>
      </div>
      <dl class="bd-grid">
        ${["likes", "replies", "reposts", "quotes", "impressions"].map((k) =>
          `<div class="bd-cell"><dt>${k}</dt><dd>${p.metrics[k] == null ? "—" : fmt(p.metrics[k])}${p.metrics[k] != null && c[k] ? ` <span class="u" style="color:var(--green)">+${fmt(c[k])}</span>` : ""}</dd></div>`).join("")}
        ${missing}
        <div class="bd-cell"><dt>author</dt><dd>@${esc(p.author) || "—"}</dd></div>
      </dl>
      <div class="bd-audit">${flags.join("")}</div>
    </article>`;
  }).join("") : `<p class="board__lede">Nothing on the wire yet. paste an X post up top.</p>`;
}

/* — the submit flow (the board's one real verb) — */
function verdict(msg, good, slotName = "submit-verdict") {
  const slot = $(`[data-slot="${slotName}"]`);
  if (!slot) return;
  slot.hidden = false;
  slot.className = `p-dash__verdict ${good ? "is-yes" : "is-no"}`;
  slot.innerHTML = msg;
}
async function doSubmit() {
  const input = $("#pb-submit-input");
  const v = (input?.value || "").trim();
  if (!v) { verdict("paste a public X post URL first.", false); return; }
  verdict("the server reads…", true);
  try {
    const j = await post("/api/account/posts", { url: v });
    const s = j.submission || {};
    const missing = (s.missing || []).length
      ? ` <span class="dim">(${s.missing.length} metrics not reported — not scored)</span>` : "";
    const defer = s.reason === "follow_deferred"
      ? ' <span class="dim">(follow deferred)</span>' : "";
    const pres = s.reason === "presence_verified"
      ? ' <span class="dim">(presence verified — metrics unreported)</span>' : "";
    const verdictMsg = s.eligible
      ? `VERIFIED ✓ — post by @${esc(s.author)} · <b>+${fmt(s.points, 1)} POINTS</b>${missing}${defer}${pres}`
      : `on the wire · <b>0 POINTS</b> — ${esc(s.reason.replace("_", " "))}`;
    verdict(verdictMsg, !!s.eligible);
    input.value = "";
    await refreshAccount(true);
    S.board = await get("/api/leaderboard").catch(() => S.board);
    paintBoard();
  } catch (e) {
    const code = e.payload?.error || "the provider bailed";
    const msg = {
      duplicate_submission: `that post is already on the board${e.payload?.owner === "self" ? " — yours" : ""}.`,
      post_not_found: "no such post out there (or the provider can't see it).",
      bad_post_url: "that doesn't read like an X post URL.",
      submit_rate_limited: "slow hands — the paste line has a pace.",
      submission_limit: "a full day of paste. try tomorrow.",
      provider_unavailable: "the syndication feed blinked. try again.",
    }[code] || code;
    verdict(esc(msg), false);
  }
}

/* — actions — */
/* -- P6K join-retry begin -- */
/* one retry, join flow only. the class retried is by SHAPE — a status at
   500+ carrying no payload error, which is the proxy-page shape (the
   tunnel's 530, the edge's 5xx). the parser cannot prove WHERE a 5xx was
   born, but the app answers only JSON, so every JSON failure — every
   refusal, every pace, the app's own keyed 5xx (server_error, mail_failed)
   — keeps its single try and its words. a no-status reject is NOT retried:
   with no proof the request died before the server, a resend could double
   the mail. the app's own magicResendSeconds (45s) window stands guard
   under a lost-but-processed first attempt — it re-answers { sent: true }
   without minting or mailing twice. the guard below is the doctrine the
   sibling buttons keep with btn.disabled — the join button has none — an
   in-flight dedupe stands in its place: a second click joins the running
   attempt instead of racing a second POST. the var keyword is deliberate:
   the battery clears it between scenarios. */
const JOIN_RETRY_PAUSE = 20000; /* p6n: sized to the measured weather — the
   connector's re-registration took ~18 s (down 20:26:40Z, registered
   20:26:58Z), which 900 ms structurally cannot outrun. one retry still,
   the retried class unchanged; the pause only moved to blip scale. */
/* p6l: the no-status reject now shares the one bounded try. audited: a
   rejection with no status reads as 'no answer came from anywhere' and
   usually means the request died before the server — the retry is then
   the first real attempt; if the first was processed and its response
   was lost, the 45s window re-answers { sent: true } without minting or
   mailing twice; a lapsed window yields one bounded extra send whose
   sibling links stay live (a landing marks only the token it landed on). */
const transientJoin = (e) => !!(e && !(e.payload && e.payload.error) &&
                               (!e.status || e.status >= 500));
var joinInFlight = null;
function postJoinWithRetry(body, postFn, wake) {
  if (joinInFlight) return joinInFlight;
  const run = postFn("/api/account/start", body)
    .catch((e) => {
      if (!transientJoin(e)) throw e;
      return (wake || ((fn) => new Promise((res) =>
        setTimeout(() => res(fn()), JOIN_RETRY_PAUSE))))
        (() => postFn("/api/account/start", body));
    });
  const done = run.then((v) => { joinInFlight = null; return v; },
                        (e) => { joinInFlight = null; throw e; });
  joinInFlight = done;
  return done;
}
/* -- P6K join-retry end -- */

async function doJoin() {
  const input = $("#pb-email-input");
  const v = (input?.value || "").trim();
  errSlot("join-err", "");
  if (!v) { errSlot("join-err", "an email first."); return; }
  try {
    const j = await postJoinWithRetry({ email: v }, post);
    setState("email");
    const slot = $('[data-slot="magic-link"]');
    if (slot) slot.innerHTML = j.link
      ? `<a class="btn btn--pill" href="${esc(j.link)}">open the magic link →</a>` : "";
  } catch (e) {
    const msg = { invalid_email: "that doesn't read like an email.",
                  magic_rate_limited: "the mail door has a pace — breathe.",
                  account_closed: "that account is closed.",
                  mail_failed: "the mail lane did not answer. try again.",
                  server_error: "the server blinked. try again." }[e.payload?.error]
                || e.payload?.error
                || ((e && e.status) ? "the mail door stuck. (n" + e.status + ")"
                                     : "the mail door stuck. no answer.");
    errSlot("join-err", msg);
  }
}

async function doOnboard() {
  const username = ($("#pb-username-input")?.value || "").trim();
  const wallet = ($("#pb-wallet-input")?.value || "").trim();
  const xhandle = ($("#pb-xhandle-input")?.value || "").trim();
  errSlot("onboard-err", "");
  try {
    S.dash = await post("/api/account/onboard", { username, wallet, xhandle });
    setState("dash");
    await refreshAccount(true);
  } catch (e) {
    const msg = { bad_username: "the byline needs letters first (2–24, no punctuation soup).",
                  username_taken: "that byline is already printing.",
                  bad_x_handle: "that doesn't read like an X handle (1–15, letters first).",
                  x_handle_taken: "that X handle already rides another account.",
                  invalid_solana_address: "that isn't shaped like a Solana address (32–44 base58)." }[e.payload?.error]
                || e.payload?.error || "the server blinked.";
    errSlot("onboard-err", msg);
  }
}

async function doChangeWallet() {
  const input = $("#pb-wallet-settings-input");
  const v = (input?.value || "").trim();
  errSlot("wallet-err", "");
  try {
    await post("/api/account/wallet", { wallet: v });
    await refreshAccount(false);
    setState("dash");
  } catch (e) {
    const msg = { fresh_auth_required: "open a fresh magic link first — moving money needs a fresh signature-free proof.",
                  invalid_solana_address: "that isn't shaped like a Solana address.",
                  unsupported_media_type: "form posts need not apply.",
                  account_closed: "that account is closed." }[e.payload?.error]
                || e.payload?.error || "the wallet desk blinked.";
    errSlot("wallet-err", msg);
  }
}

/* — the X ownership proof (a post is the deed; the server reads it) — */
const X_VERIFY_FALLBACK = "I just signed up for PAPERBOARD, a new raiding platform by @paperusdc. Get paid to post!";
async function doXVerify() {
  const input = $("#pb-xverify-input");
  const v = (input?.value || "").trim();
  try {
    const j = await post("/api/account/x/verify", { url: v });
    verdict(j.reason === "verified"
      ? `VERIFIED ✓ — your X account @${j.x_handle} is bound`
      : `already on the record (@${j.x_handle || "?"})`,
      true, "xverify-verdict");
    await refreshAccount(false);
  } catch (e) {
    const msg = { author_mismatch: "the server read a different author on that post.",
                  verification_post_used: "that post already verified somebody else.",
                  verification_exists: "this account already verified — one proof each.",
                  x_handle_taken: "that X identity already rides another account.",
                  verify_wording: S.cfg?.xverify?.wording
                    || "the post must tag @paperusdc and say you joined PAPERBOARD.",
                  verification_pending: "the witness feed answered thin (a byline without an id) — try again in a minute.",
                  bad_post_url: "that doesn't read like an X post URL.",
                  post_not_found: "no such post out there (or the provider can't see it).",
                  provider_unavailable: "the syndication feed blinked. try again.",
                  xverify_rate_limited: "the verification desk has a pace too." }[e.payload?.error]
                || e.payload?.error || "the verification desk blinked.";
    verdict(msg, false, "xverify-verdict");
  }
}

async function connect() {                      /* the legacy Connect-X demo */
  const p = panel(); p.dataset.state = "scan";
  try {
    const j = await post("/api/auth/x/start");
    if (j.mock) { await post(j.url); window.location.reload(); }
    else window.location.href = j.url;
  } catch { setState("err"); $('[data-slot="err"]').textContent = "X start refused (the classic door is legacy)."; }
}

function wire() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest?.("[data-action]"); if (!btn) return;
    const a = btn.dataset.action;
    if (a === "join") await doJoin();
    else if (a === "onboard") await doOnboard();
    else if (a === "submit-post") { btn.disabled = true; await doSubmit(); btn.disabled = false; }
    else if (a === "x-verify") { btn.disabled = true; await doXVerify(); btn.disabled = false; }
    else if (a === "copy-example") {
      /* the prompt on screen is the server's own sentence — the copy button
         copies law, not decoration; both answers are visible, not silent */
      const fb = $('[data-slot="copy-feedback"]');
      const text = ($('[data-slot="x-prompt"]')?.textContent || "").trim() || X_VERIFY_FALLBACK;
      const sayCopy = (t) => { if (fb) fb.textContent = t; };
      if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text)
        .then(() => sayCopy("copied ✓"))
        .catch(() => sayCopy("copy it by hand — the clipboard said no."));
      else sayCopy("the clipboard is asleep here — copy it by hand.");
    }
    else if (a === "open-wallet") setState("wallet");
    else if (a === "change-wallet") await doChangeWallet();
    else if (a === "logout") { try { await post("/api/auth/logout"); } catch {} window.location.reload(); }
    else if (a === "connect") connect();
    else if (a === "mock-login") connect();
    else if (a === "rescan") { await (S.cfg ? refreshAccount(true) : boot()).catch(() => setState("idle")); }
    else if (a === "toggle-submissions") {
      const box = $("#pb-submissions"), open = !box.hidden;
      if (!open && !S.submissionsLoaded) { await loadSubmissions(true); renderSubmissions(); }
      box.hidden = open;
      btn.setAttribute("aria-expanded", String(!open));
      btn.innerHTML = (open ? "View my submissions" : "Hide submissions") + " " + ICONS_CHEVRON;
    }
  });
  /* enter = the button beside the box — the panel's four verbs */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const act = { "pb-email-input": "join", "pb-submit-input": "submit-post",
                  "pb-xverify-input": "x-verify",
                  "pb-wallet-settings-input": "change-wallet" }[e.target?.id];
    if (!act) return;
    e.preventDefault();
    $(`[data-action="${act}"]`)?.click();
  });
  /* hero CTA: scroll to the board and prefill nothing — the email is yours */
  $$("[data-hero-connect]").forEach((a) => a.addEventListener("click", () => {
    if (!S.dash) setTimeout(() => $("#pb-email-input")?.focus(), 400);
  }));
}
const ICONS_CHEVRON = '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

export function startPaperboard() {
  wire();
  return boot();
}
