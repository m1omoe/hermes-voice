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

- `uv run --extra dev pytest` from `backend/`: 287 passed.
- Self-serve backend files: ruff clean.
- `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`: succeeded.
- Focused profile + pairing tests: 40 passed.
- Full iOS suite on iPhone 17 simulator: 125 passed.
- Task reviews clean for Tasks 1–7 after recorded fix rounds.

## Pending physical acceptance

- On Mandalore, run guided `setup`, then `pair`.
- Scan from an iPhone; confirm profile creation and a real Hermes turn.
- Confirm pairing replay and expiry fail.
- Confirm backend restarts after login/restart.
- Confirm manual URL/token and Bonjour fallbacks remain usable.
- Do not call optional BWS integration accepted until the live generated wrapper runs without a secret dotenv.

## Known repository-wide verification issue

- Full `ruff check backend` reports existing diagnostics in unrelated adapter and legacy script files. Files introduced or modified for self-serve backend setup are clean.
