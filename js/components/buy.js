import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* BuyOption — one premium card linking out (new tab). */
function BuyOption({ tag, name, blurb, cta, href, note = "" }) {
  return `
    <a class="buy-card" href="${href}" target="_blank" rel="noopener noreferrer" data-reveal>
      <span class="buy-card__tag">${tag}</span>
      <span class="buy-card__name display">${name}</span>
      <span class="buy-card__blurb">${blurb}</span>
      <span class="buy-card__cta">${cta} ${ICONS.arrow}</span>
      ${note ? `<span class="buy-card__note">${note}</span>` : ""}
    </a>`;
}

/* BuySection — the dedicated buy experience (deep-green ground). */
export function BuySection() {
  const cfg = CONFIG;
  return `
    <section class="buy" id="buy" aria-labelledby="buy-title">
      <div class="buy__inner">
        <header class="buy__head">
          <div>
            <p class="kicker" data-reveal>Buy</p>
            <h2 class="display buy__title" id="buy-title" data-reveal>
              How do you want your <span class="g">Paper?</span>
            </h2>
          </div>
          <a class="buy__back" href="#top">↑ back to site</a>
        </header>

        <div class="buy__grid">
          ${BuyOption({
            tag: "Option 01",
            name: "Jupiter",
            blurb: "Direct swap. the main routing rails of Solana.",
            cta: "Swap",
            href: cfg.jupiterUrl,
          })}
          ${BuyOption({
            tag: "Option 02",
            name: "Ember",
            blurb: "The Ember side of Paper — where memecoins live.",
            cta: "Buy",
            href: cfg.emberUrl,
            note: "Buying through Ember gives the team additional referral fees.",
          })}
        </div>

        <p class="buy__foot" data-reveal>both open in a new tab.</p>
      </div>
    </section>`;
}
