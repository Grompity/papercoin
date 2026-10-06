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
              qualify by posting about ${cfg.ticker} on X — the contract address,
              <b>@paperusdc</b>, or <b>$paper</b> (any one of them). you must follow
              <b>@paperusdc</b>. everything else is the server's business: metrics,
              eligibility, points, rankings, reward estimates.
            </p>

            <ol class="board__steps" data-reveal>
              <li class="board__step">
                <span class="board__step-n">01</span>
                <span><b>Connect X</b><span>proper OAuth, server-side. your secrets never touch this page.</span></span>
              </li>
              <li class="board__step">
                <span class="board__step-n">02</span>
                <span><b>Drop a Solana address</b><span>no wallet connect, no signing, no seed phrase — just where prizes would land.</span></span>
              </li>
              <li class="board__step">
                <span class="board__step-n">03</span>
                <span><b>We scan your posts</b><span>discovery, follow-check, engagement metrics, points — all computed behind the API, never by you.</span></span>
              </li>
              <li class="board__step">
                <span class="board__step-n">04</span>
                <span><b>The board is official</b><span>standings are snapshots, refreshed on a schedule — not real-time, on purpose.</span></span>
              </li>
            </ol>

            <div class="board__ctas" data-reveal>
              <button type="button" class="btn btn--solid" data-action="connect">
                Connect X ${ICONS.x}
              </button>
              <button type="button" class="btn btn--ghost" data-action="mock-login" hidden>
                demo sign-in ${ICONS.x}
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
                  <div class="p-idle">
                    <h3>Ready when you are.</h3>
                    <p>Connect X and we'll run your recent posts through the gate.
                       No wallet dance — the server does the heavy reading.</p>
                    <div class="p-idle__cta">
                      <button type="button" class="btn btn--solid" data-action="connect">
                        Connect X ${ICONS.x}
                      </button>
                    </div>
                  </div>
                </div>

                <div class="panel__state" data-state="connect">
                  <div class="p-scan">
                    <h3>Redirecting to X…</h3>
                    <p>OAuth 2.0 — the handshake happens off this page.</p>
                  </div>
                </div>

                <div class="panel__state" data-state="wallet">
                  <div class="p-wallet">
                    <h3>Where should prizes land?</h3>
                    <div class="p-wallet__form">
                      <label class="sr" for="pb-wallet-input">Solana wallet address</label>
                      <input id="pb-wallet-input" type="text" inputmode="text" autocomplete="off"
                             spellcheck="false" placeholder="paste your Solana address" maxlength="48">
                      <button type="button" class="btn btn--solid" data-action="save-wallet">Save</button>
                    </div>
                    <p class="p-wallet__note">${ICONS.wallet}
                       <span>only the <b>public address</b> — for prize delivery.
                       we never ask for a signature, a seed phrase, or permission to touch your wallet.</span></p>
                  </div>
                </div>

                <div class="panel__state" data-state="scan">
                  <div class="p-scan">
                    <div class="p-scan__art"><img src="${cfg.poses.running}" alt="" aria-hidden="true" loading="lazy" decoding="async"></div>
                    <h3>Scanning your posts…</h3>
                    <p class="p-scan__log" data-slot="scan-log">checking the gate → follow first, then one identifier…</p>
                  </div>
                </div>

                <div class="panel__state" data-state="result">
                  <div class="p-result">
                    <div class="p-result__hero">
                      <div class="score">
                        <span class="score__num" data-slot="points" aria-live="polite">0</span>
                        <span class="score__cap">points, computed by the server</span>
                      </div>
                      <div class="p-result__art"><img src="${cfg.poses.celebrate}" alt="" aria-hidden="true" loading="lazy" decoding="async"></div>
                    </div>
                    <div class="metrics" data-slot="metrics"><!-- filled by app --></div>
                    <p class="status-line" data-slot="status" aria-live="polite"></p>
                    <button type="button" class="btn btn--ghost" data-action="toggle-breakdown"
                            aria-expanded="false" aria-controls="pb-breakdown">
                      View scoring breakdown ${ICONS.chevron}
                    </button>
                    <div class="breakdown" id="pb-breakdown" hidden data-slot="breakdown"><!-- filled by app --></div>
                  </div>
                </div>

                <div class="panel__state" data-state="none">
                  <div class="p-none">
                    <div class="p-idle__art"><img src="${cfg.poses.silly}" alt="Paper, shrugging at an empty front page" loading="lazy" decoding="async"></div>
                    <h3>No qualifying posts yet.</h3>
                    <p>Post about ${cfg.ticker} — mention the CA, @paperusdc or $paper —
                       and send us back through the scanner.</p>
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
