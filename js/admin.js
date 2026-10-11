/* admin.js — the review room for pb-v3 manual verification. The page itself
   is a public file; the DATA is not: every call is gated server-side, so a
   stranger who opens /admin sees a polite 403 and an empty table. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const esc = (t) => String(t == null ? "" : t)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const fmt = (n, digits) => n == null ? "—"
    : Number(n || 0).toLocaleString("en-US",
        { maximumFractionDigits: digits == null ? 0 : digits });
  const when = (epoch) => epoch == null ? "—"
    : new Date(epoch * 1000).toLocaleString("en-US",
        { Month: "short", Day: "numeric", Hour: "2-digit", Minute: "2-digit" });

  async function get(path) {
    const r = await fetch(path, { credentials: "same-origin" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status, payload: j });
    return j;
  }
  async function postJson(path, body) {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status, payload: j });
    return j;
  }

  const slots = {};
  ["who", "banner", "room", "rows", "msg", "flt",
   "treasury", "paySum", "payRows", "payNotes", "payHistory", "payPool"].forEach(
    (k) => { slots[k] = (k === "flt" || k === "payPool") ? $("#" + k) : $('[data-slot="' + k + '"]'); });

  function say(text, bad) {
    slots.msg.textContent = text || "";
    slots.msg.className = "admin-msg" + (bad ? " bad" : "");
  }

  function evtList(events) {
    if (!events || !events.length) return "";
    return '<details class="evt"><summary>' + events.length +
      ' event(s)</summary><ul>' + events.map(function (e) {
        return "<li>" + when(e.acted_at) + " · " + esc(e.admin) + " · " + esc(e.action) +
          " · " + fmt(e.prev_count) + " → " + fmt(e.new_count) +
          " · " + fmt(e.prev_points, 1) + " → " + fmt(e.new_points, 1) + " pts" +
          " · " + esc(e.method || "") + " (" + esc(e.reason || "") + ")" +
          " · " + esc(e.scoring_version) + "</li>";
      }).join("") + "</ul></details>";
  }

  function rowHtml(r) {
    const obs = r.official_likes == null
      ? "<i>none retrieved</i>"
      : fmt(r.official_likes) + ' <span class="faint">· ' + when(r.like_measured_at)
        + " · " + esc(r.like_source || "manual") + "</span>";
    const retri = r.like_status === "pending"
      ? "no like count retrieved"
      : esc(r.like_source || "manual");
    const countVal = r.official_likes == null ? "" : r.official_likes;
    const bonus = r.like_status === "pending"
      ? "<i>pending</i>" : fmt(r.like_bonus, 1);
    return "<tr>" +
      '<td><a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">#' +
        r.id + "</a></td>" +
      "<td>@" + esc(r.submitter || "anon") +
        ' <div class="faint">' + esc(r.submitter_email || "") + "</div></td>" +
      "<td>" + when(r.submitted_at) + "</td>" +
      "<td>" + retri + "</td>" +
      "<td>" + obs + "</td>" +
      "<td>" + fmt(r.base, 1) + "</td>" +
      "<td>" + bonus + "</td>" +
      '<td><span class="badge ' + esc(r.like_status) + '">' + esc(r.like_status) + "</span>" +
        evtList(r.events) + "</td>" +
      "<td>" +
        '<input type="number" min="0" step="1" size="6" data-fld="count" placeholder="likes" value="' + countVal + '">' +
        '<input type="text" size="10" data-fld="reason" placeholder="reason">' +
        ' <button type="button" data-act="verify" data-id="' + r.id + '">Verify</button>' +
        ' <button type="button" data-act="dispute" data-id="' + r.id + '">Dispute</button>' +
      "</td></tr>";
  }

  async function load() {
    try {
      const q = await get("/api/admin/queue?status=" + encodeURIComponent(slots.flt.value || "pending"));
      slots.room.hidden = false;
      slots.banner.hidden = true;
      slots.rows.innerHTML = q.rows.length
        ? q.rows.map(rowHtml).join("")
        : '<tr><td colspan="9" class="faint">nothing in the ' + esc(q.status_filter) + " lane.</td></tr>";
      say("mode: " + q.mode + " · filter: " + q.status_filter + " · " + q.rows.length + " row(s)");
    } catch (e) {
      slots.room.hidden = true;
      slots.banner.hidden = false;
      slots.banner.textContent = e.message === "not_authenticated"
        ? "sign in first — an admin is an account too (the magic-link session)."
        : e.message === "no_admins_configured"
          ? "no admins are configured — set PAPER_ADMINS and restart. the door is shut."
          : e.message === "admin_only"
            ? "signed in, but not on the PAPER_ADMINS list."
            : e.message;
    }
  }

  async function act(kind, id, cell) {
    const row = cell.closest("tr");
    const count = row.querySelector('[data-fld="count"]').value.trim();
    const reason = row.querySelector('[data-fld="reason"]').value.trim();
    if (kind === "verify" && count === "") { say("a verification needs a count.", true); return; }
    const body = { id: id, status: kind, reason: reason };
    if (kind === "verify") body.likes = Number(count);
    try {
      const res = await postJson("/api/admin/verify", body);
      await load();
      say((res.action === "dispute" ? "disputed" : "verified") +
          " · row now " + fmt(res.points, 1) + " pts · " + res.like_status);
    } catch (e) {
      say(e.message, true);
      await load();
    }
  }

  /* — treasury: the friday rhythm, phase 1, in test clothes. the same door
     as the queue: the server re-checks every move, so the buttons here are
     courtesy and the gate is law. — */
  let payCurrent = null;
  let payBatch = null;
  const payOn = {
    prepare:   function () { return true; },
    freeze:    function (s) { return s === "draft"; },
    approve:   function (s) { return s === "ready"; },
    simulate:  function (s) { return s === "approved"; },
    retry:     function (s) { return s === "partial"; },
    reconcile: function (s) { return s === "ready" || s === "approved" ||
                                         s === "partial" || s === "simulated"; },
    cancel:    function (s) { return s === "draft" || s === "ready" || s === "approved"; },
    reload:    function () { return true; },
  };

  function walletShort(w) {
    if (!w) { return "—"; }
    return w.length > 12 ? w.slice(0, 6) + "…" + w.slice(w.length - 4) : w;
  }

  function renderPay(v) {
    const b = v.batch;
    if (!b) {
      slots.paySum.innerHTML = '<p class="faint">no payout batch yet — ' +
        "“new batch” cuts one from the live board.</p>";
      slots.payRows.innerHTML = "";
      slots.payNotes.textContent = "";
      payCurrent = null;
      payBatch = null;
    } else {
      payCurrent = b.id;
      payBatch = b;
      const tokens = b.total_base / Math.pow(10, b.decimals || 0);
      slots.paySum.innerHTML = '<table class="admin-table"><tbody>' +
        "<tr><td>batch</td><td>" + esc(b.label) + " · period " + esc(b.period) +
        " · cut " + when(b.cutoff_at) + "</td></tr>" +
        "<tr><td>status</td><td>" + esc(b.status) +
        (b.closed_reason ? " · " + esc(b.closed_reason) : "") + "</td></tr>" +
        "<tr><td>pool / mint</td><td>" + fmt(b.pool_base) + " base · " +
        esc(String(b.token_mint || "—").slice(0, 8)) + "… · " +
        b.decimals + " decimals</td></tr>" +
        "<tr><td>rule</td><td>" + esc(b.formula) + " @ " + esc(b.rule_version) + "</td></tr>" +
        "<tr><td>recipients</td><td>" + b.participants + " eligible · total " + fmt(b.total_base) +
        " base (≈ " + fmt(tokens, 3) + " tokens) · left in pool " +
        fmt(b.pool_base - b.total_base) + "</td></tr></tbody></table>";
      slots.payRows.innerHTML = (v.entries && v.entries.length) ? v.entries.map(function (e) {
        return "<tr><td>" + e.rank + "</td><td>" + esc(e.handle) + "</td><td>" + fmt(e.points, 1) +
          "</td><td title=\"" + esc(e.wallet) + "\">" + esc(walletShort(e.wallet)) + "</td><td>" +
          fmt(e.amount_base) + "</td><td>" + fmt(e.share_pct, 2) + "</td><td>" + esc(e.status) +
          "</td><td>" + e.attempts + "</td><td>" + esc(e.tx_sig || "—") + "</td></tr>";
      }).join("") : '<tr><td colspan="9" class="faint">no one earns here — read the notes.</td></tr>';
      const notes = (b.warnings || []).map(function (w) { return "! " + w; });
      (b.excluded || []).forEach(function (x) {
        notes.push("—" + x.reason + (x.first_account ? " (first: " + x.first_account + ")" : ""));
      });
      slots.payNotes.textContent = notes.join("  ·  ");
    }
    if (v.history) {
      slots.payHistory.innerHTML = v.history.map(function (h) {
        return '<tr data-batch="' + h.id + '"><td>' + h.id + "</td><td>" + esc(h.label) +
          "</td><td>" + esc(h.status) + "</td><td>" + fmt(h.total_base) + "</td><td>" +
          when(h.generated_at) + "</td></tr>";
      }).join("") || '<tr><td colspan="5" class="faint">no history.</td></tr>';
    }
    const st = payBatch ? payBatch.status : null;
    document.querySelectorAll("[data-pay]").forEach(function (btn) {
      const f = payOn[btn.dataset.pay];
      if (f) { btn.disabled = !f(st); }
    });
  }

  async function loadPay(batchId) {
    try {
      const q = batchId ? "?batch=" + encodeURIComponent(batchId) : "";
      const v = await get("/api/admin/payout" + q);
      slots.treasury.hidden = false;
      renderPay(v);
    } catch (e) {
      slots.treasury.hidden = true;
      say(e.message);
    }
  }

  async function payAct(action) {
    if (action === "reload") { await loadPay(payCurrent); return; }
    const body = {};
    if (payCurrent != null) { body.batch = payCurrent; }
    if (action === "prepare") {
      const p = slots.payPool.value.trim();
      if (p !== "") { body.poolBase = Number(p); }
    }
    if (action === "approve" && payBatch && typeof confirm === "function") {
      if (!confirm("Approve batch " + payBatch.label + " for " +
                   payBatch.total_base + " base units to " + payBatch.participants +
                   " recipient(s), exactly as shown?")) { return; }
    }
    try {
      await postJson("/api/admin/payout/" + action, body);
      await loadPay(action === "prepare" ? null : payCurrent);
      say("batch: " + (payBatch ? payBatch.status : "cut"));
    } catch (e) {
      say(e.message, true);
      await loadPay(payCurrent);
    }
  }

  slots.flt.addEventListener("change", load);
  $('[data-act="reload"]').addEventListener("click", load);
  slots.rows.addEventListener("click", function (ev) {
    const btn = ev.target.closest("button[data-act]");
    if (btn) act(btn.dataset.act, Number(btn.dataset.id), btn);
  });
  document.querySelectorAll("[data-pay]").forEach(function (btn) {
    btn.addEventListener("click", function () { payAct(btn.dataset.pay); });
  });
  slots.payHistory.addEventListener("click", function (ev) {
    const tr = ev.target.closest("tr[data-batch]");
    if (tr) loadPay(Number(tr.dataset.batch));
  });

  // the room introduces itself from the server, never from a guess:
  // whoami is 200 only for a allowlisted session.
  get("/api/admin/whoami")
    .then(function (w) { slots.who.textContent = "signed in: " + (w.email || "?"); })
    .catch(function () { slots.who.textContent = ""; })
    .then(load)
    .then(function () { return loadPay(); });
}());
