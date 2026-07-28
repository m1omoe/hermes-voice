# Current State

## Branch

`main` — 16 commits ahead of `origin/main` (unpushed). ruff green repo-wide; CI exists; 386 backend tests.

## Plan — V1 = App Store 1.0

Full plan: `~/.claude/plans/here-is-a-handoff-fluffy-treasure.md` (rev 2). Notification design: `phases/local-notifications-spec.md` (v3, after Fable REVISE + Sol RETHINK).

- [x] Phase B: ruff green `b72b90f`; first CI `ca8fb3f`; worktrees pruned; docs routed.
- [x] Phase A — Claude launchd PATH `de14cfc` (CLAUDE_BIN through adapter + wrapper + plist).
- [x] Phase A — schedule fire events backend `2ae480d` (append-only table, same-transaction write, `GET /api/schedule-fires`, retention 50/schedule, semaphore timing fix).
- [ ] Phase A — **iOS `FirePoller`** (the big remaining piece): all-profile poll on BGAppRefreshTask + foreground scenePhase/timer; per-profile cursor primed silently on first contact; route-revision binding; cursor advanced only AFTER notification acceptance; cross-profile arrivals actionable via explicit switch, never auto-switch.
- [ ] Phase A — rest: review-window backend; `DemoTransport` (turn-path only); tokens→Keychain; in-app privacy policy + AI consent; ATS drop arbitrary loads (do WITH the Bonjour cut, not before — see Blockers).
- [ ] Phase C: clean-Mac setup+reboot on 2 fresh Macs, then device acceptance (incl. server-profiles Task 5; re-mirror Coruscant first). **Gates every deletion.**
- [ ] Phase D: cut APNs/devices/push, Bonjour, legacy subprocess path, Piper/whisper/Groq, Kokoro.
- [ ] Phase E/F/G: archive privacy audit → external TestFlight beta → submit → README truth pass.

## Blockers

- **hermes-agent patches LOST, unrestorable from source.** Homebrew `2026.7.20` replaced the editable install; no repo, no older Cellar, never pushed. Gone: `ephemeral_system_prompt` hook (`decisions.md [2026-06-15]`) + voice fast profile (`[2026-06-20]`). Since ~2026-07-20: full reasoning effort + between-turn self-improvement review, and no conciseness shaping. Mitigated for V1 by defaulting to Claude (`[2026-07-27]`); re-implementable from the ADRs.
- `apns_use_sandbox=True` OR'd into endpoint choice → production tokens hit sandbox (`push.py:104`). Moot once APNs is cut in Phase D; do not "fix" it.
- `TurnTransport` covers the turn path only — 22 direct `HermesVoiceAPI(` sites, so demo mode stays turn-path only.
- ATS ordering: dropping `NSAllowsArbitraryLoads` before Phase C would break Bonjour/`http://` discovery *during* acceptance. Pair it with the Bonjour cut.
- Setup readiness gate validates Hermes regardless of configured default (`provision_macos.py:848`) — can bless a Mac whose default harness cannot run a turn.

## Open questions

- Fire-events backend is implemented but was reviewed only by me; an adversarial pass is in flight (GLM 5.2 — `opencode-go` lane is 429 quota-exhausted for ~15 days, retried via `neuralwatt`).

## History

Pre-V1 narrative: `git show ca8fb3f:.docs/ai/current-state.md`. Rationale → `decisions.md`.
