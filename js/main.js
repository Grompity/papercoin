import { CONFIG } from "./config.js";
import { ICONS } from "./icons.js";
import { Masthead } from "./components/masthead.js";
import { Hero } from "./components/hero.js";
import { Board } from "./components/board.js";
import { FrontPage } from "./components/frontpage.js";
import { Scoring } from "./components/scoring.js";
import { TheBill } from "./components/dollarbill.js";
import { BuySection } from "./components/buy.js";
import { Footer } from "./components/footer.js";
import { startPaperboard } from "./paperboard.js";

const root = document.getElementById("root");

root.innerHTML = [
  Masthead(),
  Hero(),
  Board(),
  FrontPage(),
  Scoring(),
  TheBill(),
  BuySection(),
  Footer(),
].join("\n");

/* — reveal on scroll (one observer, zero demo soup) — */
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const revealables = root.querySelectorAll("[data-reveal]");

if (reduced || !("IntersectionObserver" in window)) {
  revealables.forEach((el) => el.classList.add("is-in"));
} else {
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("is-in");
          io.unobserve(e.target);
        }
      }
    },
    { threshold: 0.15, rootMargin: "0px 0px -6% 0px" }
  );
  revealables.forEach((el) => io.observe(el));
}

/* — copy the contract address (hero + footer share one behavior) — */
function legacyCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.readOnly = true;
  ta.style.cssText = "position:fixed;top:-300%;";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* old days */ }
  ta.remove();
  return ok !== false;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return legacyCopy(text);
  }
}

for (const btn of root.querySelectorAll("[data-copy-ca]")) {
  const val = btn.dataset.original;
  const label = btn.querySelector(".ca__val");
  const ico = btn.querySelector("[data-ca-icon]");
  const original = label.textContent;
  let timer = 0;

  ico.innerHTML = ICONS.copy;

  btn.addEventListener("click", async () => {
    const ok = await copyText(val);
    if (!ok) return;
    btn.classList.add("is-copied");
    label.textContent = "Copied";
    ico.innerHTML = ICONS.check;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      btn.classList.remove("is-copied");
      label.textContent = original;
      ico.innerHTML = ICONS.copy;
    }, 1600);
  });
}

/* — pointer parallax on the hero wire-photo (fine pointers only) — */
if (!reduced && window.matchMedia("(pointer: fine)").matches) {
  for (const zone of root.querySelectorAll("[data-parallax]")) {
    const depth = Number(zone.dataset.parallaxDepth ?? 1);
    let tx = 0, ty = 0, cx = 0, cy = 0, raf = 0;

    const frame = () => {
      cx += (tx - cx) * 0.06;
      cy += (ty - cy) * 0.06;
      zone.style.setProperty("--px", `${cx.toFixed(2)}px`);
      zone.style.setProperty("--py", `${cy.toFixed(2)}px`);
      raf = Math.abs(tx - cx) > 0.05 || Math.abs(ty - cy) > 0.05
        ? window.requestAnimationFrame(frame)
        : 0;
    };
    const wake = () => { if (!raf) raf = window.requestAnimationFrame(frame); };

    zone.addEventListener("pointermove", (e) => {
      const r = zone.getBoundingClientRect();
      const nx = (e.clientX - r.left) / r.width - 0.5;
      const ny = (e.clientY - r.top) / r.height - 0.5;
      tx = nx * -16 * depth;
      ty = ny * -12 * depth;
      wake();
    });
    zone.addEventListener("pointerleave", () => { tx = 0; ty = 0; wake(); });
  }
}

/* — PAPERBOARD: the product boots after the sheet is on the page — */
startPaperboard();
