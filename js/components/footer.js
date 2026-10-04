import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";
import { ContractCopy } from "./contract-copy.js";

/* Footer — tiny, calm, everything reachable once more. */
export function Footer() {
  const cfg = CONFIG;
  return `
    <footer class="footer">
      <div class="footer__inner">
        <div class="footer__row">
          <a class="footer__brand display" href="#top" aria-label="${cfg.ticker} — back to top">${cfg.ticker}</a>
          <div class="footer__links">
            <a class="footer__link" href="${cfg.twitterUrl}" target="_blank" rel="noopener noreferrer">X ${ICONS.arrow}</a>
            <a class="footer__link" href="${cfg.telegramUrl}" target="_blank" rel="noopener noreferrer">Telegram ${ICONS.arrow}</a>
            <a class="footer__link" href="#buy">Buy ${ICONS.arrow}</a>
          </div>
          ${ContractCopy({ variant: "mini" })}
        </div>
        <p class="footer__disclaimer">${cfg.disclaimer}</p>
      </div>
    </footer>`;
}
