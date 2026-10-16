import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* Hero — the front page. one kicker, one giant sentence, one line of support,
   one plate. the sentence never crops at any size; the mascot is a printed
   photograph, not a picture frame. */
export function Hero() {
  const cfg = CONFIG;
  return `
    <section class="hero rail" id="top">
      <div class="hero__inner">

        <div class="hero__date">
          <span class="kicker" data-set="comp-line">Issue 001 · Solana edition</span>
          <span data-set="comp-state">on press</span>
        </div>

        <h1 class="hero__title" data-reveal>
          every post <span class="g">counts.</span>
        </h1>

        <p class="hero__sub" data-reveal>
          track your contributions. earn points. climb the board.
        </p>

        <div class="hero__actions" data-reveal>
          <a class="btn btn--solid" href="#board" data-hero-connect>Join PAPERBOARD ↓</a>
          <a class="btn btn--pill" href="#buy">Buy ${cfg.ticker}</a>
        </div>

        <div class="hero__art" data-reveal data-parallax data-parallax-depth="0.55">
          <figure class="wire">
            <img src="${cfg.poses.front}" alt="Paper — the dollar bill, hooded, pointing at you" decoding="async">
            <figcaption>Paper — front page, first edition</figcaption>
          </figure>
        </div>

      </div>
    </section>`;
}
