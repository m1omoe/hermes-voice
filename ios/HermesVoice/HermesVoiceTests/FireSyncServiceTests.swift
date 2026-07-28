import XCTest
import UserNotifications
@testable import HermesVoice

/// Drives `FireSyncService` through a real `FirePoller` (mirrors
/// `FirePollerTests`' fake style) plus a fake notification scheduler, so the
/// full poll → filter → post → commit pipeline can be asserted without a
/// network or the real `UNUserNotificationCenter`.
final class FireSyncServiceTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        let suite = "FireSyncServiceTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
    }

    // MARK: - Test doubles

    final class FakeFireFeed: FireFeed, @unchecked Sendable {
        var result: Result<[HermesVoiceAPI.ScheduleFireEvent], Error> = .success([])
        func listScheduleFires(after: Int, limit: Int) async throws -> [HermesVoiceAPI.ScheduleFireEvent] {
            switch result {
            case .success(let events): return events
            case .failure(let error): throw error
            }
        }
    }

    final class FakeNotificationScheduler: NotificationScheduling, @unchecked Sendable {
        var status: UNAuthorizationStatus = .authorized
        private(set) var addedRequests: [UNNotificationRequest] = []
        /// Identifiers that should throw when added, simulating a failed
        /// enqueue for that specific notification.
        var failingIdentifiers: Set<String> = []

        func authorizationStatus() async -> UNAuthorizationStatus { status }

        func add(_ request: UNNotificationRequest) async throws {
            if failingIdentifiers.contains(request.identifier) {
                throw NSError(domain: "FakeNotificationScheduler", code: 1)
            }
            addedRequests.append(request)
        }
    }

    /// Mutable box standing in for a live `AppSettings.backendProfiles` read
    /// — lets a test simulate `saveProfile` replacing a profile's route
    /// under the same UUID between when a poll was bound and when
    /// `FireSyncService` checks it's still current.
    final class ProfileBox: @unchecked Sendable {
        var profile: BackendProfile
        init(_ profile: BackendProfile) { self.profile = profile }
    }

    // MARK: - Helpers

    private func makeProfile(
        url: String = "https://original.example", token: String = "tok"
    ) -> BackendProfile {
        BackendProfile(name: "p", url: url, authToken: token, selectedHarness: "hermes")
    }

    private func event(
        seq: Int, scheduleId: String = "sched", fireId: String = "fire",
        sessionId: String? = "sess", displayName: String? = "Weather updates",
        fullText: String = "the complete synthesized reply text", preview: String = "preview text"
    ) -> HermesVoiceAPI.ScheduleFireEvent {
        let sessionField = sessionId.map { "\"\($0)\"" } ?? "null"
        let displayField = displayName.map { "\"\($0)\"" } ?? "null"
        return try! JSONDecoder().decode(
            HermesVoiceAPI.ScheduleFireEvent.self,
            from: Data("""
            {"seq": \(seq), "fire_id": "\(fireId)", "schedule_id": "\(scheduleId)",
             "display_name": \(displayField), "harness_id": "hermes", "session_id": \(sessionField),
             "preview": "\(preview)", "full_text": "\(fullText)", "completed_at": 1700000000.0}
            """.utf8)
        )
    }

    /// Wires a `FireSyncService` to a real `FirePoller` (over `defaults`), a
    /// fresh `PendingArrivalStore`, and the given fakes. `currentProfile`
    /// defaults to resolving straight from `profiles` (no simulated
    /// staleness) unless a test overrides it.
    private func makeService(
        profiles: [BackendProfile], feed: FakeFireFeed, notifier: FakeNotificationScheduler,
        currentProfile: CurrentProfileLookup? = nil
    ) -> (FireSyncService, FireMarkerStore, PendingArrivalStore) {
        let markerStore = FireMarkerStore(defaults: defaults)
        let poller = FirePoller(markerStore: markerStore, makeFeed: { _ in feed })
        let pendingArrivals = PendingArrivalStore(defaults: defaults)
        let lookup: CurrentProfileLookup = currentProfile ?? { id in
            profiles.first { $0.id == id }
        }
        let service = FireSyncService(
            poller: poller, pendingArrivals: pendingArrivals, notifier: notifier,
            currentProfile: lookup
        )
        return (service, markerStore, pendingArrivals)
    }

    // MARK: - Tests

    func testHappyPathPostsOneNotificationPerScheduleAndCommitsCursor() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let (service, markerStore, pendingArrivals) = makeService(
            profiles: [profile], feed: feed, notifier: notifier
        )

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes silently

        feed.result = .success([
            event(seq: 1, scheduleId: "sched-a", fireId: "fire-a1"),
            event(seq: 2, scheduleId: "sched-b", fireId: "fire-b1"),
        ])
        let summary = await service.sync(profiles: [profile])

        XCTAssertEqual(summary.postedCount, 2)
        XCTAssertEqual(notifier.addedRequests.count, 2)
        XCTAssertEqual(summary.committedProfileIds, [profile.id])
        XCTAssertEqual(markerStore.cursor(for: profile.id), 2)

        let arrivals = pendingArrivals.all()
        XCTAssertEqual(arrivals.count, 2)
        XCTAssertTrue(arrivals.contains { $0.fullText == "the complete synthesized reply text" })
    }

    func testCollapsesThreeFiresOfOneScheduleIntoExactlyOneNotification() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let (service, markerStore, _) = makeService(profiles: [profile], feed: feed, notifier: notifier)

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes

        feed.result = .success([
            event(seq: 1, scheduleId: "sched", fireId: "f1"),
            event(seq: 2, scheduleId: "sched", fireId: "f2"),
            event(seq: 3, scheduleId: "sched", fireId: "f3"),
        ])
        let summary = await service.sync(profiles: [profile])

        XCTAssertEqual(summary.postedCount, 1, "three fires of one schedule must collapse to one notification")
        XCTAssertEqual(notifier.addedRequests.count, 1)
        XCTAssertEqual(notifier.addedRequests.first?.identifier, "sched-f3", "must use the LATEST fire")
        XCTAssertEqual(markerStore.cursor(for: profile.id), 3, "cursor must advance to the newest seq")
    }

    func testIdentifierIsExactlyScheduleIdDashFireIdAndThreadIdentifierIsScheduleId() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let (service, _, _) = makeService(profiles: [profile], feed: feed, notifier: notifier)

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes

        feed.result = .success([event(seq: 9, scheduleId: "abc", fireId: "xyz")])
        _ = await service.sync(profiles: [profile])

        let request = try XCTUnwrap(notifier.addedRequests.first)
        XCTAssertEqual(request.identifier, "abc-xyz")
        XCTAssertEqual(request.content.threadIdentifier, "abc")
        XCTAssertNotNil(request.content.sound)
        XCTAssertNil(request.trigger, "must deliver immediately")
    }

    func testNotAuthorizedPostsNothingAndCommitsNothing() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        notifier.status = .denied
        let (service, markerStore, _) = makeService(profiles: [profile], feed: feed, notifier: notifier)

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes at 0

        feed.result = .success([event(seq: 1)])
        let summary = await service.sync(profiles: [profile])

        XCTAssertTrue(notifier.addedRequests.isEmpty)
        XCTAssertTrue(summary.unauthorized)
        XCTAssertEqual(markerStore.cursor(for: profile.id), 0, "must not consume the result silently")
    }

    func testThrowingAddSkipsCommitAndTheEventRedeliversNextSync() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let (service, markerStore, _) = makeService(profiles: [profile], feed: feed, notifier: notifier)

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes at 0

        feed.result = .success([event(seq: 7, scheduleId: "sched", fireId: "f7")])
        notifier.failingIdentifiers = ["sched-f7"]
        let firstSummary = await service.sync(profiles: [profile])

        XCTAssertEqual(firstSummary.failedProfileIds, [profile.id])
        XCTAssertEqual(markerStore.cursor(for: profile.id), 0, "a throwing add must not commit")

        // Same event redelivers (cursor unchanged) and now succeeds.
        notifier.failingIdentifiers = []
        let secondSummary = await service.sync(profiles: [profile])

        XCTAssertEqual(secondSummary.postedCount, 1)
        XCTAssertEqual(markerStore.cursor(for: profile.id), 7)
    }

    func testStaleRouteIsDiscardedAndNotCommitted() async throws {
        let profile = makeProfile(url: "https://original.example", token: "tok-orig")
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let box = ProfileBox(profile)
        let (service, markerStore, _) = makeService(
            profiles: [profile], feed: feed, notifier: notifier,
            currentProfile: { id in box.profile.id == id ? box.profile : nil }
        )

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes at 0, box still matches

        // Simulate AppSettings.saveProfile replacing the route under the
        // same UUID while this next poll is "in flight".
        box.profile = BackendProfile(
            id: profile.id, name: profile.name, url: "https://changed.example",
            authToken: profile.authToken, selectedHarness: profile.selectedHarness
        )

        feed.result = .success([event(seq: 1)])
        let summary = await service.sync(profiles: [profile])

        XCTAssertTrue(notifier.addedRequests.isEmpty, "a stale route must not post")
        XCTAssertEqual(summary.staleProfileIds, [profile.id])
        XCTAssertEqual(markerStore.cursor(for: profile.id), 0, "must not commit past the primed cursor")
    }

    func testNullSessionIdStillProducesAnArrivalWithEmptyStringUserInfo() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let (service, _, pendingArrivals) = makeService(profiles: [profile], feed: feed, notifier: notifier)

        feed.result = .success([])
        _ = await service.sync(profiles: [profile])  // primes

        feed.result = .success([event(seq: 1, scheduleId: "sched", fireId: "f1", sessionId: nil)])
        let summary = await service.sync(profiles: [profile])

        XCTAssertEqual(summary.postedCount, 1, "a null session_id must not drop the arrival")
        let request = try XCTUnwrap(notifier.addedRequests.first)
        let sessionValue = request.content.userInfo["session_id"]
        XCTAssertNotNil(sessionValue, "session_id key must be present even when null")
        XCTAssertEqual(sessionValue as? String, "", "empty string, not a missing key")
        XCTAssertEqual(request.content.userInfo["schedule_id"] as? String, "sched")
        XCTAssertEqual(request.content.userInfo["fire_id"] as? String, "f1")
        XCTAssertEqual(request.content.userInfo["profile_id"] as? String, profile.id.uuidString)

        XCTAssertNil(pendingArrivals.all().first?.sessionId)
    }

    func testGenericBodyByDefaultAndOptInPreview() async throws {
        let profile = makeProfile()
        let feed = FakeFireFeed()
        let notifier = FakeNotificationScheduler()
        let markerStore = FireMarkerStore(defaults: defaults)
        let poller = FirePoller(markerStore: markerStore, makeFeed: { _ in feed })
        let pendingArrivals = PendingArrivalStore(defaults: defaults)
        let defaultService = FireSyncService(
            poller: poller, pendingArrivals: pendingArrivals, notifier: notifier,
            currentProfile: { _ in profile }
        )

        feed.result = .success([])
        _ = await defaultService.sync(profiles: [profile])  // primes

        feed.result = .success([event(seq: 1, preview: "the sensitive preview text")])
        _ = await defaultService.sync(profiles: [profile])

        let genericRequest = try XCTUnwrap(notifier.addedRequests.first)
        XCTAssertFalse(
            genericRequest.content.body.contains("sensitive preview"),
            "agent output must not appear in the body by default"
        )

        // Opt-in preview, fresh store/profile so it primes independently.
        let previewProfile = makeProfile(url: "https://preview.example")
        let previewFeed = FakeFireFeed()
        let previewNotifier = FakeNotificationScheduler()
        let previewMarkerStore = FireMarkerStore(defaults: defaults)
        let previewPoller = FirePoller(markerStore: previewMarkerStore, makeFeed: { _ in previewFeed })
        let previewService = FireSyncService(
            poller: previewPoller, pendingArrivals: pendingArrivals, notifier: previewNotifier,
            includePreviewInBody: true, currentProfile: { _ in previewProfile }
        )
        previewFeed.result = .success([])
        _ = await previewService.sync(profiles: [previewProfile])  // primes

        previewFeed.result = .success([event(seq: 1, preview: "the sensitive preview text")])
        _ = await previewService.sync(profiles: [previewProfile])

        let previewRequest = try XCTUnwrap(previewNotifier.addedRequests.first)
        XCTAssertTrue(previewRequest.content.body.contains("the sensitive preview text"))
    }
}
