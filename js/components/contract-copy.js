import { CONFIG } from "../config.js";

/* ContractCopy — the CA pill. click to copy, brief "COPIED" feedback.
   variant: "pill" (hero) | "mini" (footer) */
export function ContractCopy({ variant = "pill" } = {}) {
  const ca = CONFIG.contractAddress;
  const shown = `${ca.slice(0, 11)}…${ca.slice(-4)}`;
  return `
    <button type="button" class="ca ca--${variant}" data-copy-ca
            data-original="${ca}"
            aria-label="copy the ${CONFIG.ticker} contract address to the clipboard"
            title="${ca}">
      <span class="ca__label">CA</span>
      <span class="ca__val">${shown}</span>
      <span class="ca__ico" data-ca-icon aria-hidden="true"></span>
    </button>`;
}
