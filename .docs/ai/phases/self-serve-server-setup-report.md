# Self-Serve Server Setup Report

## Delivered

- macOS + Tailscale provisioner with runtime path/MagicDNS discovery.
- Guided setup owns persistent Tailscale Serve HTTPS and a loopback-only backend; direct-TLS remains an advanced manual mode.
- Serve runtime fails closed unless bind, proxy trust, certificate-mode, and non-empty-token invariants all hold.
- Generated neutral `io.harnessvoice.backend` LaunchAgent; obsolete renewal and legacy agents/assets are removed only after verified cutover.
- Long-lived server token stored in macOS Keychain; a hidden Security.framework launcher adds it only to the final backend process environment.
- Optional BWS bootstrap is one-shot only; the installed backend has no BWS or dotenv runtime dependency.
- Transactional migration snapshots files, Keychain, loaded labels, and exact Serve ownership; pre-commit failures roll back in reverse order.
- LaunchAgent plist staging occurs outside watched `~/Library/LaunchAgents`, on a verified same filesystem; bootout completion is confirmed before replacement/bootstrap.
- SQLite pairing tickets: SHA-256 verifier, five-minute expiry, atomic single claim.
- Pairing requires exact Serve or complete direct-TLS mode, public HTTPS/non-mock health, and authenticated available Hermes before issuing a QR; setup now runs a real no-tools/no-context Hermes inference before any mutation.
- Terminal QR contains version, HTTPS URL, pairing ID, and one-time secret only.
- iOS strict QR parser, claim-then-authenticate coordinator, native camera scanner.
- First-run onboarding and saved-server manager entry points; manual and Bonjour paths retained.

## Verification

- Final backend suite: 372 passed; changed backend files ruff clean.
- Final iOS suite on iPhone 17 simulator: 126 passed.
- Per-task reviews and whole-branch adversarial review clean after fixes.
- Live Mandalore migration succeeded with exact `https:8765 -> http://127.0.0.1:8765` Serve ownership.
- Public `/health`: `status: ok`, `scheme: https`, `mock: false`; authenticated Hermes readiness passed during setup and pair generation.
- Neutral LaunchAgent restart returned healthy; legacy and renewal labels/assets are absent.
- Two consecutive `setup --from-bws` runs from merged `main` succeeded, proving rerun idempotence and updating the installed working directory off the feature worktree.
- TestFlight 1.0 build 31 uploaded on 2026-07-25 with the QR scanner/setup flow.

## Physical acceptance

- Production QR scanned on iPhone; Mandalore profile saved and activated.
- Real Hermes voice turn completed after persisting `openai-codex` / `gpt-5.6-luna` at medium reasoning.
- Consumed-ticket replay and expired-ticket attempts were rejected without changing the active profile.
- Saved-server manual URL/token validation succeeded.
- Bonjour fallback remains tracked under the existing Bonjour/on-device roadmap item; it is not a blocker for the Tailscale QR setup path.

## Known repository-wide verification issue

- Full `ruff check backend` reports existing diagnostics in unrelated adapter and legacy script files. Files introduced or modified for self-serve backend setup are clean.
