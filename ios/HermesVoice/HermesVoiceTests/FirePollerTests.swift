import XCTest
@testable import HermesVoice

/// Drives `FirePoller` through the `FireFeed` seam (mirrors `TurnStateMachineTests`'
/// `FakeTransport` pattern) — scripted per-profile fakes stand in for the real
/// `HermesVoiceAPI`, so cursor priming, commit-gated advancement, per-profile
/// fault isolation, and true concurrency can all be asserted without a network.
final class FirePollerTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        let suite = "FirePollerTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
    }

    // MARK: - Test double

    /// A scripted `FireFeed`. One instance is normally dedicated to one
    /// profile in a test (via the `makeFeed` lookup passed to `FirePoller`).
    final class FakeFireFeed: FireFeed, @unchecked Sendable {
        /// What `listScheduleFires` returns (or throws) on its NEXT call.
        var result: Result<[HermesVoiceAPI.ScheduleFireEvent], Error> = .success([])
        /// Every `after` value this fake was called with, in call order —
        /// lets a test assert the poller is actually threading the cursor
        /// through, not just replaying a canned script.
        private(set) var afterCalls: [Int] = []

        // MARK: race-test gate (inert unless a test opts in)

        /// When true, a call parks until `release()` — used to prove two
        /// profiles are polled concurrently rather than one blocking the
        /// other.
        var gated = false
        private var didEnter = false
        private var enteredCont: CheckedContinuation<Void, Never>?
        private var releaseCont: CheckedContinuation<Void, Never>?

        func awaitEntered() async {
            if didEnter { return }
            await withCheckedContinuation { enteredCont = $0 }
        }

        func release() {
            releaseCont?.resume()
            releaseCont = nil
        }

        /// Successive pages, consumed one per call. When non-empty this wins
        /// over `result` — lets a test drive the multi-page priming walk.
        var pages: [[HermesVoiceAPI.ScheduleFireEvent]] = []

        func listScheduleFires(after: Int, limit: Int) async throws -> [HermesVoiceAPI.ScheduleFireEvent] {
            afterCalls.append(after)
            if gated {
                didEnter = true
                enteredCont?.resume()
                enteredCont = nil
                await withCheckedContinuation { releaseCont = $0 }
            }
            if !pages.isEmpty {
                return pages.removeFirst()
            }
            switch result {
            case .success(let events): return events
            case .failure(let error): throw error
            }
        }
    }

    // MARK: - Helpers

    private func makeProfile(name: String = "profile", url: String = "https://example.invalid") -> BackendProfile {
        BackendProfile(name: name, url: url, authToken: "tok-\(name)", selectedHarness: "hermes")
    }

    private func event(seq: Int, fireId: String = "fire") -> HermesVoiceAPI.ScheduleFireEvent {
        try! JSONDecoder().decode(
            HermesVoiceAPI.ScheduleFireEvent.self,
            from: Data("""
            {"seq": \(seq), "fire_id": "\(fireId)", "schedule_id": "sched",
             "display_name": null, "harness_id": "hermes", "session_id": null,
             "preview": "p", "full_text": "f", "completed_at": 1.0}
            """.utf8)
        )
    }

    // MARK: - Tests

    func testFirstContactPrimesSilentlyAndRecordsCursor() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let profile = makeProfile()
        let fake = FakeFireFeed()
        fake.result = .success([event(seq: 1), event(seq: 2)])
        let poller = FirePoller(markerStore: store, makeFeed: { _ in fake })

        let results = await poller.poll(profiles: [profile])

        XCTAssertEqual(results.count, 1)
        XCTAssertTrue(results[0].events.isEmpty, "first contact must prime silently — no events delivered")
        XCTAssertNil(results[0].error)
        XCTAssertEqual(store.cursor(for: profile.id), 2, "priming must record the highest seq seen")
        XCTAssertEqual(fake.afterCalls, [0], "a never-before-seen profile should be fetched from the beginning")
    }

    func testAfterPrimingALaterFireIsReturned() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let profile = makeProfile()
        let fake = FakeFireFeed()
        fake.result = .success([event(seq: 1), event(seq: 2)])
        let poller = FirePoller(markerStore: store, makeFeed: { _ in fake })

        _ = await poller.poll(profiles: [profile])  // primes at seq 2

        fake.result = .success([event(seq: 3, fireId: "later-fire")])
        let results = await poller.poll(profiles: [profile])

        XCTAssertEqual(results[0].events.map(\.seq), [3])
        XCTAssertEqual(results[0].events.first?.fireId, "later-fire")
        XCTAssertEqual(fake.afterCalls, [0, 2], "the second poll must ask for events after the primed cursor")
    }

    func testCursorAdvancesOnlyWhenCallerCommits() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let profile = makeProfile()
        let fake = FakeFireFeed()
        fake.result = .success([event(seq: 5)])
        let poller = FirePoller(markerStore: store, makeFeed: { _ in fake })

        _ = await poller.poll(profiles: [profile])  // primes at seq 5
        XCTAssertEqual(store.cursor(for: profile.id), 5)

        fake.result = .success([event(seq: 6, fireId: "uncommitted")])
        let firstDelivery = await poller.poll(profiles: [profile])
        XCTAssertEqual(firstDelivery[0].events.map(\.seq), [6])
        XCTAssertEqual(store.cursor(for: profile.id), 5, "the poller must not advance the cursor itself")

        // No commit happened — the same event must be redelivered.
        let redelivery = await poller.poll(profiles: [profile])
        XCTAssertEqual(redelivery[0].events.map(\.seq), [6], "without a commit, the same events must be redelivered")

        // Caller now accepts (e.g. posted the notification) and commits.
        await poller.commit(seq: 6, for: profile.id)
        XCTAssertEqual(store.cursor(for: profile.id), 6)

        fake.result = .success([])
        let afterCommit = await poller.poll(profiles: [profile])
        XCTAssertTrue(afterCommit[0].events.isEmpty)
        XCTAssertEqual(fake.afterCalls.last, 6)
    }

    func testOneUnreachableProfileDoesNotBlockOthersFromReturningResults() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let good = makeProfile(name: "good")
        let bad = makeProfile(name: "bad")

        let goodFeed = FakeFireFeed()
        goodFeed.result = .success([event(seq: 1)])
        let badFeed = FakeFireFeed()
        badFeed.result = .failure(URLError(.timedOut))

        let poller = FirePoller(markerStore: store, makeFeed: { profile in
            profile.id == good.id ? goodFeed : badFeed
        })

        let results = await poller.poll(profiles: [good, bad])

        XCTAssertEqual(results.count, 2, "a throwing profile must not prevent the batch from completing")
        let goodResult = try XCTUnwrap(results.first { $0.profileId == good.id })
        let badResult = try XCTUnwrap(results.first { $0.profileId == bad.id })

        XCTAssertNil(goodResult.error)
        XCTAssertNotNil(badResult.error, "the unreachable profile's own result must carry its error")
        XCTAssertEqual(store.cursor(for: good.id), 1, "the reachable profile still primes normally")
        XCTAssertNil(store.cursor(for: bad.id), "a throwing profile must not prime/commit a cursor")
    }

    /// Proves polling is concurrent, not sequential: both a "slow" and a
    /// "fast" profile's fake must receive their call and reach the gate
    /// BEFORE either is released. A sequential poller awaiting one profile's
    /// network call to completion before starting the next would never let
    /// both fakes reach "entered" simultaneously while both are still
    /// parked — it would have to finish (or time out on) the first before
    /// the second's `listScheduleFires` is ever invoked.
    func testProfilesArePolledConcurrentlyNotSequentially() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let slow = makeProfile(name: "slow")
        let fast = makeProfile(name: "fast")

        let slowFeed = FakeFireFeed()
        slowFeed.gated = true
        slowFeed.result = .success([])
        let fastFeed = FakeFireFeed()
        fastFeed.gated = true
        fastFeed.result = .success([])

        let poller = FirePoller(markerStore: store, makeFeed: { profile in
            profile.id == slow.id ? slowFeed : fastFeed
        })

        let task = Task { await poller.poll(profiles: [slow, fast]) }

        // Both must enter their gate before either is released.
        async let slowEntered: Void = slowFeed.awaitEntered()
        async let fastEntered: Void = fastFeed.awaitEntered()
        _ = await (slowEntered, fastEntered)

        // Release fast first, THEN slow — if the poller were secretly
        // sequential this ordering couldn't have been reached at all (fast
        // would never have entered before slow was released).
        fastFeed.release()
        slowFeed.release()

        let results = await task.value
        XCTAssertEqual(Set(results.map(\.profileId)), Set([slow.id, fast.id]))
    }

    // MARK: - Route binding

    /// `ProfilePollResult` must carry back the url/token actually used for
    /// the request, bound at request start — the caller compares this
    /// against the profile's CURRENT url/token to detect a route swapped
    /// out from under the same UUID mid-poll (`AppSettings.saveProfile`).
    func testResultCarriesTheBoundRouteUsedForTheRequest() async throws {
        let store = FireMarkerStore(defaults: defaults)
        let profile = makeProfile(name: "p", url: "https://original.example")
        let fake = FakeFireFeed()
        fake.result = .success([])
        let poller = FirePoller(markerStore: store, makeFeed: { _ in fake })

        let results = await poller.poll(profiles: [profile])

        XCTAssertEqual(results[0].boundURL, "https://original.example")
        XCTAssertEqual(results[0].boundToken, "tok-p")
    }
}

extension FirePollerTests {
    func testPrimingPagesToTheTrueEndInsteadOfOnePage() async throws {
        // The backend clamps limit to 200 while retention keeps 50 events PER
        // SCHEDULE, so a handful of schedules already exceeds a single page.
        // Priming to one page's maximum would leave the remainder to arrive as
        // "new" on the next poll and notify for long-past fires — the exact
        // flood priming exists to prevent. Use limit 2 to model that cheaply.
        let store = FireMarkerStore(defaults: defaults)
        let profile = makeProfile()
        let fake = FakeFireFeed()
        fake.pages = [
            [event(seq: 1), event(seq: 2)],   // full page -> keep walking
            [event(seq: 3), event(seq: 4)],   // full page -> keep walking
            [event(seq: 5)],                  // short page -> end
        ]
        let poller = FirePoller(markerStore: store, limit: 2, makeFeed: { _ in fake })

        let primed = await poller.poll(profiles: [profile])
        XCTAssertEqual(primed.first?.events.count, 0, "priming must deliver nothing")
        XCTAssertEqual(
            store.cursor(for: profile.id), 5,
            "primed to the last page's max, not the first page's"
        )
        XCTAssertEqual(fake.afterCalls, [0, 2, 4], "walked forward by max seq")

        // Nothing historical may resurface on the next poll.
        fake.pages = []
        fake.result = .success([])
        let next = await poller.poll(profiles: [profile])
        XCTAssertEqual(next.first?.events.count, 0)
        XCTAssertEqual(fake.afterCalls.last, 5, "resumes from the true end")
    }
}
