import { CONFIG } from "../config.js";

/* CharacterSection — the mascot's own moment.
   big cut-out figure, spotlight glow, three-line bio. */
export function CharacterSection() {
  const cfg = CONFIG;
  return `
    <section class="char" aria-labelledby="char-title">
      <div class="char__inner">
        <div class="char__art" data-reveal data-parallax>
          <figure class="char__figure">
            <span class="char__glow" aria-hidden="true"></span>
            <img class="char__img" src="${cfg.mascotImage}" alt="the $PAPER character, full figure" width="832" height="1248">
          </figure>
        </div>
        <div class="char__copy">
          <p class="kicker" data-reveal>The character</p>
          <h2 class="display char__title" id="char-title" data-reveal>
            Meet Paper<span class="char__period">.</span>
          </h2>
          <ul class="char__bio" data-reveal>
            <li>hoodie black. <span class="g">confidence printed.</span></li>
            <li>smells like fresh bills and <span class="g">bad decisions.</span></li>
            <li>no fine print. <span class="g">always shows up.</span></li>
          </ul>
          <p class="char__foot" data-reveal>Solana-born. street-run.</p>
        </div>
      </div>
    </section>`;
}
