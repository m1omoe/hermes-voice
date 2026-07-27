# Current State

## Branch

`main` — 9 commits ahead of `origin/main` (unpushed). ruff green repo-wide; CI exists.

## Plan — V1 = App Store 1.0

Full plan: `~/.claude/plans/here-is-a-handoff-fluffy-treasure.md` (rev 2; Sol REJECT + GLM REVISE folded in).

- [x] Phase B (part): ruff green `b72b90f`; first CI `ca8fb3f`. Baseline 375 passed / 0 skipped.
- [ ] Phase B rest: prune worktrees `empty-serve-status` + `server-profiles`; commit+push hermes-agent `fda7e544e` — release gate, voice latency depends on it.
- [ ] Phase A (blocking): APNs→background-poll+local notifications; review-window backend; `DemoTransport`; Claude launchd PATH; tokens→Keychain; in-app privacy policy + AI consent; ATS drop arbitrary loads.
- [ ] Phase C: clean-Mac setup+reboot on 2 fresh Macs, then device acceptance (incl. server-profiles Task 5; re-mirror Coruscant first). **Gates every deletion.**
- [ ] Phase D: cut Bonjour, legacy subprocess path, Piper/whisper/Groq, Kokoro remnants.
- [ ] Phase E/F/G: archive privacy audit → external TestFlight beta → submit → README truth pass.

## Blockers

- APNs unusable for public users — private `.p8` can't be distributed; provisioner installs none.
- `apns_use_sandbox=True` OR'd into endpoint choice → production tokens hit sandbox (`push.py:104`).
- `TurnTransport` covers the turn path only — 22 direct `HermesVoiceAPI(` sites.
- Claude resolved via bare `shutil.which` → likely absent under launchd (`adapters/claude.py:362`).

## Open questions

- Is opportunistic local-notification timing acceptable, or is a relay service wanted later?

## History

Pre-V1 narrative lived here; recover via `git show ca8fb3f:.docs/ai/current-state.md`. Rationale → `decisions.md`.
