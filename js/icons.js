/* tiny inline marks — crisp, no icon font, no network */

const svg = (body, { fill = false } = {}) =>
  `<svg class="ic" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"${
    fill ? "" : ` fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"`
  }>${body}</svg>`;

export const ICONS = {
  /* the crown — PAPER's mark, drawn to match the master sheet */
  crown: svg(
    '<path d="M3.5 18h17M4.5 17 5.8 8.6l4.3 3.4L12 6l1.9 6 4.3-3.4L19.5 17"/>' +
    '<path d="M3.2 18.6h17.6"/>',
    { fill: false }
  ),
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
  chevron: svg('<path d="m6 9 6 6 6-6"/>'),
  copy: svg(
    '<rect x="9" y="9" width="13" height="13" rx="2.4"/>' +
    '<path d="M4.5 16H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v.5"/>'
  ),
  check: svg('<path d="m4 12.5 5.2 5.2L20 6.5"/>'),
  wallet: svg(
    '<rect x="3" y="6" width="18" height="14" rx="2.4"/>' +
    '<path d="M3 10h18M15 15h3"/><path d="M5.5 6l2-3h8l2 3"/>'
  ),
  /* the receipts, from the master sheet's engagement row */
  eye: svg('<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>'),
  heart: svg('<path d="M12 20.5C7 16.5 3.4 13.2 3.4 9.5 3.4 6.9 5.4 5 7.8 5c1.7 0 3.2 1 4.2 2.6C13 6 14.5 5 16.2 5c2.4 0 4.4 1.9 4.4 4.5 0 3.7-3.6 7-8.6 11Z"/>'),
  repost: svg('<path d="M7 4.5 3.5 8 7 11.5M3.5 8h11A4.5 4.5 0 0 1 19 12.5V13M17 19.5l3.5-3.5L17 12.5M20.5 16h-11A4.5 4.5 0 0 1 5 11.5V11"/>'),
  quote: svg('<path d="M9.5 6v6.5A4 4 0 0 1 5.5 16M14.5 18v-6.5a4 4 0 0 1 4-3.5"/><path d="M6 6h3.5M14.5 18H18"/>'),
  stamp: svg('<path d="M8 2.8 9.8 5l2.2-2 2.2 2 2.2-2L18 4.2 21.2 6l-1.6 1.8 1.4 2.2-2.5.8.8 2.5-2.3-.4.4 2.3-2.2-1.1L12 16.9l-1.7 1.6-2.2 1.1.4-2.3-2.3.4.8-2.5-2.5-.8L3.5 8 5 6.3 3.3 4.2 6.5 2 8 2.8Z" stroke-width="1.3"/>'),
  doc: svg('<path d="M6.5 3.5h11v17l-2.4-1.6-2.3 1.6-2.3-1.6-2.3 1.6-2.4-1.6Z"/><path d="M9.5 8h5M9.5 12h5"/>'),
};
