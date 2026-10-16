/* HomeDash — the masthead of the front page (the redesign's own section).
   the state of the paper in one glance: where the board stands, what the
   wall printed, where the crowd sits, when the arcade's week shuts. every
   figure here is fetched, none is typed — a silent door gets one honest
   line, never a fake zero. the cells open their destinations; the doors are
   the navigation. */
export function HomeDash() {
  return `
    <section class="dash section" id="home" aria-labelledby="dash-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>The state of the paper — live from the press</span>
          <span data-set="dash-state">reading the press…</span>
        </div></div>

        <h2 class="board__title" id="dash-title" data-reveal>
          Today's press sheet.
        </h2>

        <div class="dash__grid">
          <article class="dash__cell dash__cell--wide" data-reveal>
            <p class="dash__head">the front page, top three</p>
            <ol class="dash__peel" data-slot="dash-board">
              <li class="dash__hush">…</li>
            </ol>
            <a class="dash__door" href="#frontpage">the full standings →</a>
          </article>

          <article class="dash__cell" data-reveal>
            <p class="dash__head">fresh from the wall</p>
            <div class="dash__thumbs" data-slot="dash-art">
              <p class="dash__hush">…</p>
            </div>
            <a class="dash__door" href="#artboard">the artboard →</a>
          </article>

          <article class="dash__cell dash__cell--broad" data-reveal>
            <p class="dash__head">where the paper is read</p>
            <div data-slot="dash-map"><p class="dash__hush">…</p></div>
            <a class="dash__door" href="#worldmap">the world map →</a>
          </article>

          <article class="dash__cell dash__cell--lean" data-reveal>
            <p class="dash__head">the arcade, this week</p>
            <div data-slot="dash-arcade"><p class="dash__hush">…</p></div>
            <a class="dash__door" href="#arcade">the arcade →</a>
          </article>
        </div>
      </div>
    </section>`;
}
