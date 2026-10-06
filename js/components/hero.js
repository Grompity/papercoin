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
          the internet's most <span class="g">overqualified</span> dollar&nbsp;bill.
        </h1>

        <p class="hero__sub" data-reveal>
          <b>${cfg.ticker}</b> is a memecoin on Solana — post about it on X,
          the server prints points, points move you up the <b>Paperboard</b>.
        </p>

        <div class="hero__actions" data-reveal>
          <a class="btn btn--solid" href="#board" data-hero-connect>Connect X ${ICONS.x}</a>
          <a class="btn btn--pill" href="#buy">Buy ${cfg.ticker}</a>
          <a class="btn btn--pill" href="#frontpage">PAPERBOARD ↓</a>
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
