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
  resumeWalletDraft();                        /* p0: a half-finished save comes home */
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
      <button type="button" class="p-dash__swap" data-action="open-wallet">change</button>
      <button type="button" class="p-dash__swap" data-action="copy-wallet">copy</button>
      <span class="dim" data-slot="wallet-copy"></span></span></div>`;
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
      ? `<a class="btn btn--pill" href="${esc(j.link)}">open the magic link →</a>`
      : (j.deferred ? "an email from moments ago is still fresh." : "");
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

let walletArmed = null;   /* two stages to a save: the first click arms, the second commits */
/* p0: the wall that ate a member's afternoon. the proof to move money lives
 * fifteen minutes past a click (by design), and the board now answers for it
 * itself — it sends its own magic ask through the same anonymous door the
 * login rides, watches the very server word the wallet door reads, and commits
 * the stowed draft exactly once. the draft rides the browser's own small
 * storage, so a reload (or a return from the mail app) finds the desk still
 * mid-thought — never a logout, never a lost address. */
let walletDraft = null, walletBusy = false, walletPoll = null;
const walletStore = {
  set(v) { try { localStorage.setItem("paperboard-wallet", v ? JSON.stringify(v) : ""); } catch (e) {} },
  get() { try { const raw = localStorage.getItem("paperboard-wallet");
               return raw ? JSON.parse(raw) : null; } catch (e) { return null; } },
};
const errWords = () => $(`[data-slot="wallet-err"]`);
function armWalletWords() {                  /* the words that answer a click */
  const el = errWords();
  if (el) { el.classList.add("is-clickable");
            el.onclick = () => { if (!walletBusy) sendFreshLink(); }; }
}
function clearWalletWords() {
  const el = errWords();
  if (el) { el.classList.remove("is-clickable"); el.onclick = null; }
}
async function doChangeWallet() {
  const input = $("#pb-wallet-settings-input");
  const save = $(`[data-action="change-wallet"]`);
  if (walletBusy) return;                    /* one committed request in the while, ever */
  const v = (input?.value || "").trim();
  errSlot("wallet-err", ""); clearWalletWords();
  if (!v) { errSlot("wallet-err", "name the new address first."); return; }
  if (v === (((S.dash || {}).account || {}).wallet || null)) {
    walletArmed = null; walletDraft = null; walletStore.set(null);
    if (save) save.textContent = "Save";
    errSlot("wallet-err", "that is already the reward wallet.");
    return;
  }
  if (walletArmed !== v) {                  /* the explicit confirmation step */
    walletArmed = v;
    if (save) save.textContent = "confirm?";
    errSlot("wallet-err", `confirm: rewards move to ${shortAddr(v)}`);
    return;
  }
  walletArmed = null;
  if (save) save.textContent = "Save";
  walletBusy = true;
  try {
    const d = await post("/api/account/wallet", { wallet: v });
    walletDraft = null; walletStore.set(null);
    await refreshAccount(false);
    setState("dash");
    errSlot("wallet-err", `saved — rewards will ride ${shortAddr(d.wallet)}`);
  } catch (e) {
    const err = e.payload?.error;
    if (err === "fresh_auth_required") {    /* p0: the inline re-verify, never a logout */
      walletDraft = v;
      await sendFreshLink(v);
      return;
    }
    walletDraft = null; walletStore.set(null);
    if (err === "wallet_unchanged") {       /* a twin click landed first — that is a success */
      await refreshAccount(false);
      setState("dash");
      errSlot("wallet-err", `saved — rewards will ride ${shortAddr(v)}`);
    } else {
      const msg = { invalid_solana_address: "that isn't shaped like a Solana address.",
                    wallet_rate_limited: "ease off — the wallet desk is resting.",
                    unsupported_media_type: "form posts need not apply.",
                    account_closed: "that account is closed." }[err]
                  || err || "the wallet desk blinked.";
      errSlot("wallet-err", msg);
    }
  } finally { walletBusy = false; }
}
async function sendFreshLink(addr) {
  /* the board asks the same anonymous door login rides, for the member's own
   * inbox — never the member. a deferral is truth (the link is still walking);
   * a mail refusal or a resting bucket speaks actionable words. */
  const v = addr || walletDraft;
  if (!v) return;
  try {
    const out = await postJoinWithRetry({ email: (((S.dash || {}).account) || {}).email }, post);
    walletStore.set({ addr: v, waiting: true });
    errSlot("wallet-err", out.deferred
      ? "the link is still walking — open it and we'll finish the save."
      : "a fresh magic link is on its way to your mail — open it and we'll finish the save.");
    armWalletWords();
    startWalletPoll(v);
  } catch (e) {
    const err = e.payload?.error;
    walletStore.set({ addr: v, waiting: false });
    errSlot("wallet-err", err === "mail_failed"
      ? "the mail door refused the send — click these words to ask again."
      : err === "magic_rate_limited"
        ? "the mail door is resting a minute — click these words to ask again."
        : "the mail door stumbled — click these words to ask again.");
    armWalletWords();
  }
}
function startWalletPoll(v) {
  /* the watch for the click: the account view speaks its own freshness; when
   * it flips true, the stowed draft commits — once, and no looping. */
  if (walletPoll) clearInterval(walletPoll);
  let ticks = 0;
  walletPoll = setInterval(async () => {
    ticks += 1;
    let fresh = false;
    try { fresh = (await get("/api/account")).sessionFresh === true; } catch (e) { fresh = false; }
    if (fresh) {
      clearInterval(walletPoll); walletPoll = null;
      walletBusy = true;
      try {
        const d = await post("/api/account/wallet", { wallet: v });
        walletDraft = null; walletStore.set(null);
        await refreshAccount(false); setState("dash");
        errSlot("wallet-err", `saved — rewards will ride ${shortAddr(d.wallet)}`);
        clearWalletWords();
      } catch (e) {
        if (e.payload?.error === "wallet_unchanged") {   /* a twin commit already landed */
          walletDraft = null; walletStore.set(null);
          await refreshAccount(false); setState("dash");
          errSlot("wallet-err", "saved — rewards will ride " + shortAddr(v));
          clearWalletWords();
        } else {
          walletStore.set({ addr: v, waiting: false });
          errSlot("wallet-err", "the save still waits — click these words to ask the door again.");
          armWalletWords();
        }
      } finally { walletBusy = false; }
    }
    else if (ticks >= 40) {                  /* ~two minutes: the mail is slow, not lost */
      clearInterval(walletPoll); walletPoll = null;
      walletStore.set({ addr: v, waiting: false });
      errSlot("wallet-err", "the mail is still walking — click these words when the link is open.");
      armWalletWords();
    }
  }, 3000);
}
function resumeWalletDraft() {
  /* the recoverable path: a reload, or a return from the mail app, finds the
   * desk holding its thought instead of a blank face. */
  const st = walletStore.get();
  if (!st) return;
  walletDraft = st.addr;
  const input = $("#pb-wallet-settings-input");
  if (input) input.value = st.addr;
  if (st.waiting) {
    errSlot("wallet-err", "still waiting on your fresh click — open the link and we'll finish the save.");
    armWalletWords(); startWalletPoll(st.addr);
  } else {
    errSlot("wallet-err", "a fresh click is asked for — click these words to ask again.");
    armWalletWords();
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
    else if (a === "open-wallet") {
      walletArmed = null;
      const save = $(`[data-action="change-wallet"]`);
      if (save) save.textContent = "Save";
      setState("wallet");
    }
    else if (a === "change-wallet") await doChangeWallet();
    else if (a === "copy-wallet") {
      /* the dash wears a mask; the button copies the whole address */
      const fb = $('[data-slot="wallet-copy"]');
      const w = ((S.dash || {}).account || {}).wallet || "";
      const say = (t) => { if (fb) fb.textContent = t; };
      if (!w) say("no wallet yet.");
      else if (navigator.clipboard?.writeText) navigator.clipboard.writeText(w)
        .then(() => say("the whole address is copied ✓"))
        .catch(() => say("copy it by hand — the clipboard said no."));
      else say("the clipboard is asleep here — copy it by hand.");
    }
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

/* ============================================================
   the artboard — the community wall (feature one). its own small
   corner of the app: one fetch, one delegated click lane, one form
   that speaks base64. the server is the authority on the day, the
   bytes, and the vote; this corner only ever asks.
   ============================================================ */

const ART_KEYS = {
  daily_cap: "three sheets a day — the wall resets at midnight UTC",
  upload_rate_limited: "ease off the door — too many hangs in ten minutes",
  bad_image_type: "paper must be png, jpg, gif, or webp — the bytes decide",
  bad_image_data: "the file came through empty",
  too_large: "that sheet is over five megabytes",
  bad_title: "name your sheet (up to 80 marks)",
  bad_credit: "the credit is too long",
  bad_note: "the note is too long",
  account_absent: "that account is gone from the ledger",
  not_authenticated: "sign in at the board first — the wall knows you by cookie",
  art_absent: "that paper already left the wall",
  not_owner: "only the author takes their own paper down",
  vote_rate_limited: "the likes can rest for a minute",
  store_failed: "the wall could not take the bytes (503)",
};

let abSort = "new";
let abRows = [];

const artEsc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function artSlotText(text) {
  const el = document.querySelector('[data-set="ab-state"]');
  if (el) el.textContent = text;
}

async function artFetch() {
  const j = await get(`/api/artworks?sort=${abSort}`);
  return j.artworks ?? [];
}

function artCard(r) {
  return `
    <figure class="art" data-art-id="${r.id}">
      <img class="art__frame" src="${artEsc(r.href)}" alt="${artEsc(r.title)}"
           loading="lazy" decoding="async">
      <figcaption class="art__cap">
        <span class="art__title">${artEsc(r.title)}</span>
        <span class="art__credit">${artEsc(r.credit || "anonymous")}</span>
        <button type="button" class="art__like" data-art-vote="${r.id}">
          like <span class="art__count">${r.likes}</span>
        </button>
      </figcaption>
    </figure>`;
}

function artRender() {
  const mount = document.getElementById("pb-art-wall");
  if (!mount) return;
  if (!abRows.length) {
    mount.innerHTML = `
      <div class="state">
        <div class="p-idle__art"><img src="${CONFIG.poses.silly}" alt="" aria-hidden="true"
             loading="lazy" decoding="async"></div>
        <p>the wall is bare — be the first to hang paper.</p>
      </div>`;
    artSlotText("the wall: none shown");
    return;
  }
  mount.innerHTML = `<div class="art-grid">${abRows.map(artCard).join("")}</div>`;
  artSlotText(`the wall: ${abRows.length} shown`);
}

async function artLoad() {
  try {
    abRows = await artFetch();
  } catch (err) {
    const mount = document.getElementById("pb-art-wall");
    if (mount) mount.innerHTML = `
      <div class="state"><p>the wall is offline — the server did not answer.</p></div>`;
    artSlotText("the wall: offline");
    return;
  }
  artRender();
}

function artB64(file) {                       /* one file, one promise */
  return new Promise((go, no) => {
    const fr = new FileReader();
    fr.onload = () => {
      const s = String(fr.result ?? "");
      go(s.slice(s.indexOf(",") + 1));       /* ride the data-url's tail */
    };
    fr.onerror = () => no(new Error("read_failed"));
    fr.readAsDataURL(file);
  });
}

export function startArtboard() {
  const mount = document.getElementById("pb-art-wall");
  const form = document.getElementById("pb-art-form");
  if (!mount) return Promise.resolve();

  document.querySelectorAll("[data-art-sort]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      abSort = btn.dataset.artSort;
      document.querySelectorAll("[data-art-sort]").forEach((b) =>
        b.classList.toggle("is-on", b === btn));
      await artLoad();
    });
  });

  mount.addEventListener("click", async (e) => {
    const btn = e.target.closest ? e.target.closest("[data-art-vote]") : null;
    if (!btn) return;
    try {
      const r = await post("/api/artwork/vote", { art_id: Number(btn.dataset.artVote) });
      const count = mount.querySelector(`[data-art-id="${btn.dataset.artVote}"] .art__count`);
      if (count && r.count !== undefined) count.textContent = r.count;
      if (r.deferred) {                      /* the doctrine on screen */
        btn.classList.add("is-defer");
        btn.title = "your like already rode this paper — deferred, not doubled";
      }
    } catch (err) {
      const why = (err.payload && err.payload.error) || "vote failed";
      verdict(ART_KEYS[why] || why, false, "ab-err");
    }
  });

  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const file = f.get("paper");
    if (!file || !file.size) { verdict("pick your paper first", false, "ab-err"); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const dataB64 = await artB64(file);
      await post("/api/artwork/start", {
        title: String(f.get("title") ?? "").trim(),
        credit: String(f.get("credit") ?? "").trim(),
        note: String(f.get("note") ?? "").trim(),
        data_b64: dataB64,
      });
      abRows = await artFetch();             /* refetch — order is the server's call */
      artRender();
      form.reset();
      verdict("paper hung. the wall remembers.", true, "ab-err");
    } catch (err) {
      const why = (err.payload && err.payload.error) || "hang failed";
      verdict(ART_KEYS[why] || why, false, "ab-err");
    } finally {
      btn.disabled = false;
    }
  });

  return artLoad();
}

/* ============================================================
   worldmap — where the paper comes from (feature two). the public
   face is aggregate only: counts by place over the real world,
   drawn by leaflet from openstreetmap's tiles (the old vendored
   file, js/world-land.js, rests on the shelf). this corner drives
   the map — one circle marker per resolved place, the first drawn
   crowd fits the frame once, pan by hand and zoom by wheel or by
   the two corners — and it lets a member set or drop their own
   seat. the counts are places, never people; no account id ever
   crosses the door.
   ============================================================ */

const WM_KEYS = {
  bad_country: "a country is the one word a seat needs (up to 60 marks)",
  bad_region: "that region is too long",
  bad_city: "that city is too long",
  account_absent: "that account is gone from the ledger",
  not_authenticated: "sign in at the board first — the seat knows you by cookie",
  no_location: "no seat to remove — the map never held one",
  location_rate_limited: "the map is patient — a few seat changes per ten minutes",
};

/* — the map's own state: one leaflet view, one layer of seats, and the
   crowd's words — the chips, the markers, and the slot all read the same. */
const WM = { map: null, tiles: null, layer: null, places: [], unplaced: 0,
             fitted: false, tilesQuiet: false };

function wmWords(p) {
  const bits = [p.city, p.region, p.country].filter(Boolean);
  return bits.length ? bits.join(", ") : "somewhere";
}



/* — leaflet: real ground under real counts. the mount keeps the id the svg
   wore; tile.openstreetmap.org serves the world (their usage policy asks
   modest traffic and a real attribution — the tile line below carries it),
   and seats are circleMarkers, so no marker image can break the way the
   studio's downloads once did across the split deployment origins. */

function wmMapInit() {
  if (WM.map || !window.L) return WM.map;
  const mount = document.getElementById("wm-map");
  if (!mount) return null;
  WM.map = L.map(mount, { worldCopyJump: true, minZoom: 2, maxZoom: 18,
                         scrollWheelZoom: true });
  /* the view goes first, always: every bounds call in leaflet is guarded by
     _checkIfLoaded, and _loaded is born in setView. P6X asked for the bounds
     first (a chained bounds ask at the chain's head) and leaflet threw its own
     "Set map center and zoom first." — which left the mount with no view,
     no tile layer, no marker layer, and no attribution, because the lines
     after the throw never ran and WM.map stood truthy for every later
     visit. the maxBounds still ride after the view, where leaflet is glad
     to enforce them. */
  WM.map.setView([20, 0], 2);
  WM.map.setMaxBounds(L.latLngBounds([[-85, -179], [85, 179]]));
  /* the tile layer keeps its own handle on purpose: the vendored 1.9.4
     fires "tileerror" from _tileReady ON THE LAYER (its <img> onerror rides
     _tileOnError) and that fire is not relayed to the map the way leaflet's
     docs led P6X to hope — so the quiet word binds the layer direct. when
     the tile host answers badly the status line says so once, and the map
     going pale stays a voice, never a silent fake. */
  WM.tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, minZoom: 0,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(WM.map);
  WM.tiles.on("tileerror", wmTileTrouble);
  WM.layer = L.layerGroup().addTo(WM.map);
  return WM.map;
}

function wmTileTrouble() {
  if (WM.tilesQuiet) return;
  WM.tilesQuiet = true;
  wmSlot("the map: the tiles are quiet — the counts below are true");
}

function wmDraw() {                                  /* the crowd, one layer */
  if (!wmMapInit()) return;                   /* no leaflet: the chips speak */
  WM.layer.clearLayers();
  const pts = [];
  for (const p of WM.places) {
    pts.push([p.lat, p.lon]);
    const seat = L.circleMarker([p.lat, p.lon], {
      radius: 5 + Math.min(14, Math.sqrt(p.count) * 3),
      className: "wm__seat",
    });
    seat.bindPopup(`${artEsc(wmWords(p))} — ${p.count} `
                   + `${p.count === 1 ? "seat" : "seats"} · ${artEsc(p.precision)}`);
    seat.addTo(WM.layer);
  }
  if (!WM.fitted && pts.length && WM.map.getContainer().clientWidth) {
    /* a box that is hidden measures zero and a fit there binds a nonsense
       view; the fold's hashchange ride refits the moment the box has width */
    WM.map.fitBounds(pts, { paddingTopLeft: [24, 24],
                            paddingBottomRight: [24, 24], maxZoom: 6 });
    WM.fitted = true;
  }
}

function wmSlot(text) {
  const el = document.querySelector('[data-set="wm-state"]');
  if (el) el.textContent = text;
}

async function wmCrowd() {
  const j = await get("/api/worldmap");
  return { countries: j.countries ?? [], total: j.total ?? 0,
           places: j.places ?? [], unplaced: j.unplaced ?? 0 };
}

async function wmSeat() {                    /* a seat, none, or the door */
  try {
    const j = await get("/api/account/location");
    return j.location ?? null;
  } catch (err) {
    return undefined;                        /* the door answered; no seat talk */
  }
}

function wmRender(crowd) {
  const mount = document.getElementById("pb-world");
  if (!mount) return;
  if (!crowd.countries.length) {
    mount.innerHTML = `
      <div class="state"><p>the map is bare — set a seat and it gets a dot.</p></div>`;
  } else {
    mount.innerHTML = `<div class="wm__chips">${crowd.countries.map((c) =>
      `<span class="wm__chip">${artEsc(c.country)} <b>${c.count}</b></span>`
    ).join("")}</div>`
    + (crowd.unplaced
       ? `<p class="wm__bare">and ${crowd.unplaced} ${crowd.unplaced === 1
          ? "seat sits" : "seats sit"} where the map can not place the words"
          — no invented pin.</p>`
       : "");
  }
  const n = crowd.countries.length;
  const t = crowd.total;
  wmSlot(`the map: ${n} ${n === 1 ? "place" : "places"}, ${t} ${t === 1 ? "seat" : "seats"}`);
  WM.places = crowd.places;
  WM.unplaced = crowd.unplaced;
  wmDraw();
}

async function wmLoad() {
  let crowd = null;
  try { crowd = await wmCrowd(); } catch { /* offline below */ }
  if (crowd === null) { wmSlot("the map: offline"); return; }
  wmRender(crowd);
  const seat = await wmSeat();
  const form = document.getElementById("pb-seat-form");
  if (form && seat) {
    form.elements.country.value = seat.country ?? "";
    form.elements.region.value = seat.region ?? "";
    form.elements.city.value = seat.city ?? "";
  }
}

export function startWorldMap() {
  const mount = document.getElementById("pb-world");
  const form = document.getElementById("pb-seat-form");
  if (!mount) return Promise.resolve();

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      const country = String(f.get("country") ?? "").trim();
      if (!country) { verdict("a seat needs a country", false, "wm-err"); return; }
      try {
        await post("/api/account/location", {
          country,
          region: String(f.get("region") ?? "").trim(),
          city: String(f.get("city") ?? "").trim(),
        });
        await wmLoad();
        verdict("seat set.", true, "wm-err");
      } catch (err) {
        const why = (err.payload && err.payload.error) || "seat failed";
        verdict(WM_KEYS[why] || why, false, "wm-err");
      }
    });
    const drop = document.getElementById("pb-seat-drop");
    if (drop) drop.addEventListener("click", async () => {
      try {
        await post("/api/account/location/remove", {});
        await wmLoad();
        verdict("seat dropped.", true, "wm-err");
      } catch (err) {
        const why = (err.payload && err.payload.error) || "drop failed";
        verdict(WM_KEYS[why] || why, false, "wm-err");
      }
    });
  }
  /* the fold that hides the map is display:none: a map born in a hidden fold
     is born at zero size, and the router's class flip fires no window resize.
     so when the router opens the fold the map must be told its box moved —
     the ride is deferred a breath because startNav registers its own
     hashchange after this one and the fold's class must land before the
     check reads it. an unopened map stays quiet. */
  window.addEventListener("hashchange", () => window.setTimeout(() => {
    if (!WM.map) return;
    const opened = document.querySelector('[data-view="worldmap"].is-view');
    if (!opened) return;
    WM.map.invalidateSize();
    if (!WM.fitted && WM.places.length) wmDraw();
  }, 0));

  return wmLoad();
}

/* ============================================================
   arcade — the games floor (feature three). the shell reads the
   floor from the server (config's own words), paints the tiles,
   and asks each game's board for the week the floor named. an
   empty board is the truth of the shell, never a stub.
   ============================================================ */

function arSlot(text) {
  const el = document.querySelector('[data-set="ar-week"]');
  if (el) el.textContent = text;
}

const AR_KEYS = {
  unknown_game: "that game is not on the floor",
  bad_score: "the score must be a whole number, 0 to 100000",
  not_authenticated: "sign in at the board first — the score knows you by cookie",
  score_rate_limited: "the ledger is patient — a dozen scores per ten minutes",
};

export function startArcade() {
  const mount = document.getElementById("pb-arcade");
  const form = document.getElementById("pb-score-form");
  if (!mount) return Promise.resolve();

  async function refreshFloor() {
    let floor = null;
    try { floor = await get("/api/arcade"); } catch { /* offline below */ }
    if (!floor) {
      mount.innerHTML = `<div class="state"><p>the arcade is offline — the server did not answer.</p></div>`;
      arSlot("the arcade: offline");
      return null;
    }
    arSlot(`the week: ${floor.week} — shuts Friday midnight UTC`);
    const games = floor.games ?? [];
    mount.innerHTML = `
      <div class="ar__tiles">
        ${games.map((g) => `
          <div class="ar__tile" data-ar-game="${artEsc(g.id)}">
            <p class="ar__tile-state">${artEsc(g.state ?? "pending")}</p>
            <h3 class="ar__tile-name">${artEsc(g.name)}</h3>
            <p class="ar__blurb">${artEsc(g.blurb ?? "")}</p>
          </div>`).join("")}
      </div>
      <div class="ar__boards">
        ${games.map((g) => `
          <div class="ar__board" data-ar-board="${artEsc(g.id)}">
            <p class="ar__board-name">${artEsc(g.name)} — this week</p>
            <div class="state"><p>…</p></div>
          </div>`).join("")}
      </div>`;
    await Promise.all(games.map(async (g) => {
      const host = mount.querySelector(`[data-ar-board="${g.id}"]`);
      if (!host) return;
      let rows = null;
      try {
        const j = await get(`/api/arcade/leaderboard?game=${g.id}&week=${floor.week}`);
        rows = j.rows ?? [];
      } catch { rows = null; }
      if (rows === null) {
        host.querySelector(".state p").textContent = "the board is offline";
        return;
      }
      if (!rows.length) {
        host.querySelector(".state p").textContent = "no scores yet — the week is young.";
        return;
      }
      host.querySelector(".state").innerHTML = `<div class="ar__rows">${
        rows.map((b) => `
          <div class="ar__row">
            <span class="ar__rank">${b.rank}</span>
            <span class="ar__handle">${artEsc(b.handle)}</span>
            <b class="ar__score">${b.score}</b>
          </div>`).join("")}</div>`;
    }));
    return floor;
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      const game = String(f.get("game") ?? "");
      const raw = String(f.get("score") ?? "").trim();
      const score = Number(raw);
      if (!game) { verdict("pick the game the score belongs to", false, "ar-err"); return; }
      if (raw === "" || !Number.isInteger(score) || score < 0) {
        verdict("the score must be a whole number of marks", false, "ar-err");
        return;
      }
      try {
        const j = await post("/api/arcade/score", { game, score });
        await refreshFloor();
        const r = j.recorded || {};
        verdict(j.deferred ? `your word stood — the week keeps its best (${r.score}).`
              : j.replaced ? `booked — you bettered your week (${r.score}).`
              : `booked — the week has your word (${r.score}).`, true, "ar-err");
      } catch (err) {
        const why = (err.payload && err.payload.error) || "score failed";
        verdict(AR_KEYS[why] || why, false, "ar-err");
      }
    });
  }

  return refreshFloor().then((floor) => {
    if (!form || !floor) return;
    const sel = form.elements.game;
    sel.innerHTML = `<option value="">— pick a game —</option>`
      + (floor.games ?? []).map((g) =>
          `<option value="${artEsc(g.id)}">${artEsc(g.name)}</option>`).join("");
  });
}

/* ============================================================
   the press sheet (the redesign's masthead) — four doors, all real:
   the board's top three, the wall's freshest prints, the crowd's
   chips, and the arcade's week. a dead door says so in one line;
   nothing on this sheet was typed in by hand.
   ============================================================ */

function dashSlot(text) {
  const el = document.querySelector('[data-set="dash-state"]');
  if (el) el.textContent = text;
}

function dashSilence(sel, line, tag) {
  const el = document.querySelector(sel);
  if (el) el.innerHTML = `<${tag} class="dash__hush">${line}</${tag}>`;
}

export function startRun() {
  /* the print is static by design; the one lane that can fail silently is
     the frame itself — a refused embed never raises a visible error. so:
     the load event clears the watch, and a slow rail says so. nothing here
     reads *into* the game (cross-origin can't), and nothing awards points:
     a score is an event to be earned later, not a client's promise. */
  const frame = document.getElementById("pb-run-frame");
  const down = document.getElementById("pb-run-down");
  if (!frame || !down) return;
  let answered = false;
  const watch = window.setTimeout(() => {
    if (!answered) down.hidden = false;
  }, 9000);
  frame.addEventListener("load", () => {
    answered = true;
    window.clearTimeout(watch);
  });
}

export function startHomeDash() {
  const section = document.getElementById("home");
  if (!section) return Promise.resolve();
  let answered = 0;
  const tally = (alive) => {
    if (alive) answered += 1;
    dashSlot(`the press: ${answered} of 4 doors answered`);
  };

  get("/api/leaderboard").then((j) => {
    const rows = (j && j.rows) || [];
    const el = document.querySelector('[data-slot="dash-board"]');
    if (el) {
      el.innerHTML = rows.length
        ? rows.slice(0, 3).map((r, i) => `
            <li class="dash__row">
              <span class="dash__rank">${i + 1}</span>
              <b>${artEsc(r.username || "unnamed")}</b>
              <span class="dash__pts">${fmt(r.points, 1)}</span>
            </li>`).join("")
        : `<li class="dash__hush">the board is empty — the first byline is unwritten.</li>`;
    }
    tally(true);
  }, () => { dashSilence('[data-slot="dash-board"]', "the board is offline", "li"); tally(false); });

  get("/api/artworks").then((j) => {
    const items = (j && j.artworks) || [];
    const el = document.querySelector('[data-slot="dash-art"]');
    if (el) {
      el.innerHTML = items.length
        ? items.slice(0, 4).map((it) => `
            <a class="dash__thumb" href="#artboard">
              <img src="${artEsc(it.href)}" alt="${artEsc(it.title)}" loading="lazy" decoding="async">
              <span>${artEsc(it.title)}${it.credit ? ` · ${artEsc(it.credit)}` : ""}</span>
            </a>`).join("")
        : `<p class="dash__hush">the wall is bare — the first print is unwung.</p>`;
    }
    tally(true);
  }, () => { dashSilence('[data-slot="dash-art"]', "the wall is offline", "p"); tally(false); });

  get("/api/worldmap").then((j) => {
    const el = document.querySelector('[data-slot="dash-map"]');
    if (el) {
      const cs = (j && j.countries) || [];
      el.innerHTML = cs.length
        ? `<p class="wm__chips">${cs.slice(0, 3).map((c) =>
            `<span class="wm__chip">${artEsc(c.country)} <b>${c.count}</b></span>`).join("")}</p>
           <p class="dash__hush">${j.total} ${j.total === 1 ? "seat" : "seats"} on the map</p>`
        : `<p class="dash__hush">no seats yet — the map is waiting.</p>`;
    }
    tally(true);
  }, () => { dashSilence('[data-slot="dash-map"]', "the map is offline", "p"); tally(false); });

  get("/api/arcade").then((j) => {
    const el = document.querySelector('[data-slot="dash-arcade"]');
    if (el) {
      const games = (j && j.games) || [];
      el.innerHTML = `<p class="dash__hush">${games.length} game${games.length === 1 ? "" : "s"} on the floor — the week closes ${artEsc(j.week)} at Friday midnight.</p>`;
    }
    tally(true);
  }, () => { dashSilence('[data-slot="dash-arcade"]', "the arcade is offline", "p"); tally(false); });

  return Promise.resolve();
}
