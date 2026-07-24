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
stores the token in Keychain, creates HTTPS LaunchAgents after confirmation, and
renders a short-lived pairing QR only after `/health` reports HTTPS readiness.
The checked-in `launchd/` files are non-operational deprecation stubs.

Setup is macOS + Tailscale only. A local harness may pause for certificate and
LaunchAgent approval; rerun `setup` after resolving a prerequisite, or `pair`
to issue another five-minute code. Manual URL/token and Bonjour remain
available when camera pairing cannot be used.

Inspect the service without exposing credentials:

```bash
launchctl print "gui/$(id -u)/io.harnessvoice.backend"
tail -F "$HOME/Library/Logs/Harness Voice/backend.log"
curl -s "https://$(tailscale status --json | jq -r '.Self.DNSName | rtrimstr(\".\")'):8765/health" | jq
```

Do not retrieve or print the Keychain token for diagnostics.


## Advanced local development

```bash
uv sync --extra dev               # base + test deps
uv sync --extra dev --extra local # add faster-whisper + piper
uv run python -m app              # start loopback server (auto-loads backend/.env)
uv run pytest -q                  # run tests (env-isolated per test)
```

`backend/.env` is for advanced local development only; the guided provisioner
never reads or writes it. Shell env vars still win over `.env` when both are set.

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
