import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* Community — cream invitation. big typographic rows: X, Telegram, Buy. */
export function Community() {
  const cfg = CONFIG;
  const rows = [
    { label: "X", meta: "follow", href: cfg.twitterUrl, social: true },
    { label: "Telegram", meta: "chat", href: cfg.telegramUrl, social: true },
    { label: `Buy ${cfg.ticker}`, meta: "get in", href: "#buy", social: false },
  ];
  return `
    <section class="community" id="community" aria-labelledby="community-title">
      <div class="community__inner">
        <p class="kicker kicker--dark" data-reveal>The paper trail</p>
        <h2 class="display community__title" id="community-title" data-reveal>Come find us.</h2>

        <div class="community__rows">
          ${rows
            .map(
              (r, i) => `
            <a class="community__row" href="${r.href}"
               ${r.social ? `target="_blank" rel="noopener noreferrer"` : ""}
               data-reveal style="--rd:${i * 0.07}s">
              <span class="community__num">0${i + 1}</span>
              <span class="community__label display">${r.label}</span>
              <span class="community__meta">${r.meta}</span>
              <span class="community__arrow">${ICONS.arrow}</span>
            </a>`
            )
            .join("")}
        </div>

        <div class="community__cameo" data-reveal aria-hidden="true">
          <img src="${cfg.mascotImage}" alt="" width="832" height="1248">
        </div>
      </div>
    </section>`;
}
