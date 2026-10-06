import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* BuySection — two exits, one truth: Jupiter routes, Ember is the memecoin
   home. the Ember card keeps the referral-fee note verbatim. */
export function BuySection() {
  const cfg = CONFIG;
  return `
    <section class="buy section" id="buy" aria-labelledby="buy-title">
      <div class="rail">
        <header class="buy__head">
          <div>
            <p class="kicker" data-reveal>Buy</p>
            <h2 class="buy__title" id="buy-title" data-reveal style="margin-top:.6rem">
              How do you want your <span class="g">Paper?</span>
            </h2>
          </div>
          <a class="buy__back" href="#top">↑ back to the top</a>
        </header>

        <div class="buy__grid">
          <a class="buy-card" href="${cfg.jupiterUrl}" target="_blank" rel="noopener noreferrer" data-reveal>
            <span class="buy-card__tag">Option 01 · swap</span>
            <span class="buy-card__name">Jupiter</span>
            <span class="buy-card__blurb">Direct swap. the main routing rails of ${cfg.network}.</span>
            <span class="buy-card__cta">Swap on Jupiter ${ICONS.arrow}</span>
          </a>
          <a class="buy-card" href="${cfg.emberUrl}" target="_blank" rel="noopener noreferrer" data-reveal>
            <span class="buy-card__tag">Option 02 · memecoin home</span>
            <span class="buy-card__name">Ember</span>
            <span class="buy-card__blurb">The Ember side of Paper — where memecoins live.</span>
            <span class="buy-card__cta">Buy on Ember ${ICONS.arrow}</span>
            <span class="buy-card__note">${cfg.emberNote}</span>
          </a>
        </div>

        <p class="buy__foot" data-reveal>both open in a new tab · CA: ${cfg.contractAddress}</p>
      </div>
    </section>`;
}
