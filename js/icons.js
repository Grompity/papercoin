/* tiny inline stroke/fill marks — crisp, no icon font */

const svg = (body, { fill = false } = {}) =>
  `<svg class="ic" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"${
    fill ? "" : ` fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"`
  }>${body}</svg>`;

export const ICONS = {
  /* x (formal twitter-x mark, scaled to 24 grid) */
  x: svg(
    '<path fill="currentColor" d="M18.52 2.19h3.02l-6.61 8.4L22.6 21.8h-6.1l-4.77-6.05-5.45 6.05H3.26l7.06-8.97L1.7 2.19h6.24l4.44 5.67 5.14-5.67Zm-.53 17.44h1.68L6.67 3.86H4.86l13.13 15.77Z"/>',
    { fill: true }
  ),
  telegram: svg(
    '<path d="m22 2-3 19.3-7-5-3 5-.3-6.2L21 4.2 5.4 11.7l-3.4-1L22 2Z"/>' +
    '<path d="m12 16.3 3.2 8.2-5.8-4.6"/>'
  ),
  arrow: svg('<path d="M4 12h15M13.5 6l6 6-6 6"/>'),
  copy: svg(
    '<rect x="9" y="9" width="13" height="13" rx="2.4"/>' +
    '<path d="M4.5 16H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v.5"/>'
  ),
  check: svg('<path d="m4 12.5 5.2 5.2L20 6.5"/>'),
};
