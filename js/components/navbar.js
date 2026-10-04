import { CONFIG } from "../config.js";

/* Navbar — slim sticky bar. brand + two anchors + BUY pill.
   on mobile the anchors collapse; the pill stays tappable. */
export function Navbar() {
  const cfg = CONFIG;
  return `
    <header class="nav" id="nav">
      <div class="nav__inner">
        <a class="nav__brand" href="#top" aria-label="${cfg.ticker} — back to top">${cfg.ticker}</a>
        <nav class="nav__links" aria-label="main">
          <a class="nav__link" href="#about">About</a>
          <a class="nav__link" href="#community">Community</a>
        </nav>
        <a class="btn btn--pill" href="#buy">Buy</a>
      </div>
    </header>`;
}
