# Current State

## Branch

`main` — 23 commits ahead of `origin/main` (unpushed). Backend 388 tests, iOS 158, ruff clean, CI present.
Live backend HEALTHY (restarted 2026-07-28, pid 45989): `/health` 200 via Serve, `mock:false`, ACP child up, fds flat at 15.

## Plan — V1 = App Store 1.0

Plan: `~/.claude/plans/here-is-a-handoff-fluffy-treasure.md` (rev 2). Notification design: `phases/local-notifications-spec.md` (v3).

- [x] Phase B: ruff green; first CI; worktrees pruned; docs routed.
- [x] Phase A — Claude launchd PATH `de14cfc`; fire-events backend `2ae480d` + hardening `2e9295c`; iOS `FirePoller` `f1b4a60`; `FireSyncService` + `PendingArrivalStore` `09b01f4`.
- [x] OUTAGE FIXED `d7976e9` — SQLite connections never closed; 18h production down. See `decisions.md [2026-07-28]`.
- [ ] Phase A last piece: **app wiring** — `BGAppRefreshTask` (register in `didFinishLaunching`, resubmit at handler entry, ONE completion gate, expiration only cancels); foreground `scenePhase` + timer over ALL profiles; arrival rewiring (persist independent of cursor so an app-icon launch reconstructs; Live Activity + auto-play must use `full_text`, not the notification body; replay binds the ORIGIN profile); cross-profile = explicit switch, never auto; `project.yml` `fetch` + `BGTaskSchedulerPermittedIdentifiers`.
- [ ] Phase A — rest: review-window backend; `DemoTransport` (turn-path only); tokens→Keychain; in-app privacy policy + AI consent; ATS drop arbitrary loads (pair with the Bonjour cut, NOT before Phase C).
- [ ] Phase C: clean-Mac setup **+ reboot + soak** on 2 fresh Macs, then device acceptance (incl. server-profiles Task 5; re-mirror Coruscant first). **Gates every deletion.**
- [ ] Phase D: cut APNs/devices/push, Bonjour, legacy subprocess path, Piper/whisper/Groq, Kokoro.
- [ ] Phase E/F/G: archive privacy audit → external TestFlight beta → submit → README truth pass.

## Blockers

- **hermes-agent patches LOST, unrestorable** (Homebrew `2026.7.20` replaced the editable install; never pushed). Mitigated for V1 by defaulting to Claude (`decisions.md [2026-07-27]`); re-implementable from the ADRs.
- Setup readiness gate validates Hermes regardless of configured default (`provision_macos.py:848`) — can bless a Mac whose default harness cannot run a turn.
- `apns_use_sandbox=True` sends production tokens to sandbox (`push.py:104`). **Do not fix** — that code is deleted in Phase D.
- ATS ordering: dropping `NSAllowsArbitraryLoads` before Phase C breaks Bonjour/`http://` discovery *during* acceptance.
- `KeepAlive` cannot detect a live-but-unresponsive backend — needs a health probe (`decisions.md [2026-07-28]`).

## Open questions

- None blocking. 23 unpushed commits is real single-machine exposure — today's work includes an outage root cause that exists nowhere else.

## History

Pre-V1 narrative: `git show ca8fb3f:.docs/ai/current-state.md`. Rationale → `decisions.md`.
