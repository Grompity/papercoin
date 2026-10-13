import { CONFIG } from "../config.js";
import { ICONS } from "../icons.js";

/* Board — the PAPERBOARD entry: the pitch, the flow, and the live system
   panel where the whole Connect-X experience runs. panel states are driven
   by js/paperboard.js (the app) which renders into [data-state] slots. */
export function Board() {
  const cfg = CONFIG;
  return `
    <section class="board section" id="board" aria-labelledby="board-title">
      <div class="rail">
        <div class="rule"><div class="rule__tag">
          <span>PAPERBOARD — the competition front page</span>
          <span>live · server-computed</span>
        </div></div>

        <div class="board__split">
          <div>
            <div data-reveal>
              <span class="plate">${ICONS.crown} Paperboard</span>
            </div>
            <h2 class="board__title" id="board-title" data-reveal>
              post paper.<br><span class="line2">earn points.</span><br>climb the front page.
            </h2>
            <p class="board__lede" data-reveal>
              you bring your OWN posts; the server reads, gates, and counts
              them — you get the points for your posts, not the neighborhood's.
            </p>

            <ol class="board__steps" data-reveal>
              <li class="board__step">
                <span class="board__step-n">01</span>
                <span><b>Create your account</b><span>sign in with your email and set up your profile.</span></span>
              </li>
              <li class="board__step">
                <span class="board__step-n">02</span>
                <span><b>Verify and submit</b><span>verify your X account, then submit links to your eligible posts.</span></span>
              </li>
              <li class="board__step">
                <span class="board__step-n">03</span>
                <span><b>Earn points</b><span>qualifying posts earn points toward your leaderboard position.</span></span>
              </li>
            </ol>

            <div class="board__ctas" data-reveal>
              <button type="button" class="btn btn--ghost" data-action="mock-login" hidden>
                Demo sign-in ${ICONS.x}
              </button>
              <button type="button" class="btn btn--ghost" data-action="logout" hidden>
                Sign out
              </button>
              <a class="btn btn--ghost" href="#frontpage">Front page ↓</a>
            </div>
          </div>

          <div class="board__art" data-reveal>
            <div class="panel" id="pb-panel" data-state="boot">
              <div class="panel__head">
                <span class="panel__live"><i aria-hidden="true"></i> system readout</span>
                <span data-slot="mode" aria-live="polite">…</span>
              </div>
              <div class="panel__body">

                <div class="panel__state is-active" data-state="boot" role="status">
                  <div class="p-idle">
                    <h3>press warming up…</h3>
                    <p>reading the front page.</p>
                  </div>
                </div>

                <div class="panel__state" data-state="idle">
                  <div class="p-join">
                    <h3>Sign in / Create account</h3>
                    <p>Enter your email and PAPERBOARD will send a secure,
                       one-time sign-in link — it signs you in and opens the
                       account on first use.</p>
                    <div class="p-join__form">
                      <label class="sr" for="pb-email-input">Email</label>
                      <input id="pb-email-input" type="email" inputmode="email" autocomplete="email"
                             spellcheck="false" placeholder="you@internet.com" maxlength="254">
                      <button type="button" class="btn btn--solid" data-action="join">Continue</button>
                    </div>
                    <p class="pb-spam-note">Your sign-in link may arrive in your spam or junk folder.
                       Check there if you don't see it in your inbox.</p>
                    <p class="p-join__err" data-slot="join-err" aria-live="polite"></p>
                  </div>
                </div>

                <div class="panel__state" data-state="email">
                  <div class="p-email">
                    <div class="p-idle__art"><img src="${cfg.poses.running}" alt="" aria-hidden="true" loading="lazy" decoding="async"></div>
                    <h3>Check your email.</h3>
                    <p>One link, short-lived, single use. Click it and this page
                       will know you.</p>
                    <p class="pb-spam-note">Your sign-in link may arrive in your spam or junk folder.
                       Check there if you don't see it in your inbox.</p>
                    <p class="p-email__link" data-slot="magic-link" aria-live="polite"></p>
                  </div>
                </div>

                <div class="panel__state" data-state="welcome">
                  <div class="p-welcome">
                    <h3>Welcome to PAPERBOARD.</h3>
                    <div class="p-welcome__fields">
                      <label class="sr" for="pb-username-input">Username</label>
                      <input id="pb-username-input" type="text" autocomplete="nickname"
                             spellcheck="false" placeholder="choose a username" maxlength="24">
                      <label class="sr" for="pb-xhandle-input">Your X handle</label>
                      <input id="pb-xhandle-input" type="text" autocomplete="off"
                             spellcheck="false" placeholder="your X handle (@ optional)" maxlength="16">
                      <label class="sr" for="pb-wallet-input">Solana reward wallet</label>
                      <input id="pb-wallet-input" type="text" inputmode="text" autocomplete="off"
                             spellcheck="false" placeholder="paste your Solana address" maxlength="48">
                    </div>
                    <button type="button" class="btn btn--solid" data-action="onboard">Enter PAPERBOARD</button>
                    <p class="p-wallet__note">${ICONS.wallet}
                       <span><b>Reward entries point at this Solana address.</b>
                       only the public address — no connect, no signature, no seed phrase,
                       no private key.</span></p>
                    <p>Your X handle rides in as a claim — you prove it once with a post.</p>
                    <p class="p-join__err" data-slot="onboard-err" aria-live="polite"></p>
                  </div>
                </div>

                <div class="panel__state" data-state="dash">
                  <div class="p-dash">
                    <div class="p-dash__head">
                      <h3 class="p-dash__name" data-slot="acct-name">…</h3>
                      <span class="p-dash__rank" data-slot="acct-rank" aria-live="polite">—</span>
                    </div>
                    <div class="score">
                      <span class="score__num" data-slot="points" aria-live="polite">0</span>
                      <span class="score__cap">points · <span data-slot="acct-comp">—</span></span>
                    </div>
                    <div class="metrics" data-slot="acct-stats"><!-- filled by app --></div>

                    <div class="p-dash__submit" id="pb-submit-box">
                      <label class="sr" for="pb-submit-input">X post URL</label>
                      <input id="pb-submit-input" type="text" inputmode="url" autocomplete="off"
                             spellcheck="false" placeholder="https://x.com/…/status/…">
                      <button type="button" class="btn btn--solid" data-action="submit-post">Post it ${ICONS.x}</button>
                    </div>
                    <p class="p-xverify__gate" data-slot="submit-gate" hidden>
                      X verification first — once the proof post is read, the contribution desk opens here.</p>
                    <div class="p-dash__verdict" data-slot="submit-verdict" aria-live="polite" hidden></div>

                    <div class="p-dash__xverify" id="pb-xstrip" hidden>
                      <div class="p-dash__submit">
                        <span class="p-dash__swap" data-slot="x-status">X: —</span>
                      </div>
                      <div class="p-xverify__step" id="pb-xverify-step">
                        <p class="p-xverify__tag" data-slot="x-step">X verification required</p>
                        <p class="p-xverify__why">Publish the verification post below from the X
                           account you want bound to PAPERBOARD — the server reads the post
                           itself, so its words and its byline are the deed.</p>
                        <pre class="p-xverify__prompt" data-slot="x-prompt" aria-live="polite">I just signed up for PAPERBOARD, a new raiding platform by @paperusdc — points for posts, on the board!</pre>
                        <p class="p-xverify__copyline">
                          <button type="button" class="btn btn--ghost" data-action="copy-example">Copy verification post</button>
                          <span class="p-xverify__copy" data-slot="copy-feedback" aria-live="polite"></span>
                        </p>
                        <p class="p-xverify__next">Once you've published it, paste its link below — this happens once per account, and the post never needs to stay special.</p>
                        <div class="p-dash__submit">
                          <label class="sr" for="pb-xverify-input">X verification post URL</label>
                          <input id="pb-xverify-input" type="text" inputmode="url" autocomplete="off"
                                 spellcheck="false" placeholder="paste the verification post's link">
                          <button type="button" class="btn btn--solid" data-action="x-verify">I've posted it — verify my X account</button>
                        </div>
                      </div>
                      <div class="p-dash__verdict" data-slot="xverify-verdict" aria-live="polite" hidden></div>
                    </div>

                    <div class="p-dash__ledger">
                      <p class="p-dash__ledger-head">recent earnings</p>
                      <div data-slot="acct-recent"><!-- filled by app --></div>
                      <p class="p-dash__ledger-head">reward history</p>
                      <div data-slot="acct-rewards"><!-- filled by app --></div>
                    </div>

                    <button type="button" class="btn btn--ghost" data-action="toggle-submissions"
                            aria-expanded="false" aria-controls="pb-submissions">
                      View my submissions ${ICONS.chevron}
                    </button>
                    <div class="breakdown" id="pb-submissions" hidden data-slot="submissions"><!-- filled by app --></div>
                  </div>
                </div>

                <div class="panel__state" data-state="wallet">
                  <div class="p-wallet">
                    <h3>Where should rewards land?</h3>
                    <div class="p-wallet__form">
                      <label class="sr" for="pb-wallet-settings-input">Solana wallet address</label>
                      <input id="pb-wallet-settings-input" type="text" inputmode="text" autocomplete="off"
                             spellcheck="false" placeholder="new Solana address" maxlength="48">
                      <button type="button" class="btn btn--solid" data-action="change-wallet">Save</button>
                    </div>
                    <p class="p-wallet__note">${ICONS.wallet}
                       <span>a wallet change waits out a cooldown before rewards
                       may ride it — and needs a freshly clicked magic link.</span></p>
                    <p class="p-join__err" data-slot="wallet-err" aria-live="polite"></p>
                  </div>
                </div>

                <div class="panel__state" data-state="scan">
                  <div class="p-scan">
                    <div class="p-scan__art"><img src="${cfg.poses.running}" alt="" aria-hidden="true" loading="lazy" decoding="async"></div>
                    <h3>Scanning your posts…</h3>
                    <p class="p-scan__log" data-slot="scan-log">checking the gate → follow first, then one identifier…</p>
                  </div>
                </div>

                <div class="panel__state" data-state="none">
                  <div class="p-none">
                    <div class="p-idle__art"><img src="${cfg.poses.silly}" alt="Paper, shrugging at an empty front page" loading="lazy" decoding="async"></div>
                    <h3>No qualifying posts yet.</h3>
                    <p>Paste an X post about ${cfg.ticker} — mention the CA,
                       @paperusdc or $paper — and watch the server think.</p>
                    <div class="p-idle__cta">
                      <button type="button" class="btn btn--pill" data-action="rescan">Scan again</button>
                    </div>
                  </div>
                </div>

                <div class="panel__state" data-state="err">
                  <div class="p-err">
                    <h3>The scanner hit a snag.</h3>
                    <p data-slot="err">—</p>
                    <div class="p-idle__cta">
                      <button type="button" class="btn btn--pill" data-action="rescan">Try again</button>
                    </div>
                  </div>
                </div>

                <div class="panel__state" data-state="off">
                  <div class="p-none">
                    <h3>Off press.</h3>
                    <p>The PAPERBOARD server isn't running. the front page shows a
                       demo board until you start it.</p>
                  </div>
                </div>

              </div>
            </div>
          </div>
        </div>
      </div>
    </section>`;
}
