# PAPERBOARD wallet login backend — prepared, not deployed

Target repository: Grompity/papercoin
Base commit: d5495c81902bd0602a145a166a901b2f0854af7e

## What this patch does

Adds Solana Ed25519 message authentication to the existing account/session layer. Account IDs, submissions, points, X verification, and email magic-link login stay authoritative in the existing posting database. A reward wallet address alone never authenticates an account.

Existing users first authenticate their original email account, then sign a wallet-link challenge with that same account session. Later wallet signatures sign in to that same immutable account ID. One account can bind one wallet, and a wallet can bind one account. Wallet changes/recovery are not included in this first patch.

New users explicitly choose to create a new posting account. No account is created until signature confirmation. The existing non-null email schema is preserved with a reserved internal identifier at wallet.paperboard.invalid. This is not an email address users can receive mail at, is never email-verified, is hidden from the account response, and is rejected by the email-start route. Email recovery for wallet-only accounts is not implemented.

Challenges include configured origin, wallet, action, account ID, nonce, and expiry. They last five minutes. Confirmations verify a 64-byte Ed25519 signature using cryptography, then atomically consume the challenge and bind/create the account. Wrong signatures, replay, expired proofs, closed accounts, binding conflicts, and form submissions are rejected. Challenges are rate-limited. Successful confirmation issues the existing HttpOnly PBSD session cookie. This requests no transaction or spending allowance.

## Deployment steps for the posting-server owner

1. Back up the existing posting SQLite database using your normal SQLite-aware backup procedure. Keep the same database and existing environment configuration.
2. Review wallet-login-backend.patch. From the repository root run `git apply --check wallet-login-backend.patch`, then `git apply wallet-login-backend.patch`. If the repository has advanced, resolve the changes against the current code rather than overwriting newer work.
3. Install the additional dependency in the actual backend Python environment: `python -m pip install -r server/requirements-wallet.txt`. This requirement is separate because the existing project uses the standard library. Include it in the backend build/install command.
4. Run `python -m unittest discover -s server -p test_wallet_auth.py` — expected: 18 passing tests. Run the project's normal checks too; see the baseline issues below.
5. Deploy with `PAPER_WALLET_LOGIN` unset or `0` initially. The new tables are additive and wallet login stays disabled. Existing email login continues to work.
6. Confirm `PAPER_PUBLIC_BASE_URL` is the correct trusted HTTPS posting origin, not user-supplied request data. This value appears in signed messages. Preserve the existing secure-cookie configuration.
7. Coordinate with the PAPER Swap integration developer before enabling `PAPER_WALLET_LOGIN=1`. The Render bridge and wallet-signing interface must be integrated and verified against this backend contract first. This patch does not itself update either website's sign-in interface.
8. Verify source `/api/config` reports `walletAuth: {enabled: true, version: 1}` after enabling. Test a linked existing account and a new test account end-to-end; verify original account IDs and posting points remain the same.

No passwords, private keys, API keys, or database copies should be sent through chat.

## API contract for the Render bridge

- GET `/api/config`: additive `walletAuth.enabled` and `walletAuth.version` capability. Show wallet controls only when enabled/version 1.
- POST `/api/account/wallet-auth/challenge`: JSON `{wallet, createNew?: true}`. For linking an existing account, forward that account's already authenticated PBSD cookie. For normal wallet login, omit the cookie. Response `{nonce, message, expiresAt, intent}`. Unknown wallets require an explicit create-new choice or an email-account link first.
- Sign the exact UTF-8 message returned by the source with the connected wallet's message-signing method. Check wallet identity did not change between challenge and signature. Never construct a replacement message in the browser and never request a transaction signature.
- POST `/api/account/wallet-auth/confirm`: JSON `{nonce, signature}` where signature is canonical base64 for the 64 bytes. A link confirmation must forward the original account cookie again. Success JSON `{authenticated: true, accountId}` plus the existing PBSD Set-Cookie header. Keep this cookie server-side and encrypted using Render's existing session store; do not return it to browser JavaScript.
- GET `/api/account`: additive `account.login_wallet` derived only from the signature-verified binding table. This is distinct from the existing reward `account.wallet`. Wallet-only accounts have `email: null` and `email_verified: false`.
- A Render connection may accept a wallet-only source account only when `login_wallet` exactly equals the authenticated beta wallet and its immutable account ID matches the confirmation. Keep the existing one-to-one local point-attribution constraints. Never accept the reward wallet as proof.
- If Render already remembers an account ID for this wallet, do not offer creation of a replacement account. Reconnect/link the original account using its existing email flow when necessary.
- Preserve the current X-verification, submission eligibility, duplicate detection, scoring, and public leaderboard refresh schedule. A wallet signature does not prove X post ownership.

## Rollback

Set `PAPER_WALLET_LOGIN=0` and restart the posting backend to disable new wallet proofs without deleting data. Existing sessions and email login remain. Wallet-only users cannot start a new wallet session while disabled. Do not delete the additive tables or recreate the database. If reverting code, retain database backups and binding records for restoration.

## Validation performed locally

- 18 wallet-auth tests passed, including real Router/Service/SQLite session issuance, original email login/linking, existing ID/point preservation, disabled behavior, unsupported content types, reserved email rejection, wrong signatures, replay, expiry, binding conflicts, and closed accounts.
- Python compilation and `git diff --check` passed.
- The existing PAPER Swap account, points, API, and UI test files passed unchanged (4 test files).
- Full upstream `server/selftest.py` still reports the same five failures observed on the unchanged source before this patch: four P3 metric-refresh/competition-timing cases and the P6V clipboard fallback case. No new failure names appeared. This patch does not claim the entire upstream suite passes.

## Remaining integration work

The backend is prepared and tested locally, not installed at api.seyruna.com. The Render-side wallet challenge/confirmation bridge, encrypted-session attachment, wallet-first interface, and browser end-to-end checks still need implementation against the deployed/approved contract. Current production email login remains unchanged. The existing original posting site also remains email-first until its interface is separately updated.
