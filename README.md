# $PAPER — paper website

a premium single-page site for the **$PAPER** solana memecoin.
vanilla html/css/js — no build step, es modules, one brand stylesheet.

## run it

serve the `Paper Website` folder (es modules need http, not file://):

```sh
python3 -m http.server 5199
# open http://localhost:5199
```

## structure

```
index.html              shell + fonts + meta
css/paper.css           the entire visual system
js/main.js              mount + all interaction wiring
js/config.js            ALL brand info (CA, buy links, socials) — edit here only
js/icons.js             tiny inline svg marks
js/components/          Navbar · Hero · Marquee · About · CharacterSection
                        · BuySection · Community · FutureSection · Footer
public/paper-mascot.jpeg the character
```

## update the links

everything lives in `js/config.js`:

| key            | what it is                                                        |
| -------------- | ----------------------------------------------------------------- |
| `jupiterUrl`   | Jupiter token page (`jup.ag/tokens/CA`)                           |
| `emberUrl`     | Ember referral link (`embercurve.fun/t/CA?ref=…`)                  |
| `twitterUrl`   | X (`x.com/Papersol6`)                                              |
| `telegramUrl`  | Telegram invite (`t.me/+xehlz9Ap11ZmOTUx`)                         |

token CA: `E5Gbf7q7uHeXQ1ySSPpPiYxF1da1ZL7NaCGUYQwgA8yk`

## notes

- BUY in the hero scrolls to the dedicated **buy** section — the two purchase
  cards (Jupiter / Ember) open in a new tab; the Ember card is the one that
  carries the transparent referral-fee note.
- the CA pill (hero + footer) copies the full address; brief `COPIED` feedback.
- the mascot jpeg has its own dark ground, so the art direction feathers it
  into the page as a cut-out (hero spotlight, character crop, circle crops)
  instead of boxing it.
- respects `prefers-reduced-motion`.
