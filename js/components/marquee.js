import { CONFIG } from "../config.js";

/* Marquee — one green strip of identity between hero and about.
   track is duplicated once for a seamless -50% loop. */
export function Marquee() {
  const cfg = CONFIG;
  const items = [
    `${cfg.ticker} ON SOLANA`,
    "MONEY, BUT INTERNET",
    "NO FINE PRINT",
    "JUST PAPER",
  ];
  const group = (hidden) => `
    <div class="marquee__group"${hidden ? ' aria-hidden="true"' : ""}>
      ${items.map((t) => `<span class="marquee__item">${t}</span><span class="marquee__sep" aria-hidden="true">/</span>`).join("")}
    </div>`;
  return `
    <div class="marquee" role="presentation">
      <div class="marquee__track">
        ${group(false)}
        ${group(true)}
      </div>
    </div>`;
}
