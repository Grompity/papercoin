import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";
import { ContractCopy } from "./contract-copy.js";

/* Footer — colophon. everything reachable one last time. */
export function Footer() {
  const cfg = CONFIG;
  return `
    <footer class="footer">
      <div class="rail">
        <div class="footer__row">
          <a class="footer__brand" href="#top" aria-label="${cfg.ticker} — back to top">
            ${ICONS.crown} ${cfg.tokenName}
          </a>
          <div class="footer__links">
            <a class="footer__link" href="${cfg.twitterUrl}" target="_blank" rel="noopener noreferrer">X ${ICONS.arrow}</a>
            <a class="footer__link" href="${cfg.telegramUrl}" target="_blank" rel="noopener noreferrer">Telegram ${ICONS.arrow}</a>
            <a class="footer__link" href="#buy">Buy ${ICONS.arrow}</a>
            <a class="footer__link" href="#board">Paperboard ${ICONS.arrow}</a>
          </div>
          ${ContractCopy({ variant: "mini" })}
        </div>
        <p class="footer__disclaimer">${cfg.disclaimer} ${cfg.motto} — ${cfg.network} · ${cfg.call}</p>
      </div>
    </footer>`;
}
