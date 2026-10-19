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

        <div class="wm__canvas" id="wm-map" role="region"
             aria-labelledby="wm-title" tabindex="0" data-reveal></div>
        <p class="wm__attr">map data © OpenStreetMap contributors (ODbL) · counts are places, never people · drag to pan, wheel or double-click to zoom</p>

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
