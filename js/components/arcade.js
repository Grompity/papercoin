/* Arcade — the games floor (feature three). the shell ships ahead of the
   games: the floor names its games, their blurbs, and the honest truth of
   each one's state, and the boards answer with whatever the week holds —
   empty today, and that emptiness is the truth of the shell, not a stub.
   the integration of each game stays replaceable: a repo lands, a state
   changes, the floor itself does not move. points count on this floor; the
   prize purse lives elsewhere. */
export function Arcade() {
  return `
    <section class="arcade section" id="arcade" aria-labelledby="ar-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>The arcade — game points, not the prize purse</span>
          <span data-set="ar-week">the week: —</span>
        </div></div>

        <h2 class="board__title" id="ar-title" data-reveal>
          The arcade.
        </h2>
        <p class="board__lede" data-reveal>
          the floor where the games will hang their boards. points count here,
          the purse pays elsewhere, and every week shuts at Friday midnight —
          the same boundary the payouts already keep.
        </p>

        <div id="pb-arcade" data-reveal><!-- tiles and boards, injected by the app --></div>

        <form class="ab__hang ar__score" id="pb-score-form">
          <label class="field"><span>game</span>
            <select class="field__input" name="game" required></select>
          </label>
          <label class="field"><span>your score</span>
            <input class="field__input" name="score" type="number" min="0"
                   step="1" placeholder="the week's best" autocomplete="off">
          </label>
          <div class="wm__seat-doors">
            <button type="submit" class="btn btn--solid">book my score</button>
          </div>
          <p class="p-join__err" data-slot="ar-err" aria-live="polite"></p>
        </form>
      </div>
    </section>`;
}
