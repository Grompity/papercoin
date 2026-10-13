import { CONFIG } from "../config.js";

/* Scoring — the rules spread, simplified (the approved three-column copy).
   every number on this page is a mirror of the server's scoring config; the
   app repaints the figures from /api/config at boot, so the printed numbers
   are truthful before the script runs and can never rot away from the
   engine after it. the long-form fine print (duplicate windows, frequency
   walls, pending-metric edge cases, the audit trail, admin corrections)
   lives on in the server and its tests — it is simply no longer recited
   here, where an onboarding eye only needs the shape of the deal. */
export function Scoring() {
  return `
    <section class="scoring section" id="scoring" aria-labelledby="scoring-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>Section: how points are earned</span>
          <span>server-scored</span>
        </div></div>

        <h2 class="board__title" id="scoring-title" data-reveal
            style="margin-block:1rem clamp(30px, 4.5vw, 64px)">
          No vibes. Receipts.
        </h2>

        <div class="cols cols--3" data-reveal>
          <div>
            <h3>01. Qualify</h3>
            <p>Verify your X account and submit a link to your own public post containing at least one:</p>
            <ul>
              <li><b>${CONFIG.contractAddress}</b> — the contract address, case-strict;</li>
              <li><b>@paperusdc</b> — the handle, case-blind;</li>
              <li><b>$paper</b> — the ticker, case-blind.</li>
            </ul>
            <p>One identifier is enough.</p>
          </div>

          <div>
            <h3>02. Earn points</h3>
            <table class="score-table">
              <tbody data-slot="score-table">
                <tr><td>Qualifying post</td><td>+<span data-set="w-base">10</span></td></tr>
                <tr><td>Likes</td><td>+<span data-set="w-like">1</span> each*</td></tr>
              </tbody>
            </table>
            <p class="sc-max">Maximum: <b class="g"><span data-set="w-cap">210</span></b> points per post.</p>
            <p class="sc-fn">*Like bonuses are capped — full value to the first 100 likes, a slower tail after — and may remain pending until verified.</p>
          </div>

          <div>
            <h3>03. Keep it real</h3>
            <ul>
              <li>Only qualifying posts and verified likes earn points.</li>
              <li>Replies, reposts, quotes and impressions earn no points.</li>
              <li>Scores are calculated by the server, not your browser.</li>
            </ul>
          </div>
        </div>

        <div class="prizes" data-reveal>
          <div class="prizes__art"><img src="${CONFIG.poses.victory}" alt="Paper, sitting on the prize stack" loading="lazy" decoding="async"></div>
          <div>
            <h3>How the stack gets split</h3>
            <p>out of the box: <b data-set="prize-model">—</b> distribution over a
               <b data-set="prize-pool">—</b> <span data-set="prize-unit">—</span> pool —
               everyone with points shares by their share of the points, the top
               of the page gets a slice on top, and a community slice stays at the
               editor's discretion. every model is a config value, so the next
               issue can split it differently without a rebuild.</p>
          </div>
        </div>
      </div>
    </section>`;
}
