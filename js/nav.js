/* nav.js — the broadsheet's index, and the whole router.
   the page is not one endless crawl any more: main.js folds the spread into
   seven views (the fold helper), and this file decides which one is open.
   the run print rode in with the first game (P6Y); the index followed.
   the device is the hash alone — "#artboard" deep-links, back and forward
   ride the browser, no dependency is bought. an unknown mark lands on the
   front page rather than an error page: a broadsheet has no 404, only the
   front page. */

const VIEWS = ["home", "standings", "artboard", "worldmap", "arcade", "run", "tokens"];

export function startNav() {
  const views = [...document.querySelectorAll(".view")];
  const owners = new Map();                     /* section id → the fold it rides */
  for (const v of views) {
    for (const el of v.querySelectorAll("[id]")) owners.set(el.id, v);
  }

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const glide = reduced ? "auto" : "smooth";

  const mark = (viewName) => {
    for (const a of document.querySelectorAll(".mast__link")) {
      const on = (a.getAttribute("href") || "").slice(1) === viewName;
      a.classList.toggle("is-active", on);
      if (on) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  };

  const showFold = (name, targetId) => {
    const fold = views.find((v) => v.dataset.view === name) || views[0];
    if (!fold) return;
    for (const v of views) v.classList.toggle("is-view", v === fold);
    /* a fold that opens must not keep its own content hostage: the scroll
       observer may have met these elements only while they rode behind
       display:none, so the router hands the reveal itself — the doctrine
       says content may never stay hidden because a script or an observer
       blinked. the observer still runs; is-in is a one-way door either way. */
    for (const el of fold.querySelectorAll("[data-reveal]")) el.classList.add("is-in");
    mark(fold.dataset.view);
    const target = targetId && document.getElementById(targetId);
    if (target) target.scrollIntoView({ behavior: glide, block: "start" });
    else window.scrollTo({ top: 0, behavior: glide });
  };

  const show = (rawHash) => {
    const h = (rawHash || "").replace(/^#/, "");
    if (VIEWS.includes(h)) return showFold(h, null);
    if (owners.has(h)) return showFold(owners.get(h).dataset.view, h);
    return showFold("home", null);             /* the front page, always */
  };

  window.addEventListener("hashchange", () => show(window.location.hash));
  show(window.location.hash);
}
