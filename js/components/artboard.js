/* Artboard — the community wall (feature one). paper hung by members: the
   server keeps the day (three sheets, midnight to midnight in UTC) and the
   vote is a courtesy, never a payment — a re-vote DEFERS with a truthful 200,
   the same doctrine the mail lane speaks. the bytes live on disk beside the
   database; this section's only truth about them is what /api/artworks says.
   the runtime rides with the rest of the app in js/paperboard.js. */
export function Artboard() {
  return `
    <section class="artboard section" id="artboard" aria-labelledby="ab-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>The artboard — member paper, server-kept</span>
          <span data-set="ab-state">the wall: —</span>
        </div></div>

        <h2 class="board__title" id="ab-title" data-reveal>
          The artboard.
        </h2>
        <p class="board__lede" data-reveal>
          members hang their own paper — up to three sheets a day, and the
          wall's order is a vote count, never a promise of points.
        </p>

        <div class="ab__sort" data-reveal>
          <button type="button" class="btn btn--pill is-on" data-art-sort="new">newest first</button>
          <button type="button" class="btn btn--pill" data-art-sort="liked">most liked</button>
        </div>

        <div id="pb-art-wall" data-reveal><!-- the wall, injected by the app --></div>

        <form class="ab__hang" id="pb-art-form">
          <label class="field"><span>title</span>
            <input class="field__input" name="title" maxlength="80" required
                   placeholder="name your sheet" autocomplete="off">
          </label>
          <label class="field"><span>credit</span>
            <input class="field__input" name="credit" maxlength="60"
                   placeholder="who drew it" autocomplete="off">
          </label>
          <label class="field"><span>note</span>
            <input class="field__input" name="note" maxlength="500"
                   placeholder="one honest sentence (optional)" autocomplete="off">
          </label>
          <label class="field"><span>paper</span>
            <input class="field__input" type="file" name="paper" required
                   accept="image/png,image/jpeg,image/gif,image/webp">
          </label>
          <button type="submit" class="btn btn--solid">hang it</button>
          <p class="p-join__err" data-slot="ab-err" aria-live="polite"></p>
        </form>
      </div>
    </section>`;
}
