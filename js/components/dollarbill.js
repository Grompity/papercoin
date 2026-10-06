import { CONFIG } from "../config.js";

/* TheBill — about + character, merged into one cream broadsheet spread.
   all the original statements are preserved, set differently. */
export function TheBill() {
  const cfg = CONFIG;
  return `
    <section class="bill section" id="bill" aria-labelledby="bill-title">
      <div class="rail">
        <div class="bill__grid">
          <div>
            <p class="kicker" data-reveal>What is paper</p>
            <h2 class="bill__title" id="bill-title" data-reveal style="margin-top:.6rem">
              it's money. <em>but the internet raised it.</em>
            </h2>
            <p class="bill__body" data-reveal>
              No whitepaper cosplay. No borrowed numbers. ${cfg.ticker} is a
              community-driven dollar bill living on ${cfg.network} — here to
              make noise, stack lore, and see where this thing goes.
            </p>
            <ul class="bill__facts" data-reveal>
              <li class="bill__fact"><b>hoodie</b><span>black. <em>confidence printed.</em></span></li>
              <li class="bill__fact"><b>fragrance</b><span>fresh bills <em>that pay in USDC.</em></span></li>
              <li class="bill__fact"><b>fine print</b><span>none. <em>always shows up.</em></span></li>
            </ul>
          </div>
          <div class="bill__art" data-reveal>
            <figure class="wire" aria-hidden="true">
              <img src="${cfg.poses.cool}" alt="" loading="lazy" decoding="async"
                   width="342" height="459">
              <figcaption>Paper — reading the receipts, unbothered</figcaption>
            </figure>
          </div>
        </div>
      </div>
    </section>`;
}
