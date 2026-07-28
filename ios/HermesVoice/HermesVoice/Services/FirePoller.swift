import Foundation

/// One polled profile's outcome from `FirePoller.poll(profiles:)`. Always
/// returned — even on failure — so one dead Mac produces an error result
/// for its own profile instead of silently starving (or vanishing from) the
/// rest of the batch.
struct ProfilePollResult: Sendable {
    let profileId: UUID
    /// New fire events for this profile, ascending by `seq`. Empty when
    /// this poll primed the cursor (first contact) or when nothing new has
    /// fired since the last committed cursor.
    let events: [HermesVoiceAPI.ScheduleFireEvent]
    /// Set when the request failed (timeout, transport error, decode
    /// error, an old backend that 404s the endpoint, etc). `events` is
    /// empty whenever this is set.
    let error: Error?
    /// The url/token actually used for THIS request, captured at request
    /// start — not read live from the profile. `AppSettings.saveProfile`
    /// can replace a profile's url/token under the same UUID, so a caller
    /// must compare these against the profile's CURRENT url/token before
    /// accepting a commit; a mismatch means the route changed mid-poll and
    /// a late response must not be attributed to the new endpoint.
    let boundURL: String
    let boundToken: String
}

/// Polls the schedule-fire sync feed (`GET /api/schedule-fires`,
/// local-notifications-spec) across every backend profile concurrently.
///
/// An `actor` for single-flight: it is the SOLE owner of marker mutation
/// (via the injected `FireMarkerStore`), so two overlapping `poll()` calls
/// serialize their cursor reads/writes instead of interleaving into a
/// corrupted cursor.
///
/// Deliberately does NOT advance cursors itself, except for first-contact
/// priming — see `poll(profiles:)`.
actor FirePoller {
    private let markerStore: FireMarkerStore
    private let limit: Int
    private let makeFeed: @Sendable (BackendProfile) -> FireFeed

    /// - Parameters:
    ///   - markerStore: cursor persistence; this actor is its sole mutator.
    ///   - limit: page size passed to `listScheduleFires`. Defaults to the
    ///     backend's max (200) so one poll can absorb a large backlog
    ///     without needing follow-up pages.
    ///   - makeFeed: builds the `FireFeed` used to poll one profile.
    ///     Production's default constructs a real `HermesVoiceAPI` bound to
    ///     the profile's url/token with a short per-request timeout; tests
    ///     inject a fake keyed by profile id.
    init(
        markerStore: FireMarkerStore,
        limit: Int = 200,
        makeFeed: @escaping @Sendable (BackendProfile) -> FireFeed = FirePoller.defaultFeed
    ) {
        self.markerStore = markerStore
        self.limit = limit
        self.makeFeed = makeFeed
    }

    /// Polls every profile in `profiles` CONCURRENTLY — never sequentially.
    /// URLSession's default timeout (60s) exceeds the whole background
    /// budget, so a profile behind a down Tailscale tunnel fails by
    /// timeout; sequential polling would let that one dead Mac starve every
    /// other profile. Each profile's fetch uses a ~10s per-request timeout
    /// (see `defaultFeed`).
    ///
    /// Cursors are NOT advanced here except for first-contact priming (see
    /// `pollOne`) — the caller commits via `commit(seq:for:)` only after it
    /// has accepted the returned events (e.g. posted notifications for
    /// them). An uncommitted profile simply re-delivers the same events
    /// next poll; that at-least-once behavior is deliberate and safe.
    func poll(profiles: [BackendProfile]) async -> [ProfilePollResult] {
        await withTaskGroup(of: ProfilePollResult.self) { group in
            for profile in profiles {
                group.addTask { await self.pollOne(profile) }
            }
            var results: [ProfilePollResult] = []
            results.reserveCapacity(profiles.count)
            for await result in group {
                results.append(result)
            }
            return results
        }
    }

    /// Commits `seq` as the new cursor for `profileId`. This — together with
    /// the silent priming commit inside `pollOne` — is the only way markers
    /// are written, and only after the caller has accepted the events up
    /// through `seq`. Actor-isolated so it serializes with any concurrent
    /// `poll()` bookkeeping.
    func commit(seq: Int, for profileId: UUID) {
        markerStore.commit(seq, for: profileId)
    }

    private func pollOne(_ profile: BackendProfile) async -> ProfilePollResult {
        let feed = makeFeed(profile)
        let priorCursor = markerStore.cursor(for: profile.id)
        let after = priorCursor ?? 0

        do {
            let events = try await feed.listScheduleFires(after: after, limit: limit)
            guard priorCursor != nil else {
                // First contact for this profile: prime silently. Commit
                // immediately — there is nothing to deliver — and return no
                // events. Per PROFILE, not per schedule: priming per
                // schedule would swallow the first fire of a schedule
                // created after this poll.
                //
                // Page to the true end rather than trusting one response.
                // The backend clamps limit to 200 while retention keeps 50
                // events PER SCHEDULE, so a handful of schedules already
                // exceeds one page. Priming to a partial maximum would leave
                // the remainder to arrive as "new" on the next poll and
                // notify for long-past fires — the exact flood priming
                // exists to prevent. Bounded: retention bounds the table.
                var highest = after
                var page = events
                while true {
                    highest = max(highest, page.map(\.seq).max() ?? highest)
                    guard page.count >= limit else { break }
                    page = try await feed.listScheduleFires(after: highest, limit: limit)
                    if page.isEmpty { break }
                }
                markerStore.commit(highest, for: profile.id)
                return ProfilePollResult(
                    profileId: profile.id, events: [], error: nil,
                    boundURL: profile.url, boundToken: profile.authToken
                )
            }
            return ProfilePollResult(
                profileId: profile.id, events: events, error: nil,
                boundURL: profile.url, boundToken: profile.authToken
            )
        } catch {
            return ProfilePollResult(
                profileId: profile.id, events: [], error: error,
                boundURL: profile.url, boundToken: profile.authToken
            )
        }
    }

    /// Production `makeFeed`: a real `HermesVoiceAPI` bound to `profile`'s
    /// url/token at request-start, using a session configured with a ~10s
    /// per-request timeout so one unreachable profile can't ride
    /// URLSession's 60s default and starve the rest of the batch.
    static func defaultFeed(for profile: BackendProfile) -> FireFeed {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 10
        return HermesVoiceAPI(
            baseURL: profile.url,
            authToken: profile.authToken,
            session: URLSession(configuration: config)
        )
    }
}
