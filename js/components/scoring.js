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
          <span>server-scored</span>
        </div></div>

        <h2 class="board__title" id="scoring-title" data-reveal style="margin-block:1rem .4rem">
          No vibes. Receipts.
        </h2>

        <div class="cols cols--3" data-reveal>
          <div>
            <h3>What qualifies a post</h3>
            <p>You submit a link to your own public X post. For it to qualify:</p>
            <ul>
              <li>your account's <b>X identity is verified</b> — one proof post, once;</li>
              <li>the post is <b>authored by that same X account</b> — the server reads the byline, never the browser;</li>
              <li>and the post carries at least one of the three recognized identifiers:</li>
            </ul>
            <ul>
              <li><b>${CONFIG.contractAddress}</b> — the contract address, case-strict;</li>
              <li><b>@paperusdc</b> — the handle, case-blind;</li>
              <li><b>$paper</b> — the ticker, case-blind.</li>
            </ul>
            <p>One identifier is enough. More change nothing.</p>
          </div>

          <div>
            <h3>How points work</h3>
            <table class="score-table">
              <thead><tr><th>What earns</th><th>Points</th></tr></thead>
              <tbody data-slot="score-table">
                <tr><td>every qualifying post</td><td data-set="w-base">—</td></tr>
                <tr><td>likes — a point each to the first 100, then a slower tail</td><td data-set="w-like">—</td></tr>
                <tr><td>maximum per post</td><td data-set="w-cap">—</td></tr>
              </tbody>
            </table>
            <p style="margin-top:.8rem">Only likes add points; replies, reposts, quotes and impressions ride along as evidence and score nothing. A like count the server cannot verify yet holds its bonus <b>pending</b> — never a false zero — and an official count settles it when it arrives. Nothing is promised by the count alone: the gate above and the server's own reading decide every score.</p>
          </div>

          <div>
            <h3>Fairness, plainly</h3>
            <ul>
              <li><b>One post, one score</b> — the same text from the same author inside the window scores once; echoes ride the breakdown at zero.</li>
              <li><b>No spam farming</b> — frequency walls keep a flood of posts from buying an edge.</li>
              <li><b>Fair measurement</b> — a verified zero is a real result; a metric the server can't read stays pending instead of being recorded as zero.</li>
              <li><b>Transparent corrections</b> — every point traces to its contributions, and an admin's correction to an official like count is recorded, never silent.</li>
            </ul>
            <p><b>Scores are calculated and verified by the server.</b> Your browser cannot set your score, leaderboard rank, or rewards.</p>
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
