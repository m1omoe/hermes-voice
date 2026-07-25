# Backend

FastAPI service mediating between the iOS app and the local Hermes CLI.

See the [top-level README](../README.md) for full setup.

## Guided macOS setup

Use the app's **Set up a Mac server** prompt in a local coding harness, or run
the first-party provisioner directly. Run these commands from the repository
root (`hermes-voice/`), because the command uses the `backend/scripts/...` path:

```bash
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup --from-bws
uv run --project backend --extra provision python backend/scripts/provision_macos.py pair
```

The provisioner discovers the current checkout and Tailscale MagicDNS name,
binds the backend to loopback, reuses or adds the exact Tailscale Serve HTTPS
mapping to `127.0.0.1:8765`, stores the token in Keychain, and installs one
`io.harnessvoice.backend` LaunchAgent after confirmation. It never generates or
requests a certificate file — Tailscale Serve owns TLS termination and
renewal. The checked-in `launchd/` files are non-operational deprecation
stubs.

`pair` renders a short-lived pairing QR only after every readiness gate
passes: a server token already in Keychain, an exact non-conflicting Serve
mapping (or, for the advanced manual direct-TLS runtime, both
`HERMES_VOICE_CERT` and `HERMES_VOICE_KEY` set), a public `/health` reporting
`scheme: https` and `mock: false`, and an authenticated `/api/harnesses`
listing showing Hermes available. The pairing secret exists only in memory
and inside the QR payload — it is never printed separately.

Setup is macOS + Tailscale only. A conflicting existing Serve mapping on port
8765 stops setup before any local mutation; a failure after the mapping or
LaunchAgent changes take effect (public health, authenticated harness check)
rolls back everything setup changed and restores whatever was previously
running. Rerun `setup` after resolving a prerequisite, or `pair` to issue
another five-minute code. Manual URL/token and Bonjour remain available when
camera pairing cannot be used.

Inspect the service without exposing credentials:

```bash
tailscale serve status --json
launchctl print "gui/$(id -u)/io.harnessvoice.backend"
tail -F "$HOME/Library/Logs/Harness Voice/backend.log"
curl -s "https://$(tailscale status --json | jq -r '.Self.DNSName | rtrimstr(\".\")'):8765/health" | jq
```

`tailscale serve status --json` only reads Serve state; it is always
non-destructive to run. Do not retrieve or print the Keychain token for
diagnostics.


## Advanced local development

```bash
uv sync --extra dev               # base + test deps
uv sync --extra dev --extra local # add faster-whisper + piper
uv run python -m app              # start loopback server (auto-loads backend/.env)
uv run pytest -q                  # run tests (env-isolated per test)
```

`backend/.env` is for advanced local development only; the guided provisioner
never reads or writes it. Shell env vars still win over `.env` when both are set.

Direct-TLS is still supported as a manual runtime configuration: set both
`HERMES_VOICE_CERT` and `HERMES_VOICE_KEY` to an existing certificate/key pair
and Uvicorn terminates TLS itself. The guided provisioner never manages a
certificate for this path — bring your own and renew it yourself.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/health` | — | `{status, mock, hermes, stt, tts}` |
| `POST` | `/api/text` | `{text, session_id?}` | `TurnResponse` |
| `POST` | `/api/audio` | multipart `file=<audio>` `session_id=<id>?` | `TurnResponse` |
| `GET` | `/api/audio/{id}` | — | audio bytes (mp3 or wav) |

`TurnResponse`:
```json
{
  "session_id": "20260524_153729_be415a",
  "user_text": "what time is sunset",
  "assistant_text": "Sunset in Austin today is 8:32 PM.",
  "audio_url": "/api/audio/abc123_..."
}
```

`audio_url` is `null` when no TTS provider is configured — the iOS app falls back to displaying the text.

## Provider selection

Each provider has a `make_*(settings)` factory. Selection order (first non-empty wins) is documented at the top of `app/stt/__init__.py` and `app/tts/__init__.py`. Adding a provider:

1. Create `app/{stt,tts}/<name>.py` implementing the Protocol (`name`, `describe()`, `transcribe()` or `synthesize()`).
2. Add a branch in the factory.
3. Add a key/config slot to `Settings` and `.env.example`.
4. Add a unit test in `tests/test_providers.py`.

## Hermes session continuity

`HermesClient` parses `session_id: <id>` from stderr on every turn, and passes it back via `--resume <id>` on subsequent turns. The iOS app threads `session_id` through `TurnResponse → next request`, so a conversation keeps Hermes' tool state across turns.

If Hermes ever returns an empty session_id, the client surfaces an empty string — the iOS app then starts a fresh session on the next turn.
