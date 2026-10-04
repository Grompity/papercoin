import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";
import { ContractCopy } from "./contract-copy.js";

/* Hero — wordmark, tagline, cut-out character, CTA, CA, socials.
   the mascot breaks out of its spotlight frame (see .hero__art in css). */
export function Hero() {
  const cfg = CONFIG;
  return `
    <section class="hero" id="top">
      <div class="hero__inner">

        <div class="hero__lead">
          <p class="kicker" data-reveal>
            <span class="kicker__dot" aria-hidden="true"></span> ${cfg.network} native
          </p>
          <h1 class="display hero__title" data-reveal>
            <span class="hero__dollar">$</span>PAPER
          </h1>
          <p class="hero__tag" data-reveal>${cfg.tagline}</p>
          <p class="hero__facts" data-reveal>community-driven · no fine print</p>
        </div>

        <div class="hero__art" data-reveal data-parallax>
          <span class="hero__ghost" aria-hidden="true">PAPER</span>
          <figure class="hero__spot" aria-hidden="true">
            <img class="hero__img" src="${cfg.mascotImage}" alt="the $PAPER mascot — a dollar bill wearing a black hoodie and cap" width="832" height="1248">
          </figure>
        </div>

        <div class="hero__actions" data-reveal>
          <div class="hero__ctas">
            <a class="btn btn--solid" href="#buy">Buy ${cfg.ticker} ${ICONS.arrow}</a>
            ${ContractCopy({ variant: "pill" })}
          </div>
          <div class="hero__socials" aria-label="social links">
            <a class="sbtn" href="${cfg.twitterUrl}" target="_blank" rel="noopener noreferrer" aria-label="$PAPER on X">${ICONS.x}<span>X</span></a>
            <a class="sbtn" href="${cfg.telegramUrl}" target="_blank" rel="noopener noreferrer" aria-label="$PAPER on Telegram">${ICONS.telegram}<span>TG</span></a>
          </div>
        </div>

      </div>
      <div class="hero__cue" aria-hidden="true">scroll</div>
    </section>`;
}
