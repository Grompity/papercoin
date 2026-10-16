/* WorldMap — where the paper comes from (feature two). members name their own
   seat in their own words: a country, and optionally a region and a city —
   never an address, never a coordinate. the public face is the crowd's shape:
   counts by country, most-crowded first, and the counting forgives the twins
   of the case. the runtime rides with the rest of the app in js/paperboard.js. */
export function WorldMap() {
  return `
    <section class="worldmap section" id="worldmap" aria-labelledby="wm-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>The world map — aggregate only, member words</span>
          <span data-set="wm-state">the map: —</span>
        </div></div>

        <h2 class="board__title" id="wm-title" data-reveal>
          Where the paper comes from.
        </h2>
        <p class="board__lede" data-reveal>
          one country, and a region and a city if you like — words, not
          coordinates. the map counts places, never people: an account id
          never crosses the public door.
        </p>

        <svg class="wm__map" viewBox="0 0 360 180" role="img"
             aria-label="a small hand-drawn world, hairline continents on a grid" data-reveal>
          <defs><pattern id="wm-grid" width="20" height="20" patternUnits="userSpaceOnUserSpace">
            <path d="M20 0 H0 V20" class="wm__grid" fill="none"/>
          </pattern></defs>
          <rect width="360" height="180" fill="url(#wm-grid)"/>
          <path class="wm__land" d="M40 40 Q70 28 96 44 T120 70 Q108 96 84 104 L70 88 Q48 70 40 40 Z"/>
          <path class="wm__land" d="M86 112 Q100 118 104 140 Q98 160 88 158 Q78 138 86 112 Z"/>
          <path class="wm__land" d="M158 44 Q186 30 210 40 Q204 58 186 64 L168 70 Q154 58 158 44 Z"/>
          <path class="wm__land" d="M178 78 Q198 76 204 98 Q198 126 184 132 Q170 110 178 78 Z"/>
          <path class="wm__land" d="M224 46 Q276 34 312 52 Q322 72 300 84 L262 88 Q232 70 224 46 Z"/>
          <path class="wm__land" d="M296 116 Q316 112 322 126 Q312 138 298 134 Q290 124 296 116 Z"/>
          <line class="wm__grid" x1="0" y1="90" x2="360" y2="90"/>
        </svg>

        <div id="pb-world" data-reveal><!-- the crowd's shape, injected by the app --></div>

        <form class="ab__hang wm__seat" id="pb-seat-form">
          <label class="field"><span>country</span>
            <input class="field__input" name="country" maxlength="60" required
                   placeholder="where you hang your hat" autocomplete="off">
          </label>
          <label class="field"><span>region</span>
            <input class="field__input" name="region" maxlength="60"
                   placeholder="optional" autocomplete="off">
          </label>
          <label class="field"><span>city</span>
            <input class="field__input" name="city" maxlength="80"
                   placeholder="optional" autocomplete="off">
          </label>
          <div class="wm__seat-doors">
            <button type="submit" class="btn btn--solid">set my seat</button>
            <button type="button" class="btn" id="pb-seat-drop">remove my seat</button>
          </div>
          <p class="p-join__err" data-slot="wm-err" aria-live="polite"></p>
        </form>
      </div>
    </section>`;
}
