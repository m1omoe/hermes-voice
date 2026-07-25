# Tailscale Serve Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make guided macOS setup use persistent Tailscale Serve so App Store Tailscale installations can provision and QR-pair without writing certificate files.

**Architecture:** Serve mode binds FastAPI to loopback, trusts forwarded HTTPS only from loopback, and validates an exact non-conflicting Tailscale Serve mapping. The provisioner stages generated assets and performs a rollback-capable cutover from neutral or recognized legacy LaunchAgents; direct TLS remains a manual runtime mode.

**Tech Stack:** Python 3.11+/uv, FastAPI/Pydantic, Uvicorn, pytest, macOS launchd/Keychain, Tailscale Serve CLI.

## Global Constraints

- Guided setup mode is `tailscale-serve`; direct certificate/key TLS remains supported only as an advanced manual runtime configuration.
- Serve mode requires `HERMES_VOICE_HOST=127.0.0.1`, no certificate/key, `HERMES_VOICE_TRUSTED_HTTPS_PROXY=tailscale-serve`, `proxy_headers=True`, and `forwarded_allow_ips=127.0.0.1`.
- Same-user local processes are trusted. Network peers must not reach or spoof the unauthenticated pairing route directly.
- Pairing requires a non-empty token and either valid direct TLS or validated Serve mode. Ordinary HTTP cannot claim.
- Never call `tailscale serve reset`; preserve unrelated Serve configuration.
- Reuse only the exact HTTPS 8765 → `http://127.0.0.1:8765` mapping; abort on any conflicting 8765 mapping before mutation.
- Long-lived tokens remain in Keychain and never enter argv, generated files, QR, logs, prompts, exceptions, or reports.
- Pairing secrets exist only in memory and inside the QR payload; never print them separately.
- Generated files contain no maintainer-specific user path, hostname, or tailnet.
- Use the installed `iPhone 17` simulator for iOS regression verification; no iOS behavior change is planned.

---

## File map

| Path | Responsibility |
|---|---|
| `backend/app/config.py` | Model direct-TLS vs trusted Serve runtime invariants and external HTTPS scheme. |
| `backend/app/__main__.py` | Configure Uvicorn proxy trust exactly for Serve mode. |
| `backend/app/main.py` | Startup validation, pairing gate, and Bonjour external scheme. |
| `backend/tests/test_security.py` | Runtime-mode invariant and proxy trust tests. |
| `backend/tests/test_pairing.py` | Serve-mode claim and ordinary-HTTP rejection tests. |
| `backend/scripts/provisioning.py` | Render loopback-only Serve backend assets; retain direct-TLS renderers only if still used manually. |
| `backend/scripts/provision_macos.py` | Parse/own Serve status, stage assets, transactional migration, rollback, readiness, and pair gating. |
| `backend/tests/test_provisioning.py` | Renderer, Serve ownership, cutover, rollback, legacy migration, and pair tests. |
| `README.md` / `backend/README.md` | Public Serve-default setup and non-secret troubleshooting. |
| `.docs/ai/*` | Verification evidence and physical acceptance status. |

### Task 1: Enforce trusted Serve runtime invariants

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/__main__.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_security.py`
- Test: `backend/tests/test_pairing.py`

**Interfaces:**
- Produces: `Settings.trusted_https_proxy: str`, `Settings.serve_mode: bool`, `Settings.external_scheme: str`, and startup validation for mutually exclusive direct-TLS/Serve modes.
- Consumes later: renderers set `HERMES_VOICE_TRUSTED_HTTPS_PROXY=tailscale-serve`; provisioner readiness observes public HTTPS.

- [ ] **Step 1: Add failing configuration and startup tests**

Add cases proving:

```python
serve = Settings(
    host="127.0.0.1",
    auth_token="token",
    trusted_https_proxy="tailscale-serve",
)
assert serve.serve_mode is True
assert serve.pairing_enabled is True
assert serve.external_scheme == "https"
```

Reject Serve mode with `0.0.0.0`, any cert/key, an unknown proxy value, or a forwarded allowlist other than `127.0.0.1`. Keep direct TLS pairing enabled and direct plain HTTP disabled.

Patch `uvicorn.run` in an entrypoint test and assert Serve mode passes:

```python
proxy_headers=True
forwarded_allow_ips="127.0.0.1"
```

Assert direct TLS passes `proxy_headers=False` and does not accept inherited broad proxy trust.

- [ ] **Step 2: Add failing HTTP contract tests**

Create a configured Serve-mode client. Assert an HTTPS request forwarded from the trusted TestClient scope claims once, while an ordinary HTTP request returns the existing uniform 404. Assert Bonjour startup receives `scheme="https"` in Serve mode.

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_security.py tests/test_pairing.py tests/test_mdns.py -v
```

Expected: failures for missing Serve fields/invariants, missing Uvicorn proxy kwargs, and wrong external scheme.

- [ ] **Step 4: Implement the runtime mode boundary**

Add the exact enum-like environment value to `Settings` and `get_settings`. Keep the decision in properties rather than scattering string checks:

```python
trusted_https_proxy: str = ""
forwarded_allow_ips: str = ""

@property
def direct_tls(self) -> bool:
    return bool(self.ssl_certfile and self.ssl_keyfile)

@property
def serve_mode(self) -> bool:
    return self.trusted_https_proxy == "tailscale-serve"

@property
def pairing_enabled(self) -> bool:
    return bool(self.auth_token and (self.direct_tls or self.serve_mode))

@property
def external_scheme(self) -> str:
    return "https" if self.direct_tls or self.serve_mode else "http"
```

Add one startup validator called by `__main__.main` and app creation paths used in tests. In Serve mode it requires exact loopback host, absent cert/key, and exact forwarded allowlist. Unknown non-empty proxy values fail closed. Direct TLS rejects proxy trust.

Configure `uvicorn.run` from validated settings. Derive Bonjour scheme from `settings.external_scheme`.

- [ ] **Step 5: Run focused verification**

Run the Step 3 command. Expected: all pass.

Run:

```bash
uv run --directory backend --extra dev ruff check app/config.py app/__main__.py app/main.py tests/test_security.py tests/test_pairing.py tests/test_mdns.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/__main__.py backend/app/main.py backend/tests/conftest.py backend/tests/test_security.py backend/tests/test_pairing.py backend/tests/test_mdns.py
git commit -m "feat: validate Tailscale Serve proxy mode"
```

### Task 2: Render loopback-only Serve assets

**Files:**
- Modify: `backend/scripts/provisioning.py`
- Modify: `backend/scripts/provision_macos.py`
- Test: `backend/tests/test_provisioning.py`

**Interfaces:**
- Produces: Serve-mode `render_backend_wrapper` and `render_backend_plist` output with no certificate dependency.
- Consumes: Task 1 environment names and exact runtime invariants.

- [ ] **Step 1: Replace direct-certificate renderer expectations with failing Serve expectations**

Assert the wrapper/plist contain:

```text
HERMES_VOICE_HOST=127.0.0.1
HERMES_VOICE_TRUSTED_HTTPS_PROXY=tailscale-serve
FORWARDED_ALLOW_IPS=127.0.0.1
HERMES_BIN=<absolute detected path>
```

Assert neither output contains `HERMES_VOICE_CERT`, `HERMES_VOICE_KEY`, `server.crt`, `server.key`, or a token literal. Keep Keychain child-only assertions and shell syntax validation.

Assert guided Serve rendering no longer requires or produces renewal script/plist output. Do not delete direct-TLS runtime support in `app`; renderer helpers may be deleted if no non-guided caller remains.

- [ ] **Step 2: Run renderer tests and confirm RED**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -k 'backend_wrapper or backend_plist or renew' -v
```

Expected: old `0.0.0.0` and cert-path output fails new assertions.

- [ ] **Step 3: Implement Serve asset rendering**

Update existing renderers rather than introducing a parallel public setup path. Preserve `plistlib` and `shlex.quote` patterns. The wrapper still retrieves Keychain token into a shell-local variable and exports it only to the final child. The plist carries only non-secret stable environment.

Remove renewal rendering imports/calls from guided provisioning in this task, including `_write_assets` in `provision_macos.py`; do not alter advanced direct-TLS backend configuration. Add a setup-level assertion that no renewal asset is written or bootstrapped.

- [ ] **Step 4: Verify and commit**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -k 'backend_wrapper or backend_plist or renew' -v
uv run --directory backend --extra dev ruff check scripts/provisioning.py scripts/provision_macos.py tests/test_provisioning.py
```

Expected: all selected tests pass; Ruff clean.

Commit:

```bash
git add backend/scripts/provisioning.py backend/scripts/provision_macos.py backend/tests/test_provisioning.py
git commit -m "feat: render Tailscale Serve backend assets"
```

### Task 3: Add exact Serve ownership and conflict detection

**Files:**
- Modify: `backend/scripts/provision_macos.py`
- Test: `backend/tests/test_provisioning.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ServeMappingState:
    exact: bool
    absent: bool
    conflict: str | None


def parse_serve_status(payload: str, dns_name: str, port: int = 8765) -> ServeMappingState: ...
```

- Produces `MacProvisioner._require_serve_mapping(...) -> bool`, returning whether this run added the mapping for rollback ownership.
- Consumes: `CommandRunner.run` and Task 2 loopback target.

- [ ] **Step 1: Add pure parser tests**

Use frozen JSON fixtures for:

```json
{
  "TCP": {"8765": {"HTTPS": true}},
  "Web": {
    "mandalore.tailnet.ts.net:8765": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
    }
  }
}
```

Cover exact, fully absent, wrong proxy, non-HTTPS TCP entry, alternate root handler, malformed JSON, and a different host/port. Malformed/conflicting input must not be interpreted as absent.

- [ ] **Step 2: Add command ownership tests**

With `FakeRunner`, assert:

- exact mapping: only `tailscale serve status --json`, no mutation;
- absent mapping: one exact `serve --bg --https=8765 http://127.0.0.1:8765`, then status recheck;
- conflict: `ProvisioningError` before file/Keychain/launchctl mutation;
- no command includes `serve reset`;
- rollback of a mapping added by this run uses exactly `tailscale serve --https=8765 off` and never touches other ports. This argv is pinned to the official CLI contract; Task 6 also exercises it by adding/removing a temporary unused-port mapping before live migration.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -k 'serve_status or serve_mapping or serve_conflict' -v
```

Expected: parser/import or behavior failures.

- [ ] **Step 4: Implement parser and bounded mutation**

Parse only the exact documented `TCP` and `Web` fields. Treat unknown/malformed state as conflict, not absence. Run the exact status and mapping commands through the injected runner. A successful add is not accepted until a second status parse returns exact.

- [ ] **Step 5: Verify and commit**

Run the Step 3 command and Ruff on `scripts/provision_macos.py` plus its test. Expected: pass/clean.

Commit:

```bash
git add backend/scripts/provision_macos.py backend/tests/test_provisioning.py
git commit -m "feat: manage an exact Tailscale Serve mapping"
```

### Task 4: Make guided migration transactional

**Files:**
- Modify: `backend/scripts/provision_macos.py`
- Test: `backend/tests/test_provisioning.py`

**Interfaces:**
- Produces an internal deployment snapshot/rollback boundary covering generated assets, Keychain state, relevant loaded agents, and Serve mapping ownership.
- Defines `LEGACY_BACKEND_LABEL = "dev.finklea.harnessvoice.backend"` in `provision_macos.py`; only this exact legacy label plus the neutral backend/renewal labels are migration-owned.
- Extends `Keychain` with `delete(service: str) -> None` so rollback can restore a previously absent item; the native and fake implementations must implement it.
- Consumes: Task 3 exact Serve state; Task 2 staged wrapper/plist.

- [ ] **Step 1: Extend injected filesystem and Keychain seams for exact restoration**

Add only operations needed by real rollback and tests: read optional text/bytes, existence, replace/rename, unlink, and Keychain delete. Implement them in `LocalFileWriter`, the native Keychain implementation, and their fakes. Test both prior-token-present and prior-token-absent rollback; the absent case must delete the newly created item rather than storing an empty token. Never read secret dotenv files.

- [ ] **Step 2: Add cutover and legacy migration tests**

Assert setup:

1. snapshots prior neutral/legacy agent loaded states and existing generated assets;
2. stages wrapper/plist before stopping the old backend;
3. validates the staged wrapper with `bash -n` and parses the staged plist before any Keychain, Serve, or launchctl mutation;
4. reuses/adds the Serve mapping per Task 3;
5. boots out the loaded neutral or exact recognized legacy backend only at cutover;
6. atomically installs and bootstraps only `io.harnessvoice.backend`;
7. verifies public HTTPS/non-mock/authenticated real Hermes;
8. crosses an explicit commit boundary;
9. after commit, boots out/removes obsolete neutral renewal and recognized legacy generated assets.

Add a live-port-collision regression proving the legacy agent is stopped before neutral bootstrap.

- [ ] **Step 3: Add table-driven rollback tests**

Inject pre-commit failure at each mutating boundary: Keychain set, Serve add/recheck, staged wrapper/plist validation, asset replace, old bootout, neutral bootstrap/kickstart, public health, and authenticated harness listing. For every pre-commit post-mutation failure assert prior files/token presence-or-absence/loaded agents are restored and a newly added Serve mapping is removed. Existing exact mapping remains untouched. Assert rollback failure reports both the original and rollback boundary without exposing token values.

Inject cleanup failures after the explicit commit boundary (renewal bootout/unlink and legacy-asset unlink). Assert the verified neutral backend, token, installed files, and owned Serve mapping remain committed; report incomplete cleanup and do not roll back the working deployment.

- [ ] **Step 4: Run migration tests and confirm RED**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -k 'migration or rollback or legacy or cutover' -v
```

Expected: missing snapshot/atomic writer behavior and wrong existing unload/start order.

- [ ] **Step 5: Implement staged cutover and rollback**

Keep transaction logic in one bounded internal helper rather than adding conditionals throughout `setup`. Snapshot secrets in memory only. Stage under the destination directory; validate `bash -n` and plist parsing before token, Serve, or agent mutation; then use atomic replace. After successful public/authenticated verification, mark the deployment committed and perform obsolete renewal/legacy cleanup. Pre-commit failures restore in reverse mutation order and restart only agents previously loaded. Post-commit cleanup failures preserve the verified deployment and report incomplete cleanup.

Update `setup` to remove `tailscale cert`; confirmation copy says it will configure a persistent tailnet-only HTTPS proxy and install one backend LaunchAgent.

- [ ] **Step 6: Verify and commit**

Run the Step 4 command and the complete provisioning suite:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -v
uv run --directory backend --extra dev ruff check scripts/provision_macos.py tests/test_provisioning.py
```

Expected: all pass/clean.

Commit:

```bash
git add backend/scripts/provision_macos.py backend/tests/test_provisioning.py
git commit -m "feat: migrate guided servers to Tailscale Serve"
```

### Task 5: Enforce pair readiness and update public guidance

**Files:**
- Modify: `backend/scripts/provision_macos.py`
- Modify: `backend/tests/test_provisioning.py`
- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `backend/launchd/io.harnessvoice.backend.plist`
- Modify: `backend/launchd/io.harnessvoice.cert-renew.plist`
- Modify: `backend/launchd/renew-cert.sh`

**Interfaces:**
- `MacProvisioner.create_pairing_qr()` requires Keychain token plus exact Serve/direct-TLS readiness before ticket creation.
- Checked-in launchd files remain non-operational deprecation guidance and must not imply certificate generation is the guided path.

- [ ] **Step 1: Add failing pair-gate tests**

Cover:

- missing Keychain token → refusal, zero ticket creations;
- Serve mode with wrong/conflicting/missing mapping → refusal;
- valid direct-TLS mode with no Serve mapping → proceeds; incomplete direct TLS (one of cert/key missing) → refusal;
- public health not HTTPS or mock true → refusal;
- authenticated harness listing missing available Hermes → refusal;
- all gates pass → exactly one ticket and QR; pairing secret is not separately written to captured output.

- [ ] **Step 2: Run pair tests and confirm RED**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -k 'pair or qr or readiness' -v
```

Expected: current pair path skips Keychain token and authenticated readiness.

- [ ] **Step 3: Implement the pair gate**

Read the token through the existing Keychain abstraction, inspect exact Serve state before public requests, and pass the token to the existing health/harness readiness path. Create the ticket only after all checks return. Preserve direct-TLS manual compatibility by recognizing a valid direct-TLS runtime without requiring Serve status.

- [ ] **Step 4: Update docs and deprecation stubs**

Document:

```bash
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup --from-bws
uv run --project backend --extra provision python backend/scripts/provision_macos.py pair
```

Explain loopback backend + Tailscale Serve, exact non-destructive status diagnostics, conflict behavior, rollback, reruns, and manual fallback. Remove operational guidance that tells guided users to request or renew cert files. No diagnostic prints a token.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --directory backend --extra dev pytest tests/test_provisioning.py -v
uv run --directory backend --extra dev ruff check scripts/provisioning.py scripts/provision_macos.py tests/test_provisioning.py
rg -n 'tailscale cert|server\.crt|server\.key|serve reset|/Users/tfinklea|scadrial\.tailceb58' README.md backend/README.md backend/launchd backend/scripts
```

Expected: tests/Ruff pass; remaining grep matches are only advanced direct-TLS code/tests or explicit prohibitions, never guided setup/generated Serve assets or maintainer values.

Commit:

```bash
git add backend/scripts/provision_macos.py backend/tests/test_provisioning.py README.md backend/README.md backend/launchd
git commit -m "docs: make Tailscale Serve the guided setup path"
```

### Task 6: Full regression and Mandalore physical acceptance

**Files:**
- Modify: `.docs/ai/current-state.md`
- Modify: `.docs/ai/roadmap.md`
- Modify: `.docs/ai/phases/self-serve-server-setup-report.md`

- [ ] **Step 1: Run automated regression**

```bash
uv run --directory backend --extra dev pytest
uv run --directory backend --extra dev ruff check app/config.py app/__main__.py app/main.py scripts/provisioning.py scripts/provision_macos.py tests/test_security.py tests/test_pairing.py tests/test_mdns.py tests/test_provisioning.py
xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice
xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' test
```

Expected: backend and iOS suites pass; changed files Ruff-clean.

- [ ] **Step 2: Run live guided setup on Mandalore**

Before touching port 8765, prove the installed CLI accepts the documented scoped removal form on an unused port:

```bash
tailscale serve --bg --https=18765 http://127.0.0.1:9
tailscale serve status --json
tailscale serve --https=18765 off
```

Expected: the temporary 18765 mapping appears, then disappears; existing 8765 and all unrelated mappings remain byte-for-byte equivalent in parsed status.

From repository root:

```bash
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup --from-bws
```

Approve the Serve mapping/LaunchAgent cutover. Verify non-secret state:

```bash
tailscale serve status --json
launchctl print "gui/$(id -u)/io.harnessvoice.backend"
curl -s https://mandalore.tailceb58.ts.net:8765/health | jq '{status,mock,scheme}'
```

Expected: exact 8765 mapping to loopback, neutral backend loaded, `{status:"ok", mock:false, scheme:"https"}`. The recognized legacy backend and renewal agents are unloaded after success.

- [ ] **Step 3: Run physical pairing acceptance**

Run `pair`, scan from iPhone, save/select the profile, and complete a real Hermes turn. Confirm replay and expiry fail, login restart restores backend reachability, and manual URL/token plus Bonjour paths still work. Do not capture the terminal QR in logs/reports.

- [ ] **Step 4: Record evidence and commit**

Update the three handoff documents with exact automated counts, Serve status, migration/rollback result, and any user-only acceptance item. Mark the roadmap item complete only after the real turn and restart checks pass.

```bash
git add .docs/ai/current-state.md .docs/ai/roadmap.md .docs/ai/phases/self-serve-server-setup-report.md
git commit -m "docs: verify Tailscale Serve self-setup"
```

## Plan self-review

- **Spec coverage:** Tasks 1–2 enforce proxy/bind/pairing/Bonjour boundaries; Task 3 owns exact Serve mapping/conflicts; Task 4 provides staged migration, legacy cutover, renewal cleanup, and rollback; Task 5 gates QR creation and updates public guidance; Task 6 proves automated and physical behavior.
- **Placeholder scan:** no TBD/TODO/placeholder steps; every task has files, interfaces, RED/GREEN commands, and commit boundaries.
- **Type consistency:** `trusted_https_proxy`, `serve_mode`, `external_scheme`, `ServeMappingState`, and the exact Serve mapping are named consistently across producers and consumers.
