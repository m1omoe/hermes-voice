# Schedule results sync (replaces APNs) — design spec v3

Status: v3, after Fable 5 (REVISE) and GPT-5.6 Sol (RETHINK). Decision: `decisions.md [2026-07-27]`.

## Why, and what this is NOT

APNs needs the developer's private `.p8` on every user's Mac; it cannot be distributed, and the provisioner installs none. The phone instead polls its own backends and raises local notifications.

**This is a sync feature, not a delivery guarantee — and it must be named that way.** `earliestBeginDate` is a lower bound Apple explicitly does not honour; Low Power Mode disables background refresh outright; force-quit stops it until manual relaunch. The real contract is *"a few hours, or whenever you next open the app."* Every piece of user-facing copy says that. If timely delivery is ever required, the upgrade is a developer-operated relay sending a **content-free wake** ("profile X has news") with the phone still fetching the result from its own Mac — deliberately deferred to 1.1, and the data model below makes it a drop-in rather than a rewrite.

## Data model — append-only events, not columns on a mutable row

v2 put `last_fired_session_id` + `last_fire_preview` on the schedule row. That fails several ways at once: a harness session ID is not a safe fire identity (`_run_turn` permits it to collapse to `""`, `main.py:897`), the row is mutable configuration so a prompt/name edit races the result, a deleted schedule loses its result, and a 200-char preview cannot feed `/api/replay`, which needs the real text.

**New table `schedule_fire_events`**, append-only, written in the **same transaction** as the `_mark_fired` success path (one commit after `_run_turn` returns — a second transaction can leave a completed schedule with no marker):

| column | why |
|---|---|
| `seq` INTEGER PK AUTOINCREMENT | the poll cursor; monotonic, harness-agnostic |
| `fire_id` TEXT | scheduler-generated, always non-empty |
| `schedule_id` TEXT | joins back to config |
| `display_name` TEXT | **snapshot at fire time** — a later rename must not relabel history |
| `harness_id` TEXT | records which harness actually ran it; schedules carry no harness today, so resume must not assume the profile's current one |
| `session_id` TEXT NULL | optional, for resume; never used as identity |
| `preview` TEXT | ~200 chars, lock-screen use only |
| `full_text` TEXT | what `/api/replay` and auto-play actually speak |
| `completed_at` INTEGER | set after the turn returns, not before the semaphore |

Endpoint: `GET /api/schedule-fires?after=<seq>&limit=N`. One cursor per profile — not a nested per-schedule map. Bounded retention.

**Migration is mandatory and currently absent.** `_init_sync` uses only `CREATE TABLE IF NOT EXISTS` (`schedules.py:113-149`) and there is **no `ALTER TABLE` or `PRAGMA user_version` anywhere in the backend** — verified. A new table is additive, but the mechanism must exist and be tested **against a database created by the old schema**, not only a fresh temp DB, or CI passes green while every existing install breaks.

**Compatibility is two-way.** New client + old backend must not silently observe "nothing ever fired." The endpoint's absence is an explicit unsupported signal → diagnostics say *"backend upgrade required for schedule sync."*

## Polling

One `FirePoller` **actor** (single-flight, owning marker mutation), used by both entry points and always across **all** profiles:

- **Background** — `BGAppRefreshTask`: register the identifier in `didFinishLaunching` (`AppDelegate.swift:8-14` is currently a no-op); resubmit at handler entry; concurrent task group, ~10s per-request timeout plus a top-level deadline under the empirical budget; bounded concurrency; **exactly one completion gate** (expiration only cancels — the naive "expiration also calls `setTaskCompleted`" is a double-completion race).
- **Foreground** — `scenePhase .active` plus a light timer, also **all profiles**. Active-profile results feed the chime/Live-Activity/replay path directly; inactive-profile results raise an in-app badge naming the origin. (v2 polled only the active profile in foreground, which made Mac B invisible for as long as the app stayed open.)

**Cursor correctness.** Baseline is **per profile, on first successful contact** — record current state, notify nothing. Not per schedule: a schedule created and fired before the first poll would otherwise have its first fire silently swallowed. Advance the cursor **after** the notification is accepted, and check real `UNAuthorizationStatus` rather than the app-local Boolean, so a revoked permission or a failed enqueue cannot consume a result. On enable/re-enable, baseline silently first.

**Route safety.** A profile UUID alone is not origin identity: `saveProfile` can replace URL, token, and harness under the same UUID (`AppSettings.swift:275-292`). Every request binds a route revision and revalidates before committing; profile edit or removal cancels in-flight polls, so a late response cannot be attributed to a new endpoint or resurrect a pruned cursor.

## Arrival behaviour

Result data comes from `full_text`, never from the notification body. Default body stays generic ("Schedule ran"); preview is opt-in; keep `hermes-chime.caf` and `threadIdentifier = scheduleId`.

**Cross-profile arrivals must remain actionable.** v2 said "no resume," which avoids a wrong-backend 404 by discarding the feature. Instead: show the stored result regardless of active routing, then offer an explicit *"Switch to Mac B and continue"* guarded by the existing switch-eligibility checks. No automatic switching. If the profile is gone, show the retained result and a clear unavailable-route state.

**Arrivals must survive an ordinary app-icon launch.** `lastScheduledArrival` is populated only by `willPresent` or a notification tap (`NotificationManager.swift:328-385`), so a background-posted notification the user never taps currently reconstructs nothing. Persist pending arrivals independently of the cursor. The direct foreground path must update badge and Live Activity explicitly — Live Activity is local-only (`pushType: nil`) so it survives the APNs removal, but its content path must be rewired off `arrival.body`.

## Removal footprint (Phase D — after device acceptance)

Backend: `push.py`, `devices` table, `/api/devices`, APNs settings + env loading (`config.py:119-125,173-177`), the `aioapns` dependency and lockfile entry, device models (`models.py:167-183`), device/push tests, APNs setup docs. iOS: `registerForRemoteNotifications`, AppDelegate APNs callbacks, `handleAPNsToken`/coalescing/`registeredTokens`/`DeviceRegistering`, the unregister half of `handleBackendSwitch` (keep `stopForegroundPlayback`), `lastApnsToken` + diagnostics row, `remote-notification` and the `aps-environment` entitlement — edited in **`project.yml`**, which is the source of truth that regenerates Info.plist.

**Do not simply delete `NotificationSwitchTests.swift`** — it carries race coverage the new implementation still needs. Replace with route-snapshot, poll-cancellation, cursor-CAS, and cross-profile arrival tests.

## Acceptance (physical device)

Locked phone + worn Watch; foreground with active and inactive profiles; background refresh; Low Power Mode; force-quit then relaunch; notification tap; and an ordinary app-icon launch. Watch delivery is system mirroring and inherits every iPhone delay.

## Non-goals

No relay in V1. No APNs in the shipped path. No automatic profile switching.
