# Self-Serve macOS Server Setup and QR Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an iPhone provision and pair a new macOS + Tailscale Harness Voice backend through a safe local-harness prompt and one-time QR code.

**Architecture:** A portable Python provisioner renders user-specific launchd assets, keeps the long-lived backend token in macOS Keychain, and creates a locally persisted one-time pairing ticket only after the HTTPS service is healthy. The FastAPI backend atomically claims a ticket over HTTPS and returns the normal token. iOS parses/scans the QR, claims it, verifies `/api/harnesses` with that token, and only then saves a server profile.

**Tech Stack:** Python 3.11+/uv, FastAPI/Pydantic, SQLite, macOS Keychain + launchd + Tailscale, Swift 5.10, SwiftUI, AVFoundation/AVCaptureSession, XCTest.

## Global Constraints

- First release supports **macOS + Tailscale only**; no Linux, Docker, Windows, LAN-only, or HTTP setup path.
- QR payloads carry a version, HTTPS base URL, pairing ID, and high-entropy one-time secret — never the long-lived backend token, BWS credential, API key, or filesystem path.
- Pairing requires configured TLS plus a non-empty backend token; claim credentials expire after 5 minutes, redeem once atomically, and are pruned after expiry.
- Public setup creates a unique server token in macOS Keychain; BWS is an explicit optional local bootstrap and must never print or pass a secret in CLI arguments.
- Generated files must derive the current user, checkout, `uv`, MagicDNS host, and certificate paths at runtime. No `/Users/tfinklea`, `scadrial`, or personal tailnet host in generated output.
- Prompt, QR, diagnostics, and error/log text must not disclose the long-lived backend token or pairing secret.
- Retain manual URL/token and Bonjour onboarding as fallbacks. Save a paired profile only after authenticated `GET /api/harnesses` succeeds.
- Use `xcodebuild` with the installed `iPhone 17` simulator, not the stale iPhone 16 destination in older docs.

---

## File map

| Path | Responsibility |
| --- | --- |
| `backend/app/pairing.py` | SQLite pairing-ticket store; ticket creation, atomic claim, expiry pruning. |
| `backend/app/models.py` | Pydantic pairing claim/response shapes. |
| `backend/app/config.py` | Pairing-path and TLS-enabled settings exposed centrally. |
| `backend/app/main.py` | Store initialization and the narrow unauthenticated pairing-claim route. |
| `backend/tests/test_pairing.py` | Store and HTTP security/atomicity tests. |
| `backend/scripts/provisioning.py` | Pure macOS path discovery and launchd/wrapper rendering. |
| `backend/scripts/provision_macos.py` | Interactive `setup`, `pair`, and BWS-bootstrap CLI. |
| `backend/tests/test_provisioning.py` | Portable rendering and non-secret provisioning tests. |
| `backend/pyproject.toml` | Provisioning-only QR dependency extra. |
| `README.md` | Public setup guidance and canonical harness prompt behavior. |
| `ios/HermesVoice/HermesVoice/Services/ServerPairing.swift` | QR payload validation, claim/validation client, and testable pairing coordinator. |
| `ios/HermesVoice/HermesVoice/Views/PairServerViews.swift` | Setup sheet, code scanner, pairing progress/errors, and success callback. |
| `ios/HermesVoice/HermesVoice/Views/OnboardingView.swift` | First-run entry point and active-profile completion callback. |
| `ios/HermesVoice/HermesVoice/Views/BackendProfileViews.swift` | Existing-user “Set up a Mac server” entry point. |
| `ios/HermesVoice/HermesVoice/Services/HermesVoiceAPI.swift` | Pairing claim request/response call. |
| `ios/HermesVoice/HermesVoice/Info.plist` | Camera privacy message. |
| `ios/HermesVoice/HermesVoiceTests/ServerPairingTests.swift` | QR parser and claim-then-authenticate state tests. |

### Task 1: Pairing ticket store and configuration

**Files:**
- Create: `backend/app/pairing.py`
- Modify: `backend/app/config.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_pairing.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class PairingTicket:
    pairing_id: str
    secret: str
    expires_at: float

class PairingStore:
    def __init__(self, path: Path | None = None, *, now: Callable[[], float] = time.time): ...
    def create(self, *, ttl_seconds: int = 300) -> PairingTicket: ...
    def claim(self, pairing_id: str, secret: str) -> bool: ...
    def prune_expired(self) -> int: ...
```

`claim` returns `True` exactly once. It never returns a token; the HTTP layer reads the configured token only after a successful claim.

- [ ] **Step 1: Write store tests before implementation**

Create `backend/tests/test_pairing.py` with a temporary `PairingStore` fixture. Cover successful claim, second claim rejection, expiry, malformed/unknown claims, pruning, and concurrent claim attempts:

```python
def test_ticket_claim_is_single_use(tmp_path):
    store = PairingStore(tmp_path / "pairings.db", now=lambda: 100.0)
    ticket = store.create(ttl_seconds=300)

    assert store.claim(ticket.pairing_id, ticket.secret) is True
    assert store.claim(ticket.pairing_id, ticket.secret) is False


def test_expired_ticket_cannot_be_claimed(tmp_path):
    now = [100.0]
    store = PairingStore(tmp_path / "pairings.db", now=lambda: now[0])
    ticket = store.create(ttl_seconds=1)
    now[0] = 102.0

    assert store.claim(ticket.pairing_id, ticket.secret) is False
```

Use a thread barrier plus `ThreadPoolExecutor(max_workers=2)` for the concurrency assertion: exactly one result may be `True`.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `uv run --project backend pytest backend/tests/test_pairing.py -v`

Expected: import failure for `app.pairing`.

- [ ] **Step 3: Implement a dedicated SQLite store**

Create `backend/app/pairing.py`. Follow `schedules.py`’s per-call default-path and SQLite `busy_timeout` pattern, but use a dedicated `~/.hermes-voice/pairings.db` table. Store `pairing_id`, SHA-256 secret verifier, `created_at`, `expires_at`, and nullable `claimed_at`; never persist raw secret. Generate both fields with `secrets.token_urlsafe`, compare verifiers with `secrets.compare_digest`, and claim in one `BEGIN IMMEDIATE` transaction whose `UPDATE` requires `claimed_at IS NULL AND expires_at > now`.

Add to `Settings` only the non-secret pairing configuration needed by routes: a `pairing_enabled` property that is true only when `auth_token`, `ssl_certfile`, and `ssl_keyfile` are all non-empty. Keep database path testable through the module’s `DEFAULT_DB_PATH`, rather than adding another user-facing environment variable.

Extend the autouse test environment cleanup with `HERMES_VOICE_CERT` and `HERMES_VOICE_KEY`, then reset the settings cache as it already does.

- [ ] **Step 4: Run the focused tests and lint**

Run: `uv run --project backend pytest backend/tests/test_pairing.py -v`

Expected: all pairing-store tests pass.

Run: `uv run --project backend ruff check backend/app/pairing.py backend/tests/test_pairing.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit the store boundary**

```bash
git add backend/app/pairing.py backend/app/config.py backend/tests/conftest.py backend/tests/test_pairing.py
git commit -m "feat: add one-time pairing ticket store"
```

### Task 2: HTTPS pairing-claim API

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_pairing.py`

**Interfaces:**

```python
class PairingClaimRequest(BaseModel):
    pairing_id: str = Field(..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    secret: str = Field(..., min_length=32, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")

class PairingClaimResponse(BaseModel):
    token: str

POST /api/pairings/claim -> PairingClaimResponse
```

- [ ] **Step 1: Add failing route tests**

Add tests that set `HERMES_VOICE_TOKEN`, `HERMES_VOICE_CERT`, and `HERMES_VOICE_KEY`, reset settings, then create a ticket on `client.app.state.pairings`:

```python
def test_claim_returns_token_once_over_tls_config(monkeypatch):
    client = configured_pairing_client(monkeypatch)
    ticket = client.app.state.pairings.create()

    first = client.post("/api/pairings/claim", json={
        "pairing_id": ticket.pairing_id, "secret": ticket.secret,
    })
    replay = client.post("/api/pairings/claim", json={
        "pairing_id": ticket.pairing_id, "secret": ticket.secret,
    })

    assert first.status_code == 200
    assert first.json() == {"token": "test-token"}
    assert replay.status_code == 404
```

Also assert a server without TLS configuration returns `404`, a malformed payload is `422`, unknown/expired/replayed tickets all use the same `404` body, and the token is absent from error responses.

- [ ] **Step 2: Run the new route tests and confirm failure**

Run: `uv run --project backend pytest backend/tests/test_pairing.py -v`

Expected: missing `app.state.pairings` and route failures.

- [ ] **Step 3: Register the store and route**

In `create_app`, initialize/prune the pairing store before route registration and assign it to `app.state.pairings`, mirroring the eager schedule-store initialization pattern. Import the new models into `main.py`.

Register `POST /api/pairings/claim` without `_require_token`, but make it unavailable (`404`) unless `settings.pairing_enabled` is true. On every invalid, expired, or consumed ticket return the identical `HTTPException(status_code=404, detail="pairing code unavailable")`. On success return `PairingClaimResponse(token=settings.auth_token)`. Do not log request bodies, pairing IDs, secrets, or tokens.

- [ ] **Step 4: Verify API behavior and regression suite**

Run: `uv run --project backend pytest backend/tests/test_pairing.py backend/tests/test_security.py -v`

Expected: all pass.

Run: `uv run --project backend pytest`

Expected: full backend suite passes.

- [ ] **Step 5: Commit the API contract**

```bash
git add backend/app/models.py backend/app/main.py backend/tests/test_pairing.py
git commit -m "feat: claim server pairing codes over HTTPS"
```

### Task 3: Portable launchd asset rendering

**Files:**
- Create: `backend/scripts/__init__.py`
- Create: `backend/scripts/provisioning.py`
- Test: `backend/tests/test_provisioning.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class MacServerLayout:
    repo_root: Path
    home: Path
    uv: Path
    dns_name: str
    support_dir: Path
    backend_label: str = "io.harnessvoice.backend"
    renew_label: str = "io.harnessvoice.cert-renew"


def render_backend_wrapper(layout: MacServerLayout, keychain_service: str) -> str: ...
def render_backend_plist(layout: MacServerLayout, wrapper_path: Path) -> str: ...
def render_renew_plist(layout: MacServerLayout, renew_script_path: Path) -> str: ...
def render_renew_script(layout: MacServerLayout) -> str: ...
```

- [ ] **Step 1: Write portable-rendering tests**

Create tests with an arbitrary non-maintainer layout:

```python
def test_backend_assets_use_detected_paths_only(tmp_path):
    layout = MacServerLayout(
        repo_root=tmp_path / "checkout",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness Voice",
    )
    rendered = render_backend_plist(layout, layout.support_dir / "run-backend")

    assert str(layout.repo_root / "backend") in rendered
    assert "mandalore.tailnet.ts.net" in rendered
    assert "/Users/tfinklea" not in rendered
    assert "scadrial" not in rendered
```

Assert the wrapper uses a Keychain lookup by service name, exports the token only to the `exec uv run` child, and contains no token literal. Assert renewal invokes `tailscale cert` for the supplied DNS name, copies cert/key into the generated support directory with restrictive key permissions, and kickstarts the supplied backend label.

- [ ] **Step 2: Run the renderer tests and confirm failure**

Run: `uv run --project backend pytest backend/tests/test_provisioning.py -v`

Expected: import failure for `scripts.provisioning`.

- [ ] **Step 3: Implement pure discovery-independent renderers**

Create `backend/scripts/provisioning.py` with the exact `MacServerLayout` boundary above. Render XML using `plistlib.dumps` so dynamic paths are correctly escaped. Render the shell wrapper and renewal script as static templates populated only from the layout; quote shell paths with `shlex.quote`.

The wrapper must fail closed when its Keychain lookup is empty and must not echo the value. It starts `uv run --directory <repo>/backend python -m app` with `HERMES_VOICE_HOST=0.0.0.0`, TLS paths, `HERMES_VOICE_PUBLIC_HOST`, and the token in its child environment. The renewal script uses the same dynamic certificate destination and restarts only the generated backend label.

- [ ] **Step 4: Verify renderer tests and source hygiene**

Run: `uv run --project backend pytest backend/tests/test_provisioning.py -v`

Expected: all pass.

Run: `rg -n '/Users/tfinklea|scadrial\.tailceb58' backend/scripts/provisioning.py`

Expected: no output.

- [ ] **Step 5: Commit portable asset generation**

```bash
git add backend/scripts/__init__.py backend/scripts/provisioning.py backend/tests/test_provisioning.py
git commit -m "feat: render portable macOS backend services"
```

### Task 4: Guided macOS provisioner and pairing QR CLI

**Files:**
- Create: `backend/scripts/provision_macos.py`
- Modify: `backend/pyproject.toml`
- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `backend/launchd/io.harnessvoice.backend.plist`
- Modify: `backend/launchd/io.harnessvoice.cert-renew.plist`
- Modify: `backend/launchd/renew-cert.sh`
- Test: `backend/tests/test_provisioning.py`

**Interfaces:**

```text
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup
uv run --project backend --extra provision python backend/scripts/provision_macos.py setup --from-bws
uv run --project backend --extra provision python backend/scripts/provision_macos.py pair
```

- [ ] **Step 1: Add failing command-layer tests**

Add pure-unit tests around injected command runners/filesystem writers rather than invoking live launchd or Keychain. Cover: non-macOS rejection, missing Tailscale/`uv`/MagicDNS stop states, detected MagicDNS trailing-dot normalization, refusal to proceed until certificate creation succeeds, and `pair` refusing until the HTTPS health check returns `status: ok` with `scheme: https`.

```python
def test_pair_refuses_when_https_health_is_not_ready(tmp_path):
    runner = FakeRunner(health_body={"status": "ok", "scheme": "http"})
    provisioner = MacProvisioner(layout=layout(tmp_path), runner=runner)

    with pytest.raises(ProvisioningError, match="HTTPS"):
        provisioner.create_pairing_qr()
```

Add an assertion that the generated public token is handed directly to the Keychain abstraction and never appears in rendered file text, stdout capture, or a subprocess argument list. Add a separate BWS test asserting the nested process receives `HERMES_VOICE_TOKEN` from environment only.

- [ ] **Step 2: Run the command-layer tests and confirm failure**

Run: `uv run --project backend pytest backend/tests/test_provisioning.py -v`

Expected: missing `MacProvisioner`/CLI implementation failures.

- [ ] **Step 3: Implement guided setup without secret-bearing dotenv files**

Add a `MacProvisioner` orchestration object behind injectable runner, Keychain, clock, and file-writer boundaries. `setup` must:

1. detect macOS, repo root, `uv`, Tailscale executable, and normalized MagicDNS;
2. show each external action and require an explicit interactive confirmation before installing agents or requesting a certificate;
3. request/copy the Tailscale certificate, write generated wrapper/renewal script/plists under the current user’s Application Support and LaunchAgents locations, then bootstrap/kickstart them;
4. create a random server token for public setup and save it through the Keychain abstraction; or, only with `--from-bws`, invoke `bws-project run --` so the child receives the managed token in its environment and saves it through the same abstraction;
5. verify the public HTTPS `/health` URL before reporting success.

Use a native Keychain API or a process stdin/API boundary that never places the token in an argv element. Do not read/write `backend/.env`. Make `--from-bws` fail with a clear, non-secret explanation when `bws-project` or the project registration is unavailable.

`pair` rechecks HTTPS health, imports `PairingStore`, creates its 5-minute ticket, and renders a terminal QR through a new `provision` optional dependency (`qrcode>=8`). It prints only the scanner instruction and expiry timestamp; the QR is the only representation of the pairing secret.

Replace the personal launchd files with deprecation stubs that point to the provisioner, so a public reader cannot accidentally install the hard-coded paths. Rewrite the README setup and auto-start sections around the app prompt and the three commands above; preserve the advanced manual-development instructions separately.

- [ ] **Step 4: Verify command tests, dependency lock, and docs references**

Run: `uv lock --project backend`

Expected: lockfile updates successfully.

Run: `uv run --project backend pytest backend/tests/test_provisioning.py -v`

Expected: all pass.

Run: `uv run --project backend ruff check backend/scripts backend/tests/test_provisioning.py`

Expected: `All checks passed!`

Run: `rg -n '/Users/tfinklea|scadrial\.tailceb58' README.md backend/README.md backend/launchd`

Expected: no operational setup instruction contains a maintainer-specific value.

- [ ] **Step 5: Commit the public setup path**

```bash
git add backend/scripts backend/pyproject.toml backend/uv.lock backend/tests/test_provisioning.py README.md backend/README.md backend/launchd
git commit -m "feat: provision macOS Harness Voice servers"
```

### Task 5: iOS pairing payload and network client

**Files:**
- Create: `ios/HermesVoice/HermesVoice/Services/ServerPairing.swift`
- Modify: `ios/HermesVoice/HermesVoice/Services/HermesVoiceAPI.swift`
- Test: `ios/HermesVoice/HermesVoiceTests/ServerPairingTests.swift`

**Interfaces:**

```swift
struct ServerPairingPayload: Equatable {
    let baseURL: String
    let pairingID: String
    let secret: String
    init(qrString: String) throws
}

protocol ServerPairingClient {
    func claim(_ payload: ServerPairingPayload) async throws -> String
    func listHarnesses(baseURL: String, token: String) async throws -> [HermesVoiceAPI.HarnessOption]
}

struct PairedServer: Equatable {
    let url: String
    let token: String
    let harnesses: [HermesVoiceAPI.HarnessOption]
}
```

- [ ] **Step 1: Write parser and coordinator tests**

Create `ServerPairingTests.swift` with a fake `ServerPairingClient`. Test only the versioned `harness-voice://pair` URI shape, HTTPS URLs, non-empty identifier/secret, duplicate/unknown query item rejection, and malformed QR rejection. Test that the coordinator calls `listHarnesses` only after a successful claim and produces no `PairedServer` on claim or authenticated-check failure:

```swift
func testCoordinatorClaimsThenAuthenticates() async throws {
    let fake = FakePairingClient(token: "server-token", harnesses: [availableHermes])
    let server = try await ServerPairingCoordinator(client: fake).pair(validPayload)

    XCTAssertEqual(server.token, "server-token")
    XCTAssertEqual(fake.calls, [.claim, .listHarnesses])
}
```

- [ ] **Step 2: Run the new tests and confirm failure**

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/ServerPairingTests test`

Expected: compilation fails because pairing types do not exist.

- [ ] **Step 3: Implement strict payload parsing and claim client**

Use a fixed QR format: `harness-voice://pair?v=1&url=<percent-encoded HTTPS base URL>&id=<pairing ID>&secret=<secret>`. Reject unknown/duplicate fields, version other than `1`, non-HTTPS base URL, hostless URL, fragments, userinfo, and values outside backend field bounds.

Add `HermesVoiceAPI.claimPairing(payload:)` that POSTs `{"pairing_id":...,"secret":...}` to `/api/pairings/claim` without an auth header and decodes `{"token":...}`. Implement `LiveServerPairingClient` by using an empty-token API for claim and a token-bound API for `listHarnesses`. `ServerPairingCoordinator.pair` must return only after a non-empty available-harness list comes back from the authenticated request; it must not persist a profile.

- [ ] **Step 4: Verify focused iOS tests**

Run: `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`

Expected: project generation succeeds.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/ServerPairingTests test`

Expected: `TEST SUCCEEDED`.

- [ ] **Step 5: Commit pairing protocol client**

```bash
git add ios/HermesVoice/HermesVoice/Services/ServerPairing.swift ios/HermesVoice/HermesVoice/Services/HermesVoiceAPI.swift ios/HermesVoice/HermesVoiceTests/ServerPairingTests.swift ios/HermesVoice/HermesVoice.xcodeproj/project.pbxproj
git commit -m "feat: claim and validate paired servers"
```

### Task 6: QR scanner and reusable pairing setup sheet

**Files:**
- Create: `ios/HermesVoice/HermesVoice/Views/PairServerViews.swift`
- Modify: `ios/HermesVoice/HermesVoice/Info.plist`
- Modify: `ios/HermesVoice/project.yml`
- Test: `ios/HermesVoice/HermesVoiceTests/ServerPairingTests.swift`

**Interfaces:**

```swift
struct PairServerSheet: View {
    let onPaired: (PairedServer) -> Void
}

struct QRCodeScanner: UIViewControllerRepresentable {
    let onCode: (String) -> Void
    let onFailure: (String) -> Void
}
```

- [ ] **Step 1: Add scanner-independent state tests**

Extend `ServerPairingTests.swift` to drive the sheet’s observable pairing model with a fake client. Assert invalid QR text never calls the network client; a claim failure exposes a retryable failure state; and an available-harness result invokes `onPaired` exactly once even if the scanner emits the same code twice.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/ServerPairingTests test`

Expected: compilation fails because the pairing sheet model does not exist.

- [ ] **Step 3: Build the scanner and setup sheet**

Use `AVCaptureSession` + `AVCaptureMetadataOutput` restricted to `.qr` inside `QRCodeScanner`; do not add a third-party scanner dependency. Gate capture start on camera authorization, surface the denied/restricted state with Settings guidance, and stop the session after the first accepted barcode.

`PairServerSheet` has two stages:

1. **Set up on your Mac** — concise macOS/Tailscale/local-harness explanation and a `Copy setup prompt` button using `UIPasteboard.general.string`. The prompt must name the public GitHub repository and the Task 4 provisioner command but contain no credentials or device-specific secret.
2. **Scan pairing code** — present `QRCodeScanner`, parse locally, invoke the testable coordinator, and show distinct invalid-code, expired/unavailable, TLS/transport, and no-harness states. Every failure offers scan retry and a visible manual-setup fallback.

Add `NSCameraUsageDescription` to both `project.yml` source properties and the committed `Info.plist`, with copy that states the camera is used only to scan a local server pairing QR code.

- [ ] **Step 4: Verify build and focused tests**

Run: `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`

Expected: project generation succeeds.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/ServerPairingTests test`

Expected: `TEST SUCCEEDED`.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' build CODE_SIGNING_ALLOWED=NO`

Expected: `BUILD SUCCEEDED`.

- [ ] **Step 5: Commit the reusable pairing UI**

```bash
git add ios/HermesVoice/HermesVoice/Views/PairServerViews.swift ios/HermesVoice/HermesVoice/Info.plist ios/HermesVoice/project.yml ios/HermesVoice/HermesVoiceTests/ServerPairingTests.swift ios/HermesVoice/HermesVoice.xcodeproj/project.pbxproj
git commit -m "feat: scan one-time server pairing codes"
```

### Task 7: Wire pairing into onboarding and saved-server management

**Files:**
- Modify: `ios/HermesVoice/HermesVoice/Views/OnboardingView.swift`
- Modify: `ios/HermesVoice/HermesVoice/Views/BackendProfileViews.swift`
- Modify: `ios/HermesVoice/HermesVoiceTests/BackendProfileTests.swift`

**Interfaces:**

```swift
func profile(from paired: PairedServer) -> BackendProfile
```

The helper chooses the first `available == true` harness; pairing treats an empty available list as an error before this point.

- [ ] **Step 1: Add profile-application tests**

Add a pure test proving a paired result maps URL/token and the first available harness into a new profile, and a first-run callback saves that profile, marks reachability, and completes onboarding only after a pairing result is received. Keep existing migration/manual-profile tests untouched.

- [ ] **Step 2: Run tests and confirm failure**

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/BackendProfileTests test`

Expected: compilation fails because the pairing-to-profile helper is absent.

- [ ] **Step 3: Integrate both entry points**

In `OnboardingView`, add **Set up a Mac server** without removing local-network discovery or manual URL/token fields. On pairing success, replace the active profile with a suggested host name, claimed token, and selected available harness; call `markReachable()` and set `hasCompletedOnboarding = true` only after that result.

In `BackendProfileManagerView`, make the existing plus affordance explicitly offer **Enter manually** and **Set up a Mac server**. The latter presents `PairServerSheet`; success creates a non-active saved profile, leaving current routing, APNs registration, Watch state, and conversation state untouched. The user continues to select the new profile through the existing header picker.

- [ ] **Step 4: Verify profile regressions and full iOS suite**

Run: `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`

Expected: project generation succeeds.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:HermesVoiceTests/BackendProfileTests -only-testing:HermesVoiceTests/ServerPairingTests test`

Expected: `TEST SUCCEEDED`.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' test`

Expected: `TEST SUCCEEDED`.

- [ ] **Step 5: Commit app-flow integration**

```bash
git add ios/HermesVoice/HermesVoice/Views/OnboardingView.swift ios/HermesVoice/HermesVoice/Views/BackendProfileViews.swift ios/HermesVoice/HermesVoiceTests/BackendProfileTests.swift ios/HermesVoice/HermesVoice.xcodeproj/project.pbxproj
git commit -m "feat: guide server setup from the app"
```

### Task 8: End-to-end regression, documentation, and physical acceptance

**Files:**
- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `.docs/ai/roadmap.md`
- Modify: `.docs/ai/current-state.md`
- Create: `.docs/ai/phases/self-serve-server-setup-report.md`

- [ ] **Step 1: Add explicit public troubleshooting guidance**

Document the exact boundaries from the setup sheet: macOS + Tailscale only, a local coding harness operates the target Mac, expected approval pauses, how to re-run `setup`, how to re-render a QR with `pair`, and how to use manual setup if camera pairing is unavailable. Document the non-secret diagnostic commands (`launchctl print`, log tail, authenticated health test) and no command that prints a token.

- [ ] **Step 2: Run all automated verification**

Run: `uv run --project backend pytest`

Expected: full backend suite passes.

Run: `uv run --project backend ruff check backend`

Expected: `All checks passed!`

Run: `xcodegen generate --spec ios/HermesVoice/project.yml --project ios/HermesVoice`

Expected: project generation succeeds.

Run: `xcodebuild -project ios/HermesVoice/HermesVoice.xcodeproj -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17' test`

Expected: `TEST SUCCEEDED`.

- [ ] **Step 3: Perform the fresh-Mac acceptance check**

On Mandalore, use the app-copied prompt in the local harness. Confirm the provisioner stops for Tailscale/Keychain/BWS approval when appropriate, then starts an `io.harnessvoice.backend` launch agent listening at the detected HTTPS MagicDNS URL. Scan the terminal QR from the iPhone, validate the saved profile and a real Hermes turn, then verify QR replay and expiry fail. Restart/login Mandalore and confirm the agent restarts. Finally confirm manual URL/token and Bonjour setup still work against an existing server.

- [ ] **Step 4: Record completion and commit**

Update roadmap/current state/report with verification evidence, known limitations, and any user-only follow-up. Do not claim BWS integration complete until the live launchd wrapper has been exercised without a secret dotenv.

```bash
git add README.md backend/README.md .docs/ai/roadmap.md .docs/ai/current-state.md .docs/ai/phases/self-serve-server-setup-report.md ios/HermesVoice/HermesVoice.xcodeproj/project.pbxproj
git commit -m "docs: verify self-serve server setup"
```

## Plan self-review

- **Spec coverage:** Tasks 1–2 implement the short-lived, single-use HTTPS claim; Tasks 3–4 replace the personal deployment path with a portable macOS/Tailscale Keychain/BWS-aware provisioner; Tasks 5–7 implement QR parsing, scanning, authenticated profile validation, and both app entry points; Task 8 covers docs and fresh-Mac/replay/manual-path acceptance.
- **Placeholder scan:** no unresolved placeholders; every task has paths, interfaces, commands, expected results, and commit boundary.
- **Type consistency:** `PairingTicket` is intentionally server-local; `ServerPairingPayload` maps the QR fields to `PairingClaimRequest`; `PairedServer` is created only by `ServerPairingCoordinator` and consumed by `PairServerSheet` and profile mapping.
