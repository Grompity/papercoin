import { CONFIG } from "../config.js";

/* About — cream section. one statement, three plain truths. */
export function About() {
  const cfg = CONFIG;
  return `
    <section class="about" id="about" aria-labelledby="about-title">
      <div class="about__inner">
        <div class="about__copy">
          <p class="kicker kicker--dark" data-reveal>What is paper</p>
          <h2 class="display about__title" id="about-title" data-reveal>
            it's money. <em>but the internet raised it.</em>
          </h2>
          <p class="about__line" data-reveal>
            no whitepaper cosplay. no borrowed numbers. ${cfg.ticker} is a
            community-driven dollar bill living on Solana — it shows up,
            says something, and never pretends to be a financial product.
          </p>
        </div>
        <div class="about__sticker" data-reveal aria-hidden="true">
          <img src="${cfg.mascotImage}" alt="" width="832" height="1248">
        </div>
      </div>
    </section>`;
}
