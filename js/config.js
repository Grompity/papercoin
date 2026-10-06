/* ============================================================
   $PAPER — single source of truth (front half).
   every brand value lives here. the server keeps its own
   server/config.json; where both know a value, the server wins
   at runtime (it is authoritative), this file is the static
   fallback + the link registry.
   ============================================================ */

export const CONFIG = {
  /* identity */
  tokenName: "PAPER",
  ticker: "$PAPER",
  network: "Solana",
  tagline: "money, but internet.",
  motto: "the internet's financial paper",
  call: "post. earn. climb.",
  disclaimer:
    "$PAPER is a community-driven memecoin on Solana. Nothing on this site is financial advice.",

  /* contract (solana mint) */
  contractAddress: "E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk",

  /* buy destinations */
  jupiterUrl:
    "https://jup.ag/tokens/E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk",
  emberUrl:
    "https://embercurve.fun/t/E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk?ref=DzT4UWupdm7HhkmBukRz5Z2ssRf7YzDVnJ7VRskjVqpp",
  emberNote:
    "Buying through Ember gives the team additional referral fees.",

  /* socials */
  twitterUrl: "https://x.com/Papersol6?s=20",
  telegramUrl: "https://t.me/+xehlz9Ap11ZmOTUx",
  xHandle: "Papersol6",

  /* character — the canonical poses, as transparent cut-outs (alpha generated
     by server/tools/pose_alpha.py from the white-ground renders).
     every pose works a different moment; never paste the same one twice
     if a different job is at hand. */
  mascotImage: "public/paper-mascot.jpeg",      /* og/favicon/full-figure */
  poses: {
    front:     "public/assets/pose-front.png",      /* hero, front page */
    celebrate: "public/assets/pose-celebrate.png",  /* success / you scored */
    running:   "public/assets/pose-running.png",   /* scanning / connecting */
    cool:      "public/assets/pose-cool.png",       /* the rules / receipts */
    victory:   "public/assets/pose-victory.png",    /* prizes / #1 */
    silly:     "public/assets/pose-silly.png",      /* empty states / shrug */
  },

  /* paperboard — API is same-origin; server is the authority.
     (endpoints + fallback board below) */
  board: {
    base: "",
  },

  /* offline fallback for the front page preview when the server is down.
     clearly labeled DEMO in the UI — never sold as live. */
  demoBoard: [
    { rank: 1, handle: "degenledger",  points: 240, posts: 2 },
    { rank: 2, handle: "memecoinmom",  points: 214, posts: 3 },
    { rank: 3, handle: "chartwitch",   points: 159, posts: 2 },
  ],
};
