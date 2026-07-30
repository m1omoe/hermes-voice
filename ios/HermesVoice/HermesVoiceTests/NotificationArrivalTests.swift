import XCTest
import UserNotifications
@testable import HermesVoice

/// Covers the arrival-parsing / badge-routing behavior added for the
/// generic-notification-body redesign (local-notifications-spec, "Arrival
/// behaviour"): `parseArrival` resolving full text via `PendingArrivalStore`,
/// the pure same-profile / cross-profile / unavailable badge decision, and
/// the app-icon-launch reconstruction + consumption paths.
/// `NotificationSwitchTests` covers this same manager's separate
/// device-registration flows.
@MainActor
final class NotificationArrivalTests: XCTestCase {
    private var defaults: UserDefaults!
    private var pendingArrivals: PendingArrivalStore!

    override func setUp() {
        super.setUp()
        let suite = "NotificationArrivalTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        pendingArrivals = PendingArrivalStore(defaults: defaults)
    }

    private func makeManager() -> NotificationManager {
        NotificationManager(pendingArrivals: pendingArrivals)
    }

    private func pending(
        scheduleId: String = "sched", fireId: String = "fire",
        profileId: UUID = UUID(), sessionId: String? = "sess",
        displayName: String = "Weather updates", fullText: String = "the full reply",
        completedAt: Date = Date()
    ) -> PendingArrival {
        PendingArrival(
            scheduleId: scheduleId, fireId: fireId, profileId: profileId,
            sessionId: sessionId, displayName: displayName, fullText: fullText,
            completedAt: completedAt
        )
    }

    // MARK: - parseArrival

    func testParseArrivalResolvesFullTextAndDisplayNameFromStore() throws {
        let profileId = UUID()
        pendingArrivals.append(pending(
            scheduleId: "sched", fireId: "f1", profileId: profileId,
            sessionId: "sess-1", displayName: "Weather updates", fullText: "It's sunny."
        ))
        let manager = makeManager()

        let arrival = try XCTUnwrap(manager.parseArrival(
            userInfo: [
                "schedule_id": "sched", "fire_id": "f1", "profile_id": profileId.uuidString,
                "session_id": "sess-1",
            ],
            body: "Schedule ran — tap to hear the result",
            wasForeground: true
        ))

        XCTAssertEqual(arrival.scheduleId, "sched")
        XCTAssertEqual(arrival.fireId, "f1")
        XCTAssertEqual(arrival.profileId, profileId)
        XCTAssertEqual(arrival.sessionId, "sess-1")
        XCTAssertEqual(arrival.displayName, "Weather updates")
        XCTAssertEqual(arrival.fullText, "It's sunny.")
    }

    func testParseArrivalEmptySessionIdMeansNotResumable() throws {
        let manager = makeManager()
        let arrival = try XCTUnwrap(manager.parseArrival(
            userInfo: ["schedule_id": "sched", "fire_id": "f1", "session_id": ""],
            body: "generic body", wasForeground: false
        ))
        XCTAssertEqual(arrival.sessionId, "", "an empty string, not a missing key, means not resumable")
    }

    func testParseArrivalMissingFromStoreFireIdStillProducesAnArrival() throws {
        // Nothing appended to the store: the fire isn't there (already
        // consumed, trimmed past maxCount, or lost to a race) — must still
        // produce an arrival rather than dropping the notification.
        let manager = makeManager()
        let arrival = try XCTUnwrap(manager.parseArrival(
            userInfo: ["schedule_id": "sched", "fire_id": "gone", "session_id": "sess"],
            body: "generic body", wasForeground: true
        ))
        XCTAssertEqual(arrival.fireId, "gone")
        XCTAssertNil(arrival.fullText, "a fireId no longer in the store must resolve to nil, not crash or drop")
        XCTAssertNil(arrival.displayName)
    }

    func testParseArrivalMissingScheduleIdReturnsNil() {
        let manager = makeManager()
        let arrival = manager.parseArrival(
            userInfo: ["fire_id": "f1", "session_id": "sess"],
            body: "generic body", wasForeground: true
        )
        XCTAssertNil(arrival)
    }

    func testParseArrivalInvalidProfileIdParsesToNilRatherThanCrashing() {
        let manager = makeManager()
        let arrival = manager.parseArrival(
            userInfo: ["schedule_id": "sched", "fire_id": "f1", "profile_id": "not-a-uuid"],
            body: "generic body", wasForeground: true
        )
        XCTAssertNotNil(arrival)
        XCTAssertNil(arrival?.profileId)
    }

    // MARK: - ArrivalBadgeDecision (pure same/cross-profile/unavailable routing)

    func testDecisionSameProfileResumable() {
        let profile = BackendProfile(name: "A", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: profile.id, sessionId: "sess", activeProfileId: profile.id, profiles: [profile]
        )
        XCTAssertEqual(decision, .sameProfileResumable)
    }

    func testDecisionSameProfileNotResumable() {
        let profile = BackendProfile(name: "A", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: profile.id, sessionId: "", activeProfileId: profile.id, profiles: [profile]
        )
        XCTAssertEqual(decision, .sameProfileResult)
    }

    func testDecisionCrossProfileResumable() {
        let active = BackendProfile(name: "Active", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let origin = BackendProfile(name: "Origin", url: "https://b", authToken: "t2", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: origin.id, sessionId: "sess", activeProfileId: active.id, profiles: [active, origin]
        )
        XCTAssertEqual(decision, .crossProfile(originName: "Origin", resumable: true))
    }

    func testDecisionCrossProfileNotResumable() {
        let active = BackendProfile(name: "Active", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let origin = BackendProfile(name: "Origin", url: "https://b", authToken: "t2", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: origin.id, sessionId: "", activeProfileId: active.id, profiles: [active, origin]
        )
        XCTAssertEqual(decision, .crossProfile(originName: "Origin", resumable: false))
    }

    func testDecisionUnavailableWhenOriginProfileNoLongerExists() {
        let active = BackendProfile(name: "Active", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: UUID(), sessionId: "sess", activeProfileId: active.id, profiles: [active]
        )
        XCTAssertEqual(decision, .unavailable)
    }

    func testDecisionUnavailableWhenProfileIdIsNil() {
        let active = BackendProfile(name: "Active", url: "https://a", authToken: "t", selectedHarness: "hermes")
        let decision = NotificationManager.ArrivalBadgeDecision.decide(
            profileId: nil, sessionId: "sess", activeProfileId: active.id, profiles: [active]
        )
        XCTAssertEqual(decision, .unavailable)
    }

    // MARK: - Reconstruction (app-icon-launch survival)

    func testReconstructArrivalIfNeededNoOpsWhenStoreIsEmpty() {
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()
        XCTAssertNil(manager.lastScheduledArrival)
    }

    func testReconstructArrivalIfNeededPicksTheNewestPendingArrival() {
        pendingArrivals.append(pending(scheduleId: "sched-1", fireId: "f1", displayName: "Oldest"))
        pendingArrivals.append(pending(scheduleId: "sched-2", fireId: "f2", displayName: "Newest"))
        let manager = makeManager()

        manager.reconstructArrivalIfNeeded()

        XCTAssertEqual(manager.lastScheduledArrival?.scheduleId, "sched-2")
        XCTAssertEqual(manager.lastScheduledArrival?.displayName, "Newest")
    }

    func testReconstructArrivalIfNeededDoesNotOverwriteAnAlreadyDisplayedArrival() {
        pendingArrivals.append(pending(scheduleId: "sched-1", fireId: "f1"))
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()
        XCTAssertEqual(manager.lastScheduledArrival?.scheduleId, "sched-1")

        // A newer arrival lands, but the badge already shows something —
        // reconstruction must not silently swap it out from under the user.
        pendingArrivals.append(pending(scheduleId: "sched-2", fireId: "f2"))
        manager.reconstructArrivalIfNeeded()

        XCTAssertEqual(manager.lastScheduledArrival?.scheduleId, "sched-1")
    }

    func testReconstructedArrivalTreatsNilStoredSessionIdAsEmptyString() {
        pendingArrivals.append(pending(sessionId: nil))
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()
        XCTAssertEqual(manager.lastScheduledArrival?.sessionId, "")
    }

    // MARK: - Consumption

    func testConsumeArrivalRemovesItFromTheStore() throws {
        pendingArrivals.append(pending(scheduleId: "sched", fireId: "f1"))
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()
        let arrival = try XCTUnwrap(manager.lastScheduledArrival)

        manager.consumeArrival(arrival)

        XCTAssertTrue(pendingArrivals.all().isEmpty, "consuming must remove the arrival from the store")
    }

    func testConsumeArrivalClearsTheBadgeWhenItIsTheOneDisplayed() throws {
        pendingArrivals.append(pending(scheduleId: "sched", fireId: "f1"))
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()
        let arrival = try XCTUnwrap(manager.lastScheduledArrival)

        manager.consumeArrival(arrival)

        XCTAssertNil(manager.lastScheduledArrival)
    }

    func testConsumeArrivalLeavesADifferentlyDisplayedBadgeAlone() {
        pendingArrivals.append(pending(scheduleId: "displayed", fireId: "f1"))
        pendingArrivals.append(pending(scheduleId: "other", fireId: "f2"))
        let manager = makeManager()
        manager.reconstructArrivalIfNeeded()   // shows "other" (the newest)
        XCTAssertEqual(manager.lastScheduledArrival?.scheduleId, "other")

        let notDisplayed = NotificationManager.ScheduledArrival(
            scheduleId: "displayed", fireId: "f1", profileId: nil, sessionId: "",
            displayName: nil, fullText: nil, body: "", receivedAt: Date(), wasForeground: false
        )
        manager.consumeArrival(notDisplayed)

        XCTAssertEqual(manager.lastScheduledArrival?.scheduleId, "other",
                       "consuming a non-displayed arrival must not clear the badge")
        XCTAssertTrue(pendingArrivals.all().contains { $0.scheduleId == "other" })
        XCTAssertFalse(pendingArrivals.all().contains { $0.scheduleId == "displayed" })
    }
}
