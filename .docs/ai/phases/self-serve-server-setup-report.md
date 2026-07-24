# Self-Serve Server Setup Report

## Delivered

- macOS + Tailscale provisioner with runtime path/MagicDNS discovery.
- Generated neutral `io.harnessvoice.*` LaunchAgents and renewal assets.
- Long-lived server token stored in macOS Keychain; no guided-setup dotenv.
- Optional BWS bootstrap passes the token through a sanitized child environment.
- SQLite pairing tickets: SHA-256 verifier, five-minute expiry, atomic single claim.
- Unauthenticated pairing claim exposed only when token and TLS are configured.
- Terminal QR contains version, HTTPS URL, pairing ID, and one-time secret only.
- iOS strict QR parser, claim-then-authenticate coordinator, native camera scanner.
- First-run onboarding and saved-server manager entry points; manual and Bonjour paths retained.

## Verification

- `uv run --extra dev pytest` from `backend/`: 292 passed.
- Self-serve backend files: ruff clean.
- `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`: succeeded.
- Focused profile + pairing tests: 40 passed.
- Full iOS suite on iPhone 17 simulator: 126 passed.
- Task reviews clean for Tasks 1–7; final adversarial review clean after fixes for real-harness launchd readiness and consumed-ticket retry.

## Pending physical acceptance

- Attempted `setup --from-bws` on Mandalore on 2026-07-23.
- Blocked after approval: the App Store Tailscale client cannot write certificate/key output outside its sandbox (`operation not permitted`), including `/tmp`.
- Existing Mandalore deployment remains on Tailscale Serve; direct-cert provisioning was not installed.
- Choose or implement a Serve-compatible pairing readiness path before retrying physical acceptance.
- Then scan from iPhone, confirm profile + real Hermes turn, reject replay/expiry, verify login restart, and exercise manual/Bonjour fallbacks.
- Do not call optional BWS integration accepted until the live generated wrapper runs without a secret dotenv.

## Known repository-wide verification issue

- Full `ruff check backend` reports existing diagnostics in unrelated adapter and legacy script files. Files introduced or modified for self-serve backend setup are clean.
