/* ============================================================
   PAPERBOARD — the app.
   a small state machine on top of a server that holds all the
   authority. the frontend only ever sends: an opaque code, a
   public wallet address, and its own impatience.
   ============================================================ */

import { CONFIG } from "./config.js";

const API = (p) => (CONFIG.board.base || "") + p;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const panel = () => $("#pb-panel");
const boardEl = () => $("#pb-board");

export let S = {                       /* app state (client-side facts only) */
  mode: null, cfg: null, me: null, board: null, comps: null,
  breakdown: null, breakdownLoaded: false,
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

/* — boot: config → me → leaderboard (server is the source of truth) — */
export async function boot() {
  try {
    const cfg = await get("/api/config");
    S.cfg = cfg; S.mode = cfg.mode;
    paintConfig(cfg);
    get("/api/competitions").then((j) => { S.comps = j; paintCompetitions(); })
      .catch(() => {});
    const meRes = await get("/api/me").catch(() => null);
    S.me = meRes;
    const lb = await get("/api/leaderboard").catch((e) => (e.status === 503 || e.status === 404 ? null : null));
    S.board = lb;
    paintBoard();
    await resolveMe();
  } catch (e) {
    console.warn('[pb] boot caught:', e && e.constructor && e.constructor.name, (e||{}).message, (e||{}).stack ? String(e.stack).split(String.fromCharCode(10))[1] : '');
    if (e instanceof TypeError) {            /* network down — off press */
      S.mode = "off"; setMode("off press", "stamp--rot");
      setState("off"); paintBoard();
    }
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

/* — the front page table — */
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
  const you = S.me && S.me.connected && S.me.handle;
  const rows = b.rows.map((r) => {
    const mv = r.movement == null || r.movement === 0 ? ""
      : `<span class="mv ${r.movement > 0 ? "up" : "down"}">${r.movement > 0 ? "▲" : "▼"}${Math.abs(r.movement)}</span>`;
    return `
    <tr class="${r.rank === 1 ? "tr-front" : (r.rank <= 3 ? "tr-head" : "")} ${you && r.user.handle === you ? "is-you" : ""}">
      <td class="cell-rank rk">${r.rank}${mv}</td>
      <td class="cell-name nm">
        <a class="nm__handle" href="https://x.com/${r.user.handle}" target="_blank" rel="noopener noreferrer">@${r.user.handle}</a>
        ${r.user.handle === you ? `<span class="nm__sub" style="color:var(--green)"> · that's you</span>` : ""}
      </td>
      <td class="num num--pts" data-label="Points">${fmt(r.points, 1)}</td>
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
const tierTone = (label) => label === "FRONT PAGE" ? "gold" : label === "HEADLINE" ? "silver" : "print";

function setFPFoot(b, demo) {
  const gen = $('[data-set="fp-generated"]'), tot = $('[data-set="fp-totals"]'),
        st = $('[data-set="fp-state"]');
  if (gen) gen.textContent = demo
    ? "snapshot: — (server offline)"
    : `snapshot: generated ${fmtTime(b.generated_at)} · next ~${fmtTime(b.next_refresh_at)}`;
  if (tot && !demo) tot.textContent = `${fmt(b.participants)} participants · ${fmt(b.total_points)} points on the board`;
  if (st) st.textContent = `state: ${(b && b.state) ?? (demo ? "demo" : "—")}`;
}

/* mini preview inside the board panel */

/* — resolve me → decide the panel state — */
async function resolveMe() {
  const me = S.me;
  if (!me || !me.connected) { setState("idle"); return; }
  if (me.competition_state && me.competition_state !== "live") {
    S.lastState = "result"; paintResult(); await loadBreakdown(false); renderBreakdown();
    setState("result"); paintResult(true); return;
  }
  if (!me.wallet) { setState("wallet"); return; }
  if (me.points > 0) { setState("result"); paintResult(); await loadBreakdown(false); renderBreakdown(); }
  else if (me.scanned) setState("none");
  else await doScan();
}

/* — the me → result rendering — */
function paintResult(isFinal = true) {
  const me = S.me; if (!me) return;
  const pts = $('[data-slot="points"]');
  if (pts) {
    const to = me.points || 0;
    if (reduced() || !isFinal) pts.textContent = fmt(to, 1);
    else countUp(pts, to);
  }
  const unit = S.cfg?.prize?.unit || S.cfg?.prize?.type || "";
  const slot = $('[data-slot="metrics"]');
  if (slot) slot.innerHTML = `
    <div class="metric"><span class="metric__k">Qualifying posts</span><span class="metric__v">${fmt(me.qualifying_posts)}</span></div>
    <div class="metric"><span class="metric__k">Rank</span><span class="metric__v">${me.rank ?? '<span class="u">next snapshot</span>'}</span></div>
    <div class="metric"><span class="metric__k">Receipts (L+R+RP+Q)</span><span class="metric__v">${fmt(me.engagement)}</span></div>
    <div class="metric"><span class="metric__k">Est. share</span><span class="metric__v">${me.share_pct ? me.share_pct.toFixed(2) + "%" : "—"} <span class="u">${fmt(me.share_est)} ${unit}</span></span></div>`;
  const st = $('[data-slot="status"]');
  if (st) {
    const followChip = me.follows_paper
      ? '<span class="tag" style="color:var(--green);border-color:var(--green-soft)">following @paperusdc</span>'
      : '<span class="tag" style="color:var(--tan);border-color:var(--tan)">not following — points paused</span>';
    const stateChip = { live: "COMPETITION LIVE", upcoming: "OPENS SOON", ended: "PRINT CLOSED", closed: "ARCHIVED" }[me.competition_state] || "—";
    st.innerHTML = `<span>${S.me.handle ? "@" + S.me.handle : ""}</span> ${followChip} <span>${stateChip}</span>`;
  }
  /* you-note rides the full table (is-you) — the mini lane is gone */
}

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

/* — the breakdown (server's audit trail, rendered, never re-divined) — */
async function loadBreakdown(refresh = true) {
  if (!S.me?.connected) return;
  if (S.breakdownLoaded && !refresh) return;
  try {
    const j = await get("/api/me/posts");
    S.breakdown = j.posts; S.breakdownLoaded = true;
  } catch { S.breakdown = []; }
}
function renderBreakdown() {
  const mount = $('[data-slot="breakdown"]'); if (!mount) return;
  const posts = S.breakdown || [];
  mount.innerHTML = posts.length ? posts.map((p) => {
    const c = p.audit?.contributions || {};
    const flags = [];
    if (p.matched) flags.push(`<span class="tag bd-flag">${p.matched === CONFIG.contractAddress ? "CA" : p.matched}${p.followed ? " · following" : ""}</span>`);
    if (p.reason && p.reason !== "ok") flags.push(`<span class="tag tag--off bd-flag">${p.reason.replace("_", " ")}</span>`);
    (p.audit?.applied || []).forEach((a) => flags.push(`<span class="tag tag--off bd-flag">${a.replace("_", " ")}</span>`));
    return `
    <article class="bd-post ${p.points ? "" : "is-no"}">
      <div class="bd-post__head">
        <p class="bd-post__text"><a href="${p.url}" target="_blank" rel="noopener noreferrer">${esc(p.text)}</a></p>
        <span class="bd-post__pts">${fmt(p.points, 1)} pts</span>
      </div>
      <dl class="bd-grid">
        ${["impressions", "likes", "replies", "reposts", "quotes"].map((k) =>
          `<div class="bd-cell"><dt>${k}</dt><dd>${fmt(p.metrics[k])}${c && (c[k] || c[k === "impressions" ? "impressions" : k]) ? ` <span class="u" style="color:var(--green)">+${fmt(c[k === "impressions" ? "impressions" : k])}</span>` : ""}</dd></div>`).join("")}
        <div class="bd-cell"><dt>posted</dt><dd>${fmtTime(p.posted_at)}</dd></div>
        ${!p.points && p.reason !== "ok" ? `<div class="bd-cell"><dt>verdict</dt><dd class="neg">${p.followed ? "no identifier" : "no follow"}</dd></div>` : ""}
      </dl>
      <div class="bd-audit">${flags.join("")}</div>
    </article>`;
  }).join("") : `<p class="board__lede">Nothing on the wire yet. post about PAPER and scan again.</p>`;
}
const esc = (s) => (s || "").replace(/[&<>"`]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "`": "&#96;" }[c]));

/* — actions — */
async function doScan() {
  setState("scan");
  try {
    const j = await post("/api/me/scan");
    S.me = { connected: true, ...strip(j) };
    S.board = await get("/api/leaderboard").catch(() => S.board);
    paintBoard();
    if (S.me.qualifying_posts > 0 && S.me.points > 0) {
      setState("result"); paintResult(); await loadBreakdown(true); renderBreakdown();
    } else setState("none");
  } catch { setState("err"); $('[data-slot="err"]').textContent = "the scanner bailed (429 rate limit or X trouble). one more go?"; }
}
const strip = (j) => { const { scan, ...rest } = j; return rest; };

async function connect() {
  const p = panel(); p.dataset.state = "connect";
  try {
    const j = await post("/api/auth/x/start");
    if (j.mock) { await post(j.url); window.location.reload(); }
    else window.location.href = j.url;
  } catch { setState("err"); $('[data-slot="err"]').textContent = "X start refused: " + (await get("/api/health").catch(() => ({}))).error || "server off"; }
}

function wire() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest?.("[data-action]"); if (!btn) return;
    const a = btn.dataset.action;
    if (a === "connect") connect();
    else if (a === "mock-login") connect();
    else if (a === "logout") { try { await post("/api/auth/x/logout"); } catch {} window.location.reload(); }
    else if (a === "save-wallet") {
      const input = $("#pb-wallet-input"), v = input.value.trim();
      const j = await post("/api/me/wallet", { wallet: v }).catch(async (er) => {
        input.style.borderColor = "var(--tan)";
        setTimeout(() => input.style.borderColor = "", 1600);
        return { error: er.payload?.error || "bad" };
      });
      if (j?.error) return;
      await doScan();
    }
    else if (a === "rescan") { await resolveMe().catch(() => setState("idle")); }
    else if (a === "toggle-breakdown") {
      const box = $("#pb-breakdown"), open = !box.hidden;
      if (!open && !S.breakdownLoaded) { await loadBreakdown(true); renderBreakdown(); }
      box.hidden = open;
      btn.setAttribute("aria-expanded", String(!open));
      btn.innerHTML = (open ? "View scoring breakdown" : "Hide breakdown") + " " + ICONS_CHEVRON;
    }
  });
  /* hero CTA: scroll to the board and kick a connect if idle */
  $$("[data-hero-connect]").forEach((a) => a.addEventListener("click", () => {
    if (!S.me?.connected) setTimeout(connect, 400);
  }));
}
const ICONS_CHEVRON = '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

export function startPaperboard() {
  wire();
  return boot();
}
