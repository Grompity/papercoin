import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* Masthead — the nameplate. one sticky bar, nothing above it.
   the brand leans into the serif masthead; nav stays mono. */
export function Masthead() {
  const cfg = CONFIG;
  return `
    <header class="mast">
      <div class="mast__bar rail">
        <a class="mast__brand" href="#top" aria-label="${cfg.ticker} — back to top">
          ${ICONS.crown}<span class="mast__name">${cfg.tokenName}</span>
        </a>
        <nav class="mast__links" aria-label="main">
          <a class="mast__link" href="#board">Paperboard</a>
          <a class="mast__link" href="#frontpage">Front page</a>
          <a class="mast__link" href="#scoring">Scoring</a>
          <a class="mast__link" href="#bill">The bill</a>
        </nav>
        <a class="btn btn--pill mast__cta" href="#buy">Buy ${cfg.ticker}</a>
      </div>
    </header>`;
}
