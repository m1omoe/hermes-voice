# Self-Serve macOS Server Setup and QR Pairing — Design

## Goal

Let a Harness Voice user set up a new macOS backend without manually adapting repository paths, launchd plist files, Tailscale certificates, or an authentication token. The iPhone guides the user to copy a safe prompt into a local coding harness; that harness provisions the local backend and shows a one-time pairing QR code. Scanning the code creates a tested server profile.

The first release targets macOS with Tailscale only. It supports public users and offers a Bitwarden Secrets Manager shortcut for the maintainer's own machines.

## Problem

The app currently discovers and connects to an already running backend, but server installation is manual. The checked-in launchd template also contains one machine's checkout path, home directory, and Tailscale certificate host. A fresh Mac can therefore have Hermes running in a terminal while no Harness Voice listener exists, as occurred on Mandalore.

## Decisions

- Use an in-app, copyable harness prompt rather than remote machine control.
- Pair through a short-lived, single-use QR credential; never encode the backend's long-lived token in the QR code.
- Run the portable setup path on macOS + Tailscale only for v1.
- Generate a distinct per-server token for public setup and store it in the macOS Keychain.
- On a registered personal machine, permit an opt-in BWS bootstrap into the same Keychain slot without printing a secret.
- Retain Bonjour discovery and manual URL/token profile entry as fallbacks.

## User journey

1. A new user reaches onboarding, or an existing user opens Saved Servers and chooses to add one.
2. They select **Set up a Mac server**. The app explains the macOS, Tailscale, and local-harness prerequisites and copies a versioned, non-secret setup prompt.
3. The user pastes that prompt into a coding harness running on the target Mac. The harness clones or updates the official repository and runs the first-party provisioner.
4. The provisioner stops for interactive Tailscale login, macOS authorization, or BWS access. It never asks the user to paste secrets into the iPhone or chat.
5. Once HTTPS is healthy, the provisioner generates a one-time pairing record and displays a terminal QR code.
6. The user returns to the app, scans the code, and the app claims it over HTTPS.
7. The app validates the returned credential with the existing authenticated harness-list call, then saves the resulting server profile.

## Portable provisioner

A first-party, documented macOS provisioning command owns setup behavior. It must:

- discover the current home directory, repository location, `uv` executable, Tailscale DNS name, and local certificate paths at runtime;
- check macOS, Tailscale/MagicDNS, `uv`, and the requested harness; explain the unmet prerequisite and stop rather than guessing;
- request a Tailscale-issued certificate for the machine's `*.ts.net` name and arrange renewal;
- generate launchd service definitions from the detected paths instead of installing the checked-in personal template;
- install/start the backend and certificate-renewal agents only after user approval;
- verify `https://<machine>.ts.net:8765/health` after startup; and
- generate and render a pairing QR code only after verification succeeds.

The generated launchd program invokes a small wrapper that reads the backend token from the macOS Keychain and exports it only to the backend process. Neither the generated plist nor the wrapper contains a token. The provisioner stores public-user tokens in a per-server Keychain item. On personal machines, an explicit BWS path retrieves the managed value through `bws-project` and writes it to that Keychain item without emitting it in output.

No secret-bearing dotenv file is created by the provisioning flow.

## Pairing protocol

### QR payload

The terminal QR encodes a versioned Harness Voice pairing payload containing:

- HTTPS base URL;
- pairing identifier; and
- high-entropy pairing secret.

It does not include a backend token, BWS credential, API key, repository path, or other runtime configuration.

### Backend state and claim

The backend persists pairing records in its own local store, separate from schedules and conversations. A record includes an identifier, a verifier derived from the pairing secret, creation and expiry times, and consumed state. Records have a short fixed lifetime and are pruned on creation/claim/startup.

A deliberately narrow unauthenticated HTTPS claim endpoint accepts the pairing identifier and secret. It atomically verifies that the record exists, has not expired, and has not already been consumed; it then marks the record consumed and returns the normal backend token. Replayed, expired, malformed, and unknown claims fail without exposing token or record state. The endpoint must not log the QR secret or backend token.

The app scans and validates the payload locally, claims it only over HTTPS, and uses the returned token for the normal authenticated harness-list check. A profile is saved only after that check succeeds.

## iPhone experience

### Entry points

- **Onboarding:** add **Set up a Mac server** alongside existing Bonjour/manual connection choices.
- **Saved Servers:** expose the same setup sheet from the Add Server path, so a second Mac does not require resetting onboarding.

### Setup sheet

The sheet communicates the prerequisites and the local-harness handoff. **Copy setup prompt** places only public, versioned instructions on the clipboard. It makes clear that setup happens on the target Mac and that the user may be asked to approve Tailscale, Keychain, or Bitwarden access.

After copying, **Scan pairing code** opens the scanner. The scanner asks for camera permission only when used and rejects non-Harness Voice or malformed QR payloads before networking.

### Failure states

The UI distinguishes camera denial, invalid code, expired/already-used code, Tailscale/TLS reachability failure, and successful claim with no available harness. Each state offers an appropriate retry. Bonjour and manual URL/token entry remain usable throughout.

## Security and privacy

- The prompt, QR payload, diagnostics, pairing errors, and logs never contain the long-lived backend token.
- Pairing secrets are high entropy, stored as a verifier where practical, short lived, single use, and removed after redemption or expiry.
- The claim endpoint is HTTPS-only. This feature adds no certificate bypass and no tokenless general API access.
- The app stores a claimed backend token using the existing profile storage model; migrating profile credentials to the iOS Keychain is outside this feature.
- BWS is optional and local to the provisioner. The app does not receive or handle a BWS credential.

## Testing and verification

### Automated backend tests

- pairing creation, expiry pruning, and atomic single redemption;
- replay, expired, malformed, and concurrent claim rejection;
- token/secret non-disclosure in responses and logs; and
- claim response contract and authenticated follow-up behavior.

### Automated provisioner tests

- dynamic generation for arbitrary user/repository paths and Tailscale DNS names;
- prerequisite failures and explicit no-op stop states;
- Keychain-backed, non-secret wrapper generation; and
- no retained Scadrial- or maintainer-specific paths in generated output.

### Automated iOS tests

- QR payload parser accepts only the expected versioned shape;
- malformed, expired, and claim-failure mapping;
- a profile saves only after the authenticated harness check; and
- existing manual/Bonjour profile paths remain unchanged.

### Manual acceptance

1. On a clean macOS machine with Tailscale and a supported local harness, copy the app prompt into the harness and finish the guided setup.
2. Confirm the service survives terminal exit, login, and a restart; confirm the Tailscale HTTPS health URL responds.
3. Scan the terminal QR, confirm the profile is saved and a turn reaches that Mac.
4. Attempt to scan the same QR again and after expiry; both must fail safely.
5. Confirm a public machine can use a generated Keychain token and a personal machine can choose the BWS shortcut.
6. Confirm manual URL/token and Bonjour setup still work.

## Out of scope

- Linux, Docker, Windows, and LAN-only/HTTP setup paths.
- Remote unattended machine control from the iPhone.
- Replacing the existing profile-token storage model with iOS Keychain storage.
- Multi-backend APNs identity/routing.
- Automatic installation or login without user approval.
