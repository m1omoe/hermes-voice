import XCTest
@testable import HermesVoice

/// `FireMarkerStore` is the per-profile cursor side table `FirePoller` reads
/// and writes through — covered here in isolation from the poller so cursor
/// read/write/prune semantics are pinned down independently of the actor's
/// concurrency behavior (see `FirePollerTests`).
final class FireMarkerStoreTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        let suite = "FireMarkerStoreTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
    }

    func testCursorIsNilBeforeFirstCommit() {
        let store = FireMarkerStore(defaults: defaults)
        XCTAssertNil(store.cursor(for: UUID()))
    }

    func testCommitThenReadRoundTrips() {
        let store = FireMarkerStore(defaults: defaults)
        let id = UUID()
        store.commit(42, for: id)
        XCTAssertEqual(store.cursor(for: id), 42)
    }

    func testCommitOverwritesExistingCursor() {
        let store = FireMarkerStore(defaults: defaults)
        let id = UUID()
        store.commit(1, for: id)
        store.commit(5, for: id)
        XCTAssertEqual(store.cursor(for: id), 5)
    }

    func testCursorsAreIsolatedPerProfile() {
        let store = FireMarkerStore(defaults: defaults)
        let a = UUID()
        let b = UUID()
        store.commit(10, for: a)
        store.commit(20, for: b)
        XCTAssertEqual(store.cursor(for: a), 10)
        XCTAssertEqual(store.cursor(for: b), 20)
    }

    /// Cursor persistence must survive a fresh `FireMarkerStore` value —
    /// state lives in `UserDefaults`, not the struct instance.
    func testCursorPersistsAcrossStoreInstancesOverTheSameDefaults() {
        let id = UUID()
        FireMarkerStore(defaults: defaults).commit(7, for: id)
        let reopened = FireMarkerStore(defaults: defaults)
        XCTAssertEqual(reopened.cursor(for: id), 7)
    }

    func testPruneRemovesOnlyTheGivenProfilesCursor() {
        let store = FireMarkerStore(defaults: defaults)
        let a = UUID()
        let b = UUID()
        store.commit(10, for: a)
        store.commit(20, for: b)

        store.prune(profileId: a)

        XCTAssertNil(store.cursor(for: a))
        XCTAssertEqual(store.cursor(for: b), 20, "pruning one profile must not disturb another's cursor")
    }

    /// Pruning a profile with no cursor yet is a harmless no-op.
    func testPruneUnknownProfileIsANoOp() {
        let store = FireMarkerStore(defaults: defaults)
        let known = UUID()
        store.commit(1, for: known)

        store.prune(profileId: UUID())

        XCTAssertEqual(store.cursor(for: known), 1)
    }

    func testPruneMissingRemovesCursorsForVanishedProfiles() {
        let store = FireMarkerStore(defaults: defaults)
        let a = UUID()
        let b = UUID()
        let c = UUID()
        store.commit(1, for: a)
        store.commit(2, for: b)
        store.commit(3, for: c)

        // Only `b` still exists (e.g. `a` and `c` were removed from
        // AppSettings.backendProfiles while this store held stale entries).
        store.pruneMissing(existingIds: [b])

        XCTAssertNil(store.cursor(for: a))
        XCTAssertEqual(store.cursor(for: b), 2)
        XCTAssertNil(store.cursor(for: c))
    }

    func testPruneMissingWithEmptyExistingIdsClearsEverything() {
        let store = FireMarkerStore(defaults: defaults)
        let a = UUID()
        store.commit(1, for: a)

        store.pruneMissing(existingIds: [])

        XCTAssertNil(store.cursor(for: a))
    }
}
