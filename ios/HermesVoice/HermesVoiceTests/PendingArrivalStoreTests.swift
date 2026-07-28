import XCTest
@testable import HermesVoice

/// `PendingArrivalStore` persists posted arrivals independently of
/// `FireMarkerStore`'s poll cursor, so a background-posted notification the
/// user never taps still reconstructs on an ordinary app-icon launch. Covered
/// here in isolation from `FireSyncService` (see `FireSyncServiceTests`).
final class PendingArrivalStoreTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        let suite = "PendingArrivalStoreTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
    }

    private func arrival(
        scheduleId: String = "sched", fireId: String = "fire",
        sessionId: String? = "sess", fullText: String = "the full reply text"
    ) -> PendingArrival {
        PendingArrival(
            scheduleId: scheduleId, fireId: fireId, profileId: UUID(),
            sessionId: sessionId, displayName: "Weather updates",
            fullText: fullText, completedAt: Date()
        )
    }

    func testAllIsEmptyBeforeAnyAppend() {
        let store = PendingArrivalStore(defaults: defaults)
        XCTAssertTrue(store.all().isEmpty)
    }

    func testAppendThenAllRoundTripsFullText() {
        let store = PendingArrivalStore(defaults: defaults)
        store.append(arrival(fullText: "the complete synthesized reply, not the generic body"))

        let all = store.all()
        XCTAssertEqual(all.count, 1)
        XCTAssertEqual(all[0].fullText, "the complete synthesized reply, not the generic body")
    }

    func testAppendPreservesOptionalSessionId() {
        let store = PendingArrivalStore(defaults: defaults)
        store.append(arrival(sessionId: nil))

        XCTAssertNil(store.all()[0].sessionId)
    }

    func testAppendIsBoundedToNewestEntries() {
        let store = PendingArrivalStore(defaults: defaults)
        for i in 0..<25 {
            store.append(arrival(scheduleId: "sched-\(i)", fireId: "fire-\(i)"))
        }

        let all = store.all()
        XCTAssertLessThanOrEqual(all.count, 20, "must stay bounded rather than growing forever")
        XCTAssertEqual(all.last?.scheduleId, "sched-24", "the newest entry must survive trimming")
        XCTAssertFalse(all.contains { $0.scheduleId == "sched-0" }, "the oldest entries must be trimmed first")
    }

    func testRemoveDropsOnlyTheGivenIds() {
        let store = PendingArrivalStore(defaults: defaults)
        let a = arrival(scheduleId: "a", fireId: "1")
        let b = arrival(scheduleId: "b", fireId: "1")
        store.append(a)
        store.append(b)

        store.remove(ids: [a.id])

        let remaining = store.all()
        XCTAssertEqual(remaining.count, 1)
        XCTAssertEqual(remaining.first?.id, b.id)
    }

    func testClearRemovesEverything() {
        let store = PendingArrivalStore(defaults: defaults)
        store.append(arrival())
        store.clear()
        XCTAssertTrue(store.all().isEmpty)
    }

    /// State lives in UserDefaults, not the struct instance.
    func testArrivalsPersistAcrossStoreInstancesOverTheSameDefaults() {
        PendingArrivalStore(defaults: defaults).append(arrival(scheduleId: "persisted"))
        let reopened = PendingArrivalStore(defaults: defaults)
        XCTAssertEqual(reopened.all().first?.scheduleId, "persisted")
    }
}
