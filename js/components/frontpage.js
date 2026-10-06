/* FrontPage — the full leaderboard. the table body is server data, rendered
   by the app (js/paperboard.js). tiers are editorial vocabulary
   (front page / headline / extra), stated once in prose — never as row badges. */
export function FrontPage() {
  return `
    <section class="frontpage section" id="frontpage" aria-labelledby="fp-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>Standings — official snapshot</span>
          <span data-set="fp-state">state: —</span>
        </div></div>

        <h2 class="board__title" id="fp-title" data-reveal>
          The front page.
        </h2>
        <p class="board__lede" data-reveal>
          ranked by server points — rank&nbsp;1 owns the front page, two and three
          are the headlines, four through ten ride the extra.
        </p>

        <div id="pb-board" data-reveal><!-- table / empty state injected by the app --></div>

        <div class="fp__foot">
          <span data-set="fp-generated">—</span>
          <span data-set="fp-totals">—</span>
        </div>
      </div>
    </section>`;
}
