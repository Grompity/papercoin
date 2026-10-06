import { CONFIG } from "../config.js";

/* Scoring — the rules spread. every number on this page is a mirror of the
   server's scoring config; the app repaints the table from /api/config so the
   printed numbers can never rot away from the engine. */
export function Scoring() {
  return `
    <section class="scoring section" id="scoring" aria-labelledby="scoring-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>Section: how points are printed</span>
          <span>engine <span data-set="scoring-version">—</span></span>
        </div></div>

        <h2 class="board__title" id="scoring-title" data-reveal style="margin-block:1rem .4rem">
          No vibes. Receipts.
        </h2>

        <div class="cols cols--3" data-reveal>
          <div>
            <h3>The gate</h3>
            <p>
              a post scores only if <b>the author follows @paperusdc</b>
              <em>(checked by the server, not asserted by the browser)</em>
              <b>and</b> the post contains one of the three identifiers:
            </p>
            <ul>
              <li><b>${CONFIG.contractAddress}</b> — the CA, case-strict</li>
              <li><b>@paperusdc</b> — the handle, case-blind</li>
              <li><b>$paper</b> — the ticker, case-blind</li>
            </ul>
            <p>One is enough. Two is not a bonus. That is the whole gate.</p>
          </div>

          <div>
            <h3>The point press</h3>
            <table class="score-table">
              <thead><tr><th>Signal</th><th>Points</th></tr></thead>
              <tbody data-slot="score-table">
                <tr><td>per qualifying post (base)</td><td data-set="w-base">—</td></tr>
                <tr><td>like</td><td data-set="w-like">—</td></tr>
                <tr><td>reply</td><td data-set="w-reply">—</td></tr>
                <tr><td>repost</td><td data-set="w-repost">—</td></tr>
                <tr><td>quote</td><td data-set="w-quote">—</td></tr>
                <tr><td>impressions</td><td data-set="w-imp">—</td></tr>
                <tr><td>per-post cap</td><td data-set="w-cap">—</td></tr>
              </tbody>
            </table>
            <p style="margin-top:.8rem">Impressions never print linearly —
               a saturating curve keeps one bloated post from buying the front page.</p>
          </div>

          <div>
            <h3>Anti-gaming</h3>
            <ul>
              <li><b>Duplicates</b> — same author, same text inside the window: the first one scores, the echoes watch from the breakdown.</li>
              <li><b>Frequency cap</b> — a wall of posts stops farming after the configured count.</li>
              <li><b>Thin signal</b> — near-zero views and zero engagement prints half points.</li>
              <li><b>Audit trail</b> — every stored point keeps its contributions; any row can be explained, in court.</li>
            </ul>
            <p><b class="g">Client input is never trusted:</b> no endpoint takes a
               score, a ranking, or a reward from the browser.</p>
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
