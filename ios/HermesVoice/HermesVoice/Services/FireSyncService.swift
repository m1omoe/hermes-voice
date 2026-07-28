import Foundation
import UserNotifications

/// The narrow slice of `UNUserNotificationCenter` `FireSyncService` needs:
/// the REAL OS-level authorization status — never an app-local Bool, since
/// the user can revoke notification permission independently of any in-app
/// toggle — and posting one request. Mirrors the `FireFeed`/`DeviceRegistering`
/// seam (`FirePoller.swift`, `NotificationManager.swift`): protocol + empty
/// conformance extension on the real type, so tests never touch the real
/// `UNUserNotificationCenter`.
protocol NotificationScheduling: Sendable {
    func authorizationStatus() async -> UNAuthorizationStatus
    func add(_ request: UNNotificationRequest) async throws
}

extension UNUserNotificationCenter: NotificationScheduling {
    func authorizationStatus() async -> UNAuthorizationStatus {
        await notificationSettings().authorizationStatus
    }
}

/// Supplies a profile's CURRENT url/token by id, looked up FRESH — not from
/// whatever `profiles` snapshot `sync(profiles:)` was called with.
///
/// This seam is not one of the dependencies named in the local-notifications
/// spec's own list, but it's required for the "route staleness" behaviour the
/// spec describes to be checkable at all: `ProfilePollResult.boundURL`/
/// `boundToken` are captured once, at that profile's request start
/// (`FirePoller.pollOne`), directly from whatever `BackendProfile` value was
/// in the `profiles` array passed to `poll(profiles:)`. Comparing THAT result
/// back against the very same frozen array `sync(profiles:)` received would
/// always trivially match — a plain Swift array can't observe an edit that
/// happens after it was captured. Only a lookup that re-reads live state
/// (in production, `AppSettings.backendProfiles` at commit time, after the
/// poll's ~10s network wait) can tell a late response bound to an
/// already-replaced route apart from a fresh one. Returns nil when the
/// profile no longer exists — treated the same as a mismatch: discard,
/// don't commit.
typealias CurrentProfileLookup = @Sendable (UUID) -> BackendProfile?

/// Aggregate result of one `sync(profiles:)` call.
struct FireSyncSummary: Sendable, Equatable {
    /// `UNNotificationRequest`s successfully posted, across every profile.
    let postedCount: Int
    /// Profiles whose cursor was committed this call (every surviving event
    /// posted without error).
    let committedProfileIds: Set<UUID>
    /// Profiles skipped because `FirePoller` returned an error for them —
    /// no notification, no commit.
    let erroredProfileIds: Set<UUID>
    /// Profiles skipped because their poll result's bound route no longer
    /// matches the profile's current url/token (or the profile no longer
    /// exists) — discarded, no commit.
    let staleProfileIds: Set<UUID>
    /// Profiles where at least one `add()` call threw — NOT committed, so
    /// every event for that profile (including any that did post) redelivers
    /// on the next sync.
    let failedProfileIds: Set<UUID>
    /// True when real notification authorization was not granted — nothing
    /// was posted or committed for ANY profile this call.
    let unauthorized: Bool
}

/// Turns `FirePoller` results into local notifications, then commits cursors
/// — the layer directly above `FirePoller`/`FireMarkerStore` (local-
/// notifications-spec, "Arrival behaviour" + "Cursor correctness").
///
/// Order is load-bearing throughout `sync(profiles:)`: poll → drop errors →
/// drop stale routes → gate on real authorization → collapse to latest-per-
/// schedule → post → persist the full text → commit. Nothing is committed
/// until AFTER its notification is accepted, so a failure at any step simply
/// redelivers the same events next time (`FirePoller`'s at-least-once
/// contract).
struct FireSyncService {
    private let poller: FirePoller
    private let pendingArrivals: PendingArrivalStore
    private let notifier: NotificationScheduling
    private let currentProfile: CurrentProfileLookup
    /// When true, the notification body includes the fire's `preview` text.
    /// Default OFF: agent output must never reach the lock screen unless the
    /// user opts in (local-notifications-spec, "Arrival behaviour").
    private let includePreviewInBody: Bool

    /// - Parameters:
    ///   - poller: the single-flight actor that owns cursor reads and
    ///     provides poll results; this service is the sole caller of its
    ///     `commit(seq:for:)`.
    ///   - pendingArrivals: where every posted fire's full text is persisted,
    ///     independent of the poll cursor.
    ///   - notifier: defaults to the real `UNUserNotificationCenter`; tests
    ///     inject a fake so they never touch the real notification center.
    ///   - includePreviewInBody: opt-in lock-screen preview; default off.
    ///   - currentProfile: live per-id profile lookup for the route-
    ///     staleness check — see `CurrentProfileLookup`'s doc comment.
    init(
        poller: FirePoller,
        pendingArrivals: PendingArrivalStore,
        notifier: NotificationScheduling = UNUserNotificationCenter.current(),
        includePreviewInBody: Bool = false,
        currentProfile: @escaping CurrentProfileLookup
    ) {
        self.poller = poller
        self.pendingArrivals = pendingArrivals
        self.notifier = notifier
        self.includePreviewInBody = includePreviewInBody
        self.currentProfile = currentProfile
    }

    /// Polls every profile, posts one notification per surviving schedule,
    /// and commits cursors only for profiles that fully succeeded. See the
    /// type doc for the required step order.
    func sync(profiles: [BackendProfile]) async -> FireSyncSummary {
        let results = await poller.poll(profiles: profiles)

        var erroredProfileIds: Set<UUID> = []
        var staleProfileIds: Set<UUID> = []
        var candidates: [(profileId: UUID, events: [HermesVoiceAPI.ScheduleFireEvent])] = []

        for result in results {
            // (b) Skip errored results outright — no notification, no commit.
            guard result.error == nil else {
                erroredProfileIds.insert(result.profileId)
                continue
            }
            // (c) Route staleness: the profile must still resolve to the
            // exact url/token this result was bound to at request start.
            guard let current = currentProfile(result.profileId),
                  current.url == result.boundURL,
                  current.authToken == result.boundToken
            else {
                staleProfileIds.insert(result.profileId)
                continue
            }
            guard !result.events.isEmpty else { continue }
            candidates.append((result.profileId, result.events))
        }

        guard !candidates.isEmpty else {
            return FireSyncSummary(
                postedCount: 0, committedProfileIds: [], erroredProfileIds: erroredProfileIds,
                staleProfileIds: staleProfileIds, failedProfileIds: [], unauthorized: false
            )
        }

        // (d) Real OS-level authorization, checked once per call — not an
        // app-local Bool, since the user can revoke it independently. A
        // revoked/never-granted status must not silently consume results.
        let status = await notifier.authorizationStatus()
        guard status == .authorized || status == .provisional else {
            return FireSyncSummary(
                postedCount: 0, committedProfileIds: [], erroredProfileIds: erroredProfileIds,
                staleProfileIds: staleProfileIds, failedProfileIds: [], unauthorized: true
            )
        }

        var postedCount = 0
        var committedProfileIds: Set<UUID> = []
        var failedProfileIds: Set<UUID> = []

        for (profileId, events) in candidates {
            // (e) Collapse to latest-per-schedule: multiple fires of one
            // schedule since the last poll produce ONE notification —
            // deliberate, see local-notifications-spec and the retention
            // comment in backend/app/schedules.py.
            let latestBySchedule = Dictionary(grouping: events, by: \.scheduleId)
                .compactMapValues { $0.max(by: { $0.seq < $1.seq }) }

            var profileFailed = false
            for event in latestBySchedule.values {
                let request = makeRequest(for: event, profileId: profileId)
                do {
                    // (f) Post.
                    try await notifier.add(request)
                    postedCount += 1
                    // (g) Persist the FULL text — the notification body is
                    // generic and must never be spoken.
                    pendingArrivals.append(PendingArrival(
                        scheduleId: event.scheduleId,
                        fireId: event.fireId,
                        profileId: profileId,
                        sessionId: event.sessionId,
                        displayName: displayName(for: event),
                        fullText: event.fullText,
                        completedAt: Date(timeIntervalSince1970: event.completedAt)
                    ))
                } catch {
                    profileFailed = true
                }
            }

            // (h) Only after every surviving schedule's notification is
            // accepted does this profile's cursor advance. A throwing
            // `add()` anywhere for this profile skips the commit entirely —
            // `FireMarkerStore`'s cursor is per-profile, not per-schedule, so
            // partial commit isn't possible; the whole batch redelivers.
            if profileFailed {
                failedProfileIds.insert(profileId)
            } else if let maxSeq = events.map(\.seq).max() {
                await poller.commit(seq: maxSeq, for: profileId)
                committedProfileIds.insert(profileId)
            }
        }

        return FireSyncSummary(
            postedCount: postedCount, committedProfileIds: committedProfileIds,
            erroredProfileIds: erroredProfileIds, staleProfileIds: staleProfileIds,
            failedProfileIds: failedProfileIds, unauthorized: false
        )
    }

    private func displayName(for event: HermesVoiceAPI.ScheduleFireEvent) -> String {
        event.displayName?.isEmpty == false ? event.displayName! : "Schedule"
    }

    private func makeRequest(
        for event: HermesVoiceAPI.ScheduleFireEvent, profileId: UUID
    ) -> UNNotificationRequest {
        let content = UNMutableNotificationContent()
        content.title = displayName(for: event)
        // Generic by default — agent output must not reach the lock screen
        // unless the user opted into `includePreviewInBody`.
        content.body = (includePreviewInBody && !event.preview.isEmpty)
            ? event.preview
            : "Schedule ran — tap to hear the result"
        content.sound = UNNotificationSound(named: UNNotificationSoundName("hermes-chime.caf"))
        content.threadIdentifier = event.scheduleId
        content.userInfo = [
            "schedule_id": event.scheduleId,
            "profile_id": profileId.uuidString,
            "fire_id": event.fireId,
            // A fire's session_id is legitimately null. An empty string (not
            // a missing key) means "not resumable" to the consuming side —
            // `NotificationManager.parseArrival` requires BOTH schedule_id
            // AND session_id and returns nil otherwise, which would
            // otherwise drop the arrival entirely for a null session.
            "session_id": event.sessionId ?? "",
        ]
        return UNNotificationRequest(
            // Load-bearing: the poller redelivers uncommitted events, and
            // this exact identifier is what makes iOS coalesce the
            // redelivered duplicate instead of double-notifying.
            identifier: "\(event.scheduleId)-\(event.fireId)",
            content: content,
            trigger: nil
        )
    }
}
