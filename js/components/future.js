/* FutureSection — "what's next", teased, never promised. */
export function FutureSection() {
  const items = [
    { n: "01", label: "Community", note: "here now" },
    { n: "02", label: "Games", note: "small, soon" },
    { n: "03", label: "???", note: "", mystery: true },
  ];
  return `
    <section class="future" aria-labelledby="future-title">
      <div class="future__inner">
        <p class="kicker" data-reveal>What's next</p>
        <h2 class="display future__title" id="future-title" data-reveal>
          More Paper loading<i class="ld" aria-hidden="true"><i></i><i></i><i></i></i>
        </h2>
        <ol class="future__list">
          ${items
            .map(
              (it, i) => `
            <li class="future__item" data-reveal style="--rd:${i * 0.09}s">
              <span class="future__n">${it.n}</span>
              <span class="future__sep" aria-hidden="true">—</span>
              <span class="future__label display ${it.mystery ? "g" : ""}">${it.label}</span>
              ${it.note ? `<span class="future__note">${it.note}</span>` : ""}
            </li>`
            )
            .join("")}
        </ol>
        <p class="future__foot" data-reveal>small games. bigger vibes. more soon.</p>
      </div>
    </section>`;
}
