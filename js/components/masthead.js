import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* Masthead — the nameplate. one sticky bar, nothing above it.
   the brand leans into the serif masthead; nav stays mono. */
export function Masthead() {
  const cfg = CONFIG;
  return `
    <header class="mast">
      <a class="skip" href="#board">skip to the board</a>
      <div class="mast__bar rail">
        <a class="mast__brand" href="#home" aria-label="${cfg.ticker} — back to the front page">
          ${ICONS.crown}<span class="mast__name">${cfg.tokenName}</span>
        </a>
        <nav class="mast__links" aria-label="main">
          <a class="mast__link" href="#standings">Standings</a>
          <a class="mast__link" href="#artboard">Artboard</a>
          <a class="mast__link" href="#worldmap">Map</a>
          <a class="mast__link" href="#arcade">Arcade</a>
          <a class="mast__link" href="#run">Run</a>
          <a class="mast__link" href="#tokens">Tokens</a>
        </nav>
        <a class="btn btn--pill mast__cta" href="#buy">Buy ${cfg.ticker}</a>
      </div>
    </header>`;
}
