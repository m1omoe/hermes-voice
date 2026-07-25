# Tailscale Serve Default for Guided macOS Setup

## Status

Approved after adversarial review by Opus 5 and `ollama-cloud/glm-5.2` in OMP.

## Goal

Make guided self-serve setup work with the macOS App Store Tailscale client by using persistent Tailscale Serve for HTTPS termination instead of writing certificate files outside Tailscale's sandbox.

## Scope

- Guided macOS setup uses Tailscale Serve by default.
- Direct certificate/key TLS remains supported for advanced manual deployments.
- Existing QR, manual URL/token, and Bonjour onboarding paths remain.
- The existing Mandalore deployment using Tailscale Serve and the legacy `dev.finklea.harnessvoice.backend` agent can migrate safely.
- No Linux, Windows, Docker, LAN-only guided setup, or public Internet exposure.

## Threat model

Network peers, including tailnet peers, must not reach the backend's unauthenticated pairing claim route over direct plaintext HTTP or spoof proxy headers.

Processes running as the same macOS user are trusted. Tailscale Serve cannot be cryptographically distinguished from another same-user process that connects over loopback and sends proxy headers; this is an explicit accepted boundary.

Long-lived server tokens remain in macOS Keychain. The QR contains only the HTTPS URL and a short-lived, one-time pairing credential.

## Deployment modes

### Guided Serve mode

The generated backend LaunchAgent:

- binds exactly to `127.0.0.1:8765`;
- has no `HERMES_VOICE_CERT` or `HERMES_VOICE_KEY`;
- sets `HERMES_VOICE_TRUSTED_HTTPS_PROXY=tailscale-serve`;
- resolves Hermes to an absolute executable path;
- reads the backend token from Keychain without placing it in argv or generated files;
- enables Uvicorn proxy-header processing with `forwarded_allow_ips=127.0.0.1` only.

Startup rejects Serve mode unless the bind is exactly loopback, local cert/key settings are absent, and the proxy allowlist is exactly loopback. Inherited environment values cannot broaden proxy trust.

### Advanced direct-TLS mode

Direct TLS continues to require a non-empty token plus certificate and key. Proxy-header processing is disabled. Existing manual deployments remain valid and are outside guided setup generation.

## Pairing and external scheme

Pairing is enabled only when a non-empty token exists and either:

1. direct TLS has both certificate and key; or
2. validated Serve mode is active.

The claim route continues to require an effective HTTPS request. In Serve mode, Uvicorn accepts `X-Forwarded-Proto` only from `127.0.0.1`.

Bonjour and health reporting use the externally served scheme. Serve mode advertises the MagicDNS hostname with HTTPS on port 8765 even though FastAPI receives loopback HTTP.

## Serve configuration

Guided setup uses this non-destructive command when no mapping exists:

```bash
tailscale serve --bg --https=8765 http://127.0.0.1:8765
```

It never invokes `tailscale serve reset`.

Before mutation, setup parses:

```bash
tailscale serve status --json
```

The desired state is exactly:

- `TCP["8765"].HTTPS == true`
- `Web["<MagicDNS>:8765"].Handlers["/"].Proxy == "http://127.0.0.1:8765"`

Policy:

- exact mapping: reuse without mutation;
- absent mapping: add it and record ownership for rollback;
- any conflicting TCP/Web mapping on port 8765: abort before changing agents, files, Keychain, or Serve state.

If this setup run added the mapping and a later step fails, remove only that mapping using the matching `tailscale serve --https=8765 http://127.0.0.1:8765 off` form. Never alter unrelated Serve ports, hosts, paths, or services.

## Transactional migration

Setup snapshots internally, without printing secrets:

- generated wrapper and plist contents;
- loaded state for neutral and recognized legacy backend/renewal agents;
- the prior Keychain token state/value;
- the pre-existing Serve status relevant to port 8765.

Execution order:

1. Discover and validate macOS, Tailscale login/MagicDNS, `uv`, and real Hermes.
2. Parse Serve status and abort on conflicts.
3. Render new wrapper/plist to temporary files and validate them.
4. Obtain and stage the Keychain token.
5. Add the Serve mapping only if absent.
6. At cutover, boot out the prior generated or recognized legacy backend agent, atomically install the staged assets, and bootstrap the neutral backend agent.
7. Verify the exact Serve mapping and public HTTPS behavior.
8. Verify `/health` reports HTTPS and non-mock mode.
9. Verify authenticated `/api/harnesses` contains available real Hermes.
10. Commit the migration, then boot out and remove obsolete generated renewal assets and recognized legacy generated assets.

Any failure after mutation restores the previous files, Keychain token, agent loaded state, and any Serve mapping added by this run. The old backend is restarted when it was previously loaded. Cleanup failure after successful service verification is reported explicitly and does not falsely claim full migration.

The recognized legacy migration target is `dev.finklea.harnessvoice.backend`; unrelated user LaunchAgents are never removed.

## Pair command

`pair`:

1. discovers the same absolute Hermes and MagicDNS state;
2. reads the token from Keychain and refuses if absent;
3. verifies the exact Serve mapping or valid direct-TLS deployment;
4. calls public HTTPS `/health` and requires HTTPS plus non-mock mode;
5. calls authenticated `/api/harnesses` and requires available real Hermes;
6. only then creates and renders the five-minute one-time QR.

A failed readiness check creates no ticket.

## Generated assets

Serve-mode guided setup generates only the neutral backend wrapper and `io.harnessvoice.backend` plist. It does not generate or bootstrap a certificate-renewal agent.

After successful migration, it boots out and removes obsolete generated renewal scripts/plists. Existing certificate files may be left untouched because they are non-secret public/private TLS material owned by the previous deployment; the private key remains permission-restricted. Removal is not required for correctness.

## Error handling

Errors are non-secret and identify the failed boundary:

- Tailscale unavailable, logged out, or MagicDNS missing;
- conflicting Serve mapping;
- missing real Hermes;
- failed staged asset validation;
- LaunchAgent cutover failure;
- public HTTPS or scheme failure;
- mock backend or authenticated harness failure;
- rollback failure.

The pairing secret exists only in memory and inside the rendered QR payload, where it is required for claiming. It is never printed separately or disclosed in plaintext logs, prompts, exceptions, reports, or subprocess argv. The long-lived backend token is never present in any QR or output.

## Verification

### Backend configuration and routing

- Serve mode requires token, exact loopback bind, absent cert/key, exact trusted-proxy enum, and loopback-only forwarded allowlist.
- Invalid combinations fail startup.
- Direct TLS remains enabled and does not trust proxy headers.
- Ordinary HTTP remains unable to claim pairing credentials.
- A loopback proxy request with the allowed forwarded HTTPS scheme can claim once.
- Bonjour and public health scheme are HTTPS in Serve mode.

### Renderer and provisioner

- Serve wrapper is loopback-only, contains no cert paths, uses absolute Hermes, and fixes proxy trust to loopback.
- Exact existing Serve mapping is a no-op.
- Missing mapping is added with the exact persistent command.
- Conflicting 8765 mapping aborts without mutation.
- `serve reset` is never invoked.
- Every failure point after cutover restores prior files, token state, agents, and newly added mapping.
- Neutral and recognized legacy backend migration avoids port collision.
- Renewal agent is removed only after successful verification.
- `pair` requires Keychain token, public HTTPS, non-mock mode, and real authenticated Hermes before ticket creation.

### Regression

- Existing direct-TLS pairing tests remain green.
- Backend full suite and Ruff pass for changed files.
- iOS pairing/profile suites remain green; no iOS contract changes are expected.
- Live Mandalore acceptance verifies setup migration, Serve status, QR scan, real turn, replay/expiry rejection, login restart, and manual/Bonjour fallbacks.

## Review resolutions

The adversarial reviews required revisions for proxy-header enforcement, loopback binding, pairing eligibility, external mDNS scheme, Serve conflict ownership, migration rollback, renewal cleanup, and `pair` readiness. This design incorporates those findings. The accepted local trust boundary is same-user macOS processes.
