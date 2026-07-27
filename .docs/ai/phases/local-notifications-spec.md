# Local notifications (replaces APNs) — design spec

Status: REVISED after Fable 5 companion review (verdict REVISE; blocker + foreground gap both fixed below). Decision: `decisions.md [2026-07-27]`.

## Why

APNs needs the developer's private `.p8` on every user's Mac; it cannot be distributed, and the provisioner installs none. Polling also removes the multi-backend identity problem outright — the phone knows which profile it polled — and keeps agent output off Apple's infrastructure.

## The blocker the first draft missed

Draft 1 polled `/api/sessions?source=schedule`. That cannot work, for three independent reasons, all verified:

1. A session's `source` is written by the harness, not by hermes-voice. The live `~/.hermes/state.db` contains exactly two values: `acp` and `cli`. There is no `schedule`, and `_fire_one` has no way to inject one — it calls `_run_turn` on the same path as a user turn (`schedules.py:500-502`).
2. The *schedule record's* `source` (`ios`) is a different field, on a different table, in a different database.
3. **Fatal, and a direct consequence of `decisions.md [2026-07-27]` making Claude the default harness:** `_fire_one` passes **no** `harness` argument, so fires run through the default. Claude sessions live in `~/.claude/projects/<slug>/<id>.jsonl`, while `/api/sessions` reads only `~/.hermes/state.db` (`sessions.py:19`). In the shipped configuration, fire sessions never appear in the polled endpoint at all.

## Shape — record the fire where the push dies

**Backend (one small, additive diff).** `_fire_one` already holds the session id and assistant text at the exact point it calls `send_push` (`schedules.py:511-524`). Replace that with a write of two new columns on the **schedule row** — `last_fired_session_id` and `last_fire_preview` (~200 chars) — **on success only** — and surface both in `ScheduleResponse`.

This is harness-agnostic (works for hermes, claude, codex alike), needs no new endpoint, no fires table, and no session-store dependency. `GET /api/schedules` is already consumed by `SchedulesView`/`HermesVoiceAPI`, and JSONDecoder ignores unknown keys, so the change is purely additive.

**Why a session-id marker, not a timestamp:** `_mark_fired` advances `last_fired_at` **on failure too** (`schedules.py:469-483`). Any timestamp diff would notify the user about fires that produced nothing. Equality-diffing `last_fired_session_id` — written only on success — avoids this for free, and assumes no monotonicity.

**iOS — one poll engine, two entry points.**

A `FirePoller` takes a set of profiles, **concurrently** GETs `/api/schedules` per profile (authenticated with that profile's URL + token), and diffs `last_fired_session_id` against its marker.

- *Entry point A — background.* `BGAppRefreshTask` over all profiles → one `UNNotificationRequest` per changed schedule → resubmit the task.
- *Entry point B — foreground.* `scenePhase .active` plus a ~30–60s timer on the active profile, feeding the existing chime → Live Activity → replay pipeline **directly**, posting no notification.

Entry point B is not optional: `BGAppRefreshTask` never runs while the app is foreground, so without it `willPresent` (`NotificationManager.swift:328-355`) becomes dead code and scheduled-arrival auto-play — the flagship arrival behavior — loses its only trigger.

## Budget and unreachability

~30s of runtime, scheduled entirely at iOS's discretion. The trap: URLSession's default request timeout is **60s — longer than the whole task budget** — and a profile behind a down tunnel fails by *timeout*, not refusal, so sequential "active-first" polling lets one dead Mac starve every other profile.

Required: concurrent task group; ~10s per-request timeout; commit each profile's marker the moment its fetch returns (incremental, so expiry loses nothing already done); expiration handler cancels the group and still calls `setTaskCompleted`; resubmit on every run and on `scenePhase .background`; skip entirely when notifications are disabled. "Active first" survives only as commit priority, never as ordering.

Rejected: `BGProcessingTask` (wants idle/charging), `NEAppPushProvider` (Wi-Fi-SSID-scoped, incompatible with Tailscale roaming), silent push (is APNs).

## Marker storage

A side table in UserDefaults, `hv.fireMarkers`, shaped `[profileUUID: [scheduleId: lastFiredSessionId]]` — deliberately **not** fields inside `BackendProfile`, which would force a Codable migration of the profiles blob and let background writes race the MainActor `didSet` persistence (`AppSettings.swift:77-89`).

Correctness: key by `BackendProfile.id` (a stable `let UUID`) so renames and URL edits keep markers; prune the entry in `removeProfile`; prune schedule ids absent from a response. **An absent marker primes silently** — record current state, notify nothing — so a new profile or a post-reinstall re-onboard never floods historical fires. Not Keychain: markers aren't secrets, and Keychain's reinstall survival is actively wrong here.

## Cross-profile arrival identity

`ScheduledArrival` (`NotificationManager.swift:73-79`) carries no profile field, and today the badge tap resumes against the **active** backend (`MainView.swift:28-31`) — so a profile-B fire resumed against active profile A 404s. Foreground auto-play likewise replays against `settings.backendURL` (`:424-430`).

Rule, consistent with the no-auto-switching non-goal: **same-profile arrivals** get full resume + auto-play, replay bound to the origin profile's URL + token; **cross-profile arrivals** degrade to a notification/badge naming the profile, with no resume. `userInfo` must carry `schedule_id`, `session_id`, and `profile_id`, or `parseArrival` returns nil and taps do nothing.

## Content

Default: title = schedule display name, body = "Schedule ran — tap to hear the result." No agent output on the lock screen — today's APNs path puts up to 300 raw chars there (`push.py:27,36`), which is part of what this replaces. Reply preview is **opt-in**, sourced from `last_fire_preview`.

Preserve parity: `UNNotificationSound(named: "hermes-chime.caf")` and `threadIdentifier = scheduleId` for per-schedule grouping. Missed fires collapsing to latest-only per schedule is **designed behavior**, not a bug.

## Honest timeliness promise

> "Your Mac runs schedules on time. Your phone checks for results periodically in the background — iOS decides when, typically within a few hours — and instantly whenever you open the app."

Required in three places: the Settings notifications footer (plus force-quit and Low Power Mode caveats); the schedule create/edit view, because the cadence picker is where the false expectation forms; and a per-profile "Last checked" row in diagnostics, mirroring the existing `lastReachable` pattern.

## Removal footprint (Phase D — after device acceptance)

Backend: `push.py`, the `devices` table, `/api/devices`. iOS, larger than draft 1 listed: `registerForRemoteNotifications` (`HermesVoiceApp.swift:26`), the AppDelegate APNs callbacks, `handleAPNsToken`/coalescing/`registeredTokens`/`DeviceRegistering`, the unregister half of `handleBackendSwitch` (keep `stopForegroundPlayback`), `lastApnsToken` and its diagnostics row, `NotificationSwitchTests.swift`, `remote-notification` in Info.plist (→ `fetch` + `BGTaskSchedulerPermittedIdentifiers`), and the `aps-environment` entitlement. **Notification permission stays** — local notifications require it.

## Non-goals

No developer-operated relay. No APNs in the shipped public path. No auto-switching of the active profile from a notification.
