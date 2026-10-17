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

/* — the campaign painting — */
function renderCampaign(c) {
  state.campaign = c;
  el.stage.textContent = "step five · review";
  const plan = c.plan || {};
  const qc = c.qc || {};
  const days = (plan.days || []).map((d) => `
    <div class="st-day">
      <div class="st-day__top"><b>day ${esc(String(d.day))}</b>
        <span>${esc(d.purpose)}</span><span>${esc(d.platform || "X")}</span>
        <span>${esc(d.aspect || "")}</span></div>
      <div class="st-day__concept">${esc(d.concept)}</div>
      <div class="st-day__cap">${esc(d.caption)} <span class="st-day__cta">${esc(d.cta)}</span></div>
    </div>`).join("");

  const roles = (c.brand && c.brand.roles) || {};
  const swatch = (name) => roles[name]
    ? `<span class="st-role"><i style="--sw: rgb(${roles[name].join(",")})"></i>${esc(name)}</span>` : "";
  const brand = `
    <div class="st-swatch">${["paper", "press", "accent"].map(swatch).join("")}</div>
    ${(c.brand?.notes || []).map((n) => `<p class="st-note">${esc(n)}</p>`).join("")}`;

  const cards = (c.assets || []).map((a) => `
    <article class="st-card" data-asset="${esc(String(a.id))}">
      <img src="${esc(a.href || "")}" alt="${esc(`${a.use} card for day ${a.day}`)}" loading="lazy">
      <div class="st-card__meta">
        <div class="st-card__name">${esc(a.href ? a.href.split("/").pop() : "no file")}</div>
        <div class="st-card__use">day ${esc(String(a.day))} · ${esc(a.use)}</div>
        <div class="st-card__dims">${a.width && a.height ? esc(`${a.width}×${a.height}`) : "—"} · PNG
          ${a.note ? ` · <span class="st-card__note">${esc(a.note)}</span>` : ""}</div>
      </div>
      <div class="st-card__row">
        <span class="st-state ${a.status === "ready" ? "" : "st-state--failed"}">${esc(a.status)}</span>
        <div class="st-card__acts">
          ${a.href ? `<a class="btn btn--ghost" href="${esc(a.href)}" download>get</a>` : ""}
          ${a.kind === "ai_hero" ? "" : `<button class="btn btn--pill st-regen" type="button" data-regen="${esc(String(a.id))}">reroll</button>`}
        </div>
      </div>
    </article>`).join("");

  el.out.innerHTML = `
    <section class="panel st-sec">
      <div class="panel__head"><h2>the plan · ${esc(String(qc.days || (plan.days || []).length))} days</h2>
        <span class="st-stage">${esc(c.engine)}</span></div>
      <div class="panel__body">
        <p class="st-note">${esc(plan.objective || "")} — ${esc(plan.core_message || "")}</p>
        <div class="st-days">${days}</div>
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
      <div class="panel__head"><h2>the cards · ${esc(String(qc.assets_ready || 0))} of ${esc(String(qc.assets_total || 0))} ready</h2></div>
      <div class="panel__body">
        <div class="st-shelf">${cards || `<p class="st-hush">none shipped — the brief spoke, the pen did not.</p>`}</div>
        <div class="st-acts">
          <span class="st-qc">sizes from the written headers: <b>${esc(String(qc.sizes_verified || 0))}</b>
            · concepts distinct: <b>${esc(String(qc.concepts_distinct || 0))}</b>/${esc(String(qc.days || 0))}</span>
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
  if (btn) reroll(btn.dataset.regen, btn);
});
boot();
