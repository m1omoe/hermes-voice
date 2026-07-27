# Current State

## Branch

`main` — 9 commits ahead of `origin/main` (unpushed). ruff green repo-wide; CI exists.

## Plan — V1 = App Store 1.0

Full plan: `~/.claude/plans/here-is-a-handoff-fluffy-treasure.md` (rev 2; Sol REJECT + GLM REVISE folded in).

- [x] Phase B (part): ruff green `b72b90f`; first CI `ca8fb3f`. Baseline 375 passed / 0 skipped.
- [x] Phase B rest: worktrees pruned (`empty-serve-status`, `server-profiles`; all branches merged, deleted).
- [ ] **RESTORE the two lost hermes-agent changes — was "commit+push `fda7e544e`", now impossible: the source is GONE.** See Blockers.
- [ ] Phase A (blocking): APNs→background-poll+local notifications; review-window backend; `DemoTransport`; Claude launchd PATH; tokens→Keychain; in-app privacy policy + AI consent; ATS drop arbitrary loads.
- [ ] Phase C: clean-Mac setup+reboot on 2 fresh Macs, then device acceptance (incl. server-profiles Task 5; re-mirror Coruscant first). **Gates every deletion.**
- [ ] Phase D: cut Bonjour, legacy subprocess path, Piper/whisper/Groq, Kokoro remnants.
- [ ] Phase E/F/G: archive privacy audit → external TestFlight beta → submit → README truth pass.

## Blockers

- **hermes-agent patches LOST + unrestorable from source (found 2026-07-27).** Homebrew `hermes-agent 2026.7.20` replaced the editable install. `~/.hermes/hermes-agent` no longer exists; no git repo anywhere; no older Cellar version; changes were never pushed. Gone: the `ephemeral_system_prompt` prelude hook (`decisions.md [2026-06-15]`) and the voice fast profile (`[2026-06-20]`). hermes-voice still SENDS the `_meta` (`acp_client.py:194,359`); the stock agent absorbs it into `**kwargs` and ignores it. So since ~2026-07-20: full reasoning effort + self-improvement review between turns (the latency bug that was fixed in June is back) and zero conciseness shaping. **Public users get the unpatched agent regardless — patching a Homebrew libexec is not a shippable V1 architecture.** Re-implementable from the two ADRs.
- APNs unusable for public users — private `.p8` can't be distributed; provisioner installs none.
- `apns_use_sandbox=True` OR'd into endpoint choice → production tokens hit sandbox (`push.py:104`).
- `TurnTransport` covers the turn path only — 22 direct `HermesVoiceAPI(` sites.
- Claude resolved via bare `shutil.which` → likely absent under launchd (`adapters/claude.py:362`).

## Open questions

- Is opportunistic local-notification timing acceptable, or is a relay service wanted later?

## History

Pre-V1 narrative lived here; recover via `git show ca8fb3f:.docs/ai/current-state.md`. Rationale → `decisions.md`.
