/* ============================================================
   PAPER Studio — the desk.
   one module, zero dependencies: it reads what the server says it
   can do, speaks the brief, paints the plan and the card shelf,
   and lets a single card answer for itself. the server holds all
   the authority — this file only ever sends a brief and a click.
   ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);

const esc = (s) => (s || "").replace(/[&<>"`]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "`": "&#96;" }[c]));

const ERRORS = {
  bad_project: "the project name came back blank or too long",
  bad_brief: "the brief came back blank or too long",
  bad_days: "days is a whole number between one and the cap",
  bad_types: "pick at least one card, and only cards the desk knows",
  bad_image_data: "that reference is not clean base64",
  bad_image_type: "that reference is not a png, jpeg, gif, or webp",
  too_large: "that reference is heavier than the door allows",
  unsupported_png: "that png is deeper than the stdlib shelf can open",
  studio_store_failed: "the bytes would not land — try again",
  studio_rate_limited: "the studio door is catching its breath — try in a bit",
  studio_write_rate_limited: "the studio door is catching its breath — try in a bit",
  campaign_absent: "that campaign is not on the shelf",
  asset_absent: "that card is not on this campaign",
  bad_id: "the desk could not read that number",
};

/* — the copy lane: P6V copy seam begin -- */
function copyText(text, onOK, onFail) {
  const body = String(text === null || text === undefined ? "" : text);
  const fallback = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = body;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-99em";
      document.body.appendChild(ta);
      ta.select();
      const cut = document.execCommand && document.execCommand("copy");
      document.body.removeChild(ta);
      if (cut) { onOK("Copied"); return "legacy"; }
      onFail("Copy failed");
    } catch (e) { onFail("Copy failed"); }
    return "legacy";
  };
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard
        && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(body).then(() => onOK("Copied"), fallback);
      return "clipboard";
    }
    if (typeof document !== "undefined" && document.execCommand) return fallback();
    onFail("Copy unavailable");
  } catch (e) { onFail("Copy failed"); }
  return "none";
}
/* -- P6V copy seam end — the battery may drive the lane between the markers */

async function get(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status });
  return j;
}
async function post(path, body) {
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status });
  return j;
}

const state = { caps: null, campaign: null };

const el = {
  cap: $("#st-cap"), form: $("#st-form"), out: $("#st-out"), stage: $("#st-stage"),
  status: $("#st-status"), err: $("#st-err"),
};

function showErr(text) {
  el.err.hidden = !text;
  el.err.textContent = text || "";
}

function speak(err) {
  return ERRORS[err?.message] || "the door answered: " + (err?.message || "?");
}

function fileToDataUrl(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result || ""));
    fr.onerror = () => rej(new Error("unreadable_file"));
    fr.readAsDataURL(file);
  });
}

/* — boot: what genuinely answers here? — */
async function boot() {
  try {
    state.caps = await get("/api/studio");
  } catch (e) {
    /* a desk that cannot reach its server must say so — silent boot is how
       a deployed tier that does not know the studio routes masquerades as
       a live page until the first submit (the 502 lesson, P6U). */
    el.cap.hidden = false;
    el.cap.innerHTML = `<span class="st-chip">the desk cannot reach the`
      + ` server — the door answered: ${esc(e && e.message || "?")}</span>`;
    return;
  }
  const c = state.caps;
  el.cap.hidden = false;
  el.cap.innerHTML = `
    <span class="st-chip">planner <b>${esc(c.planner)}</b></span>
    <span class="st-chip">image hero <b>${esc(c.imageGen)}</b></span>
    <span class="st-chip">up to <b>${esc(String(c.limits.maxAssets))}</b> cards</span>
    <span class="st-chip">reference ≤ <b>${(c.limits.maxRefBytes / 1000000).toFixed(1)} mb</b></span>`;
  const days = $('input[name="days"]', el.form);
  if (days) {
    days.value = String(c.limits.daysDefault || 7);
    days.max = String(c.limits.daysMax || 14);
  }
}

/* — the human face of the formats: a board reader reads "square", not "1:1" — */
const ASPECT_WORDS = {
  "16:9": "wide 16:9", "1:1": "square", "4:5": "portrait 4:5",
  "3:1": "marquee 3:1", "8:5": "card 8:5", "as provided": "as sent",
};

/* — the lightbox: one dialog, built the first time a preview is asked — */
function stLb() {
  let lb = document.getElementById("st-lb");
  if (!lb) {
    lb = document.createElement("div");
    lb.id = "st-lb";
    lb.className = "st-lb";
    lb.hidden = true;
    lb.setAttribute("role", "dialog");
    lb.setAttribute("aria-modal", "true");
    lb.setAttribute("aria-label", "the image, large");
    lb.innerHTML = `<div class="st-lb__box"><img alt=""><p class="st-lb__cap"></p></div>`
      + `<button type="button" class="btn st-lb__x" aria-label="close the large image">close ✕</button>`;
    (document.querySelector(".studio") || document.body).appendChild(lb);
    lb.addEventListener("click", (e) => {
      if (e.target === lb || e.target.closest?.(".st-lb__x")) lb.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") lb.hidden = true;
    });
  }
  return lb;
}

function stLbOpen(href, cap) {
  const lb = stLb();
  lb.querySelector("img").src = href;
  lb.querySelector("img").alt = cap || "the campaign image";
  lb.querySelector(".st-lb__cap").textContent = cap || "";
  lb.hidden = false;
}

/* — the campaign painting — */
function renderCampaign(c) {
  state.campaign = c;
  el.stage.textContent = "step five · review";
  const plan = c.plan || {};
  const qc = c.qc || {};
  const days = plan.days || [];
  const kindInfo = {};
  ((state.caps && state.caps.types) || []).forEach((t) => { kindInfo[t.id] = t; });
  const byDay = {};
  (c.assets || []).forEach((a) => { (byDay[a.day] = byDay[a.day] || []).push(a); });
  const claimed = {};
  const pad = (n) => String(n).padStart(2, "0");
  const slug = c.slug || "campaign";
  const postsOf = (d) => (d.posts && d.posts.length ? d.posts : [{
    id: "d" + pad(d.day) + "-p01", purpose: d.purpose,
    concept: d.concept, caption: d.caption, cta: d.cta, notes: "",
  }]);

  /* the post's own media: the right bytes, honestly labelled, large enough
     to judge before anything is downloaded. a day's second post of the same
     kind is the validator's business (one card per kind per day); the desk
     simply asks for the kind it was promised and says text-only out loud. */
  const mediaOf = (p, d) => {
    const pool = byDay[d.day] || [];
    const a = p.kind ? pool.find((x) => x.kind === p.kind && !claimed[x.id])
                     : pool.find((x) => !claimed[x.id]);
    if (!a) return `<span class="st-chip">text-only</span>`;
    claimed[a.id] = 1;
    if (a.status !== "ready" || !a.href)
      return `<span class="st-chip st-chip--state">image ${esc(a.status)}</span>`
        + (a.note ? ` <span class="st-card__note">${esc(a.note)}</span>` : "");
    const ext = a.href.slice(a.href.lastIndexOf(".") + 1) || "png";
    const mm = pad(Number(String(p.id || "").slice(-2)) || 1);
    const stem = `${slug}-day-${pad(d.day)}-post-${mm}`;
    const info = p.kind && kindInfo[p.kind] ? kindInfo[p.kind] : null;
    const fmt = info
      ? `${info.use} · ${ASPECT_WORDS[info.aspect] || info.aspect}`
      : (a.use || "");
    return `<figure class="st-media is-load">
      <button type="button" class="st-media__zoom" data-lb="${esc(a.href)}"
        data-lb-cap="${esc(`${a.use} — day ${d.day} of ${c.name || "the campaign"}`)}"
        aria-label="see the image large">
        <img src="${esc(a.href)}" alt="${esc(`${a.use} card for day ${d.day}`)}" loading="lazy">
      </button>
      <figcaption class="st-media__meta"><span class="st-post__meta">${esc(fmt)}</span>
        <span class="st-media__wait">the image is coming…</span></figcaption>
      <div class="st-media__acts">
        <a class="btn btn--ghost st-dl" href="${esc(`${a.href}?name=${stem}`)}"
           download="${esc(`${stem}.${ext}`)}">download</a>
      </div>
    </figure>`;
  };

  const postCard = (p, d) => {
    const info = p.kind && kindInfo[p.kind] ? kindInfo[p.kind] : null;
    return `
      <article class="st-post">
        <div class="st-post__top">
          ${info ? `<b>${esc(info.use)}</b><span>${esc(ASPECT_WORDS[info.aspect] || info.aspect)}</span>`
                 : `<span>text-only</span><span>${esc(p.purpose || "")}</span>`}
        </div>
        <p class="st-post__cap">${esc(p.caption || "")}</p>
        <div class="st-post__row">
          ${p.cta ? `<span class="st-day__cta">${esc(p.cta)}</span>`
                  : `<span class="st-post__rest">no call today — the post stands on its own</span>`}
          <button class="btn btn--pill st-copy" type="button"
            data-copy-cap="${esc(p.caption || "")}" data-copy-cta="${esc(p.cta || "")}">copy</button>
        </div>
        ${mediaOf(p, d)}
        ${p.concept ? `<p class="st-post__idea">${esc(p.concept)}${p.notes ? ` — ${esc(p.notes)}` : ""}</p>` : ""}
      </article>`;
  };

  const dayHtml = (d) => `
    <div class="st-day">
      <div class="st-day__top"><b>day ${pad(d.day)}</b>
        <span class="st-day__theme">${esc(d.theme || d.purpose || "")}</span>
        <span>${esc(d.platform || "X")}</span></div>
      ${d.why ? `<p class="st-day__why">${esc(d.why)}</p>` : ""}
      <div class="st-posts">${postsOf(d).map((p) => postCard(p, d)).join("")}</div>
    </div>`;

  const ovRow = (label, val) => (val
    ? `<div class="st-ov__row"><i>${esc(label)}</i><span>${esc(val)}</span></div>` : "");
  const overview = `
    <div class="st-ov">
      <h3>${esc(c.name || "campaign")} — ${esc(String(qc.days || days.length))} days, one run</h3>
      ${ovRow("the concept", plan.core_message)}
      ${ovRow("the tension", plan.insight)}
      ${ovRow("the promise", plan.promise)}
      ${ovRow("objective", plan.objective)}
      ${ovRow("audience", plan.audience)}
      ${ovRow("the tone", plan.tone)}
      ${ovRow("what we ask", plan.action)}
      ${ovRow("when it works", plan.success)}
      ${ovRow("the mix", plan.mix)}
      ${ovRow("strategy", plan.strategy)}
      ${ovRow("outcome", plan.outcome)}
      ${ovRow("the arc", plan.arc)}
    </div>`;

  const roles = (c.brand && c.brand.roles) || {};
  const swatch = (name) => roles[name]
    ? `<span class="st-role"><i style="--sw: rgb(${roles[name].join(",")})"></i>${esc(name)}</span>` : "";
  const brand = `
    <div class="st-swatch">${["paper", "press", "accent"].map(swatch).join("")}</div>
    ${(c.brand?.notes || []).map((n) => `<p class="st-note">${esc(n)}</p>`).join("")}`;

  const slotOf = (a) => {
    const d = days[a.day - 1];
    if (!d || !d.posts || !d.posts.length || !a.kind) return null;
    const i = d.posts.findIndex((p) => p.kind === a.kind);
    return i < 0 ? null : pad(i + 1);
  };
  const cards = (c.assets || []).map((a) => {
    const mm = slotOf(a);
    return `
    <article class="st-card" data-asset="${esc(String(a.id))}">
      <img src="${esc(a.href || "")}" alt="${esc(`${a.use} card for day ${a.day}`)}" loading="lazy">
      <div class="st-card__meta">
        <div class="st-card__name">day ${esc(pad(a.day))} · ${esc(a.use || "card")}${mm ? ` · post ${esc(mm)}` : ""}</div>
        <div class="st-card__file">${esc(a.href ? a.href.split("/").pop() : "no file")}</div>
        <div class="st-card__dims">${a.width && a.height ? esc(`${a.width}×${a.height}`) : "—"} · PNG
          ${a.note ? ` · <span class="st-card__note">${esc(a.note)}</span>` : ""}</div>
      </div>
      <div class="st-card__row">
        <span class="st-state ${a.status === "ready" ? "" : "st-state--failed"}">${esc(a.status)}</span>
        <div class="st-card__acts">
          ${a.href && a.status === "ready"
            ? `<a class="btn btn--ghost" href="${esc(`${a.href}?name=${slug}-day-${pad(a.day)}-card`)}" download>get</a>` : ""}
          ${a.kind === "ai_hero" ? "" : `<button class="btn btn--pill st-regen" type="button" data-regen="${esc(String(a.id))}">reroll</button>`}
        </div>
      </div>
    </article>`; }).join("");

  el.out.innerHTML = `
    <section class="panel st-sec">
      <div class="panel__head"><h2>the campaign · ${esc(String(qc.days || days.length))} days</h2>
        <span class="st-stage">${esc(c.engine)}</span></div>
      <div class="panel__body">
        ${overview}
        <div class="st-days">${days.map(dayHtml).join("")}</div>
      </div>
    </section>
    <section class="panel st-sec">
      <div class="panel__head"><h2>the brand read</h2>
        <span class="st-stage">${esc(c.brand?.source || "")}</span></div>
      <div class="panel__body">${brand}
        ${c.ref_href ? `<p class="st-note">reference on the shelf:
          <a href="${esc(c.ref_href)}" download>the file itself</a></p>` : ""}
      </div>
    </section>
    <section class="panel st-sec">
      <div class="panel__head"><h2>the cards</h2>
        <span class="st-stage">${esc(String(qc.assets_ready || 0))} of ${esc(String(qc.assets_total || 0))} ready</span></div>
      <div class="panel__body">
        <details class="st-shelfbox"><summary>the card shelf — ${esc(String((c.assets || []).length))} named files</summary>
          <div class="st-shelf">${cards || `<p class="st-hush">none shipped — the brief spoke, the pen did not.</p>`}</div>
        </details>
        <div class="st-acts">
          <span class="st-qc">verified sizes: <b>${esc(String(qc.sizes_verified || 0))}</b>
            · distinct concepts: <b>${esc(String(qc.concepts_distinct || 0))}</b>/${esc(String(qc.days || 0))}</span>
          <a class="btn btn--solid" href="/api/studio/export?id=${esc(String(c.id))}" download>campaign .zip</a>
        </div>
        ${(qc.notes || []).length ? `<p class="st-note">${esc(qc.notes.join(" · "))}</p>` : ""}
      </div>
    </section>`;
  el.status.hidden = false;
  el.status.textContent = `campaign #${c.id} · ${qc.assets_ready ?? 0}/${qc.assets_total ?? 0} cards`;
}

/* — the one verb with a noun: reroll one card — */
async function reroll(assetId, btn) {
  if (!state.campaign) return;
  btn.disabled = true;
  try {
    const out = await post("/api/studio/regenerate",
      { campaign: state.campaign.id, asset: Number(assetId) });
    renderCampaign(out.campaign);
  } catch (e) {
    showErr(speak(e));
    btn.disabled = false;
  }
}

/* — submit: the brief rides as JSON; the reference rides as base64 — */
async function runBrief(ev) {
  ev.preventDefault();
  const fd = new FormData(el.form);
  const types = fd.getAll("types").filter(Boolean);
  if (!types.length) { showErr("pick at least one card"); return; }
  const body = {
    project: String(fd.get("project") || "").trim(),
    brief: String(fd.get("brief") || "").trim(),
    goals: String(fd.get("goals") || "").trim(),
    directives: String(fd.get("directives") || "").trim(),
    days: parseInt(String(fd.get("days") || ""), 10),
    types,
  };
  if (!body.project) { showErr("the project needs a name"); return; }
  if (!body.brief) { showErr("the brief needs words"); return; }
  const file = $("input[name=ref]", el.form).files?.[0];
  const go = $("button[type=submit]", el.form);
  go.disabled = true;      /* one campaign per click — the busy button is the wall */
  showErr("");
  el.status.hidden = false;
  el.status.textContent = "the pen is moving…";
  el.stage.textContent = "working";
  try {
    if (file) body.ref_b64 = await fileToDataUrl(file);
    const out = await post("/api/studio/campaigns", body);
    renderCampaign(out.campaign);
  } catch (e) {
    showErr(speak(e));
    el.status.textContent = "not shipped";
    el.stage.textContent = "step one";
  } finally {
    $("button[type=submit]", el.form).disabled = false;
  }
}

el.form.addEventListener("submit", runBrief);
el.out.addEventListener("click", (ev) => {
  const btn = ev.target.closest?.("[data-regen]");
  if (btn) { reroll(btn.dataset.regen, btn); return; }
  const zoom = ev.target.closest?.("[data-lb]");
  if (zoom) { stLbOpen(zoom.dataset.lb, zoom.dataset.lbCap); return; }
  const cp = ev.target.closest?.("[data-copy-cap]");
  if (!cp) return;
  const txt = (cp.dataset.copyCap || "")
    + (cp.dataset.copyCta ? "\n\n" + cp.dataset.copyCta : "");
  const prev = cp.textContent;
  copyText(txt, (m) => { cp.textContent = m; cp.dataset.st = "ok"; },
               (m) => { cp.textContent = m; cp.dataset.st = "bad"; });
  setTimeout(() => { cp.textContent = prev; delete cp.dataset.st; }, 1500);
});
/* the image's own two honest states: it came, or it did not. load and error
   do not bubble, so the desk listens in the catching phase and speaks to the
   figure the image lives in. */
el.out.addEventListener("load", (ev) => {
  const f = ev.target.closest?.(".st-media");
  if (f) { f.classList.remove("is-load"); f.classList.add("is-ready"); }
}, true);
el.out.addEventListener("error", (ev) => {
  const f = ev.target.closest?.(".st-media");
  if (!f) return;
  f.classList.remove("is-load");
  f.classList.add("is-failed");
  const w = f.querySelector(".st-media__wait");
  if (w) w.textContent = "the image did not come — reroll, or open the campaign .zip";
}, true);
boot();
