/* Run — the first machine on the floor, playing for real (PAPERBOARD P6Y).
   PAPER RUN rides the broadsheet as a live frame: its own rails, its own
   scripts, its own board. nothing is copied, proxied, or re-served — the
   print names the URL and gets out of the way. the arcade's own law (the
   week is the server's, one best per game per week) waits on wiring nobody
   has signed yet; see the score lane in the server's arcade method. */
export function Run() {
  return `
    <section class="run section" id="run" aria-labelledby="run-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>The arcade floor — the first machine, playing live</span>
          <span>PAPER RUN — its rails, its board, its week</span>
        </div></div>

        <h2 class="board__title" id="run-title" data-reveal>
          the run.
        </h2>
        <p class="board__lede" data-reveal>
          paper run is an endless runner on railway rails: the green $PAPER
          mascot gallops past the folds and you keep him off the walls, coins
          in his wake. its board answers to its own server, and the arcade's
          law — one best a week, the board shut at Friday midnight — waits on
          wiring nobody has signed yet. no points have been paid on a
          runner's word alone, and they never will be.
        </p>

        <figure class="run__stage" data-reveal>
          <div class="run__frame">
            <iframe id="pb-run-frame"
                    title="Paper Run — an endless runner for the $PAPER community, playing on its own rails"
                    src="https://paper-run-production.up.railway.app/"
                    referrerpolicy="strict-origin-when-cross-origin"
                    allow="autoplay"
                    sandbox="allow-scripts allow-same-origin allow-popups"></iframe>
          </div>
          <figcaption class="run__cap">
            <span class="run__keys">keys — space, w, and up jump; s and down
              slide; p pauses, the tune with it; m mutes. the frame wants a
              click or a tab before the keys reach it. a thumb works the same
              lane: tap to jump, swipe down to slide.</span>
            <a class="btn btn--pill" target="_blank" rel="noopener"
               href="https://paper-run-production.up.railway.app/">open PAPER
               RUN in a new tab</a>
          </figcaption>
          <p class="run__down" id="pb-run-down" hidden>
            the rails answer slowly — or the game is simply down there today.
            the pill above still opens it in its own tab.
          </p>
        </figure>
      </div>
    </section>`;
}
