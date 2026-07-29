import XCTest
@testable import HermesVoice

/// `FireSyncCoordinator` wires `BGTaskScheduler` and `scenePhase`, neither of
/// which is meaningfully unit-testable (Apple explicitly doesn't provide a
/// way to drive a real `BGAppRefreshTask` launch/expiration cycle from an
/// XCTest logic test). These tests cover exactly the two slices that ARE
/// pure and testable: the plist/Swift identifier stays in sync, and the
/// "skip when notifications disabled" decision.
final class FireSyncCoordinatorTests: XCTestCase {
    /// `HermesVoiceTests` is a HOSTED unit test bundle (required to
    /// `@testable import` an application target), so `Bundle.main` here
    /// resolves to the running HermesVoice.app process — its real, merged
    /// Info.plist, generated from `project.yml`. This is what actually
    /// catches a drift between the Swift constant and the plist entry:
    /// `BGTaskScheduler.register` throws at launch if the identifier isn't
    /// declared here.
    func testBackgroundTaskIdentifierMatchesInfoPlist() throws {
        let permitted = Bundle.main.object(
            forInfoDictionaryKey: "BGTaskSchedulerPermittedIdentifiers"
        ) as? [String]
        let identifiers = try XCTUnwrap(
            permitted, "Info.plist must declare BGTaskSchedulerPermittedIdentifiers"
        )
        XCTAssertTrue(
            identifiers.contains(FireSyncCoordinator.backgroundTaskIdentifier),
            "the identifier registered in Swift (FireSyncCoordinator.backgroundTaskIdentifier) "
                + "must be declared in project.yml's BGTaskSchedulerPermittedIdentifiers, or "
                + "BGTaskScheduler.register throws at launch"
        )
    }

    func testSkipsBackgroundSyncWhenNotificationsDisabled() {
        XCTAssertTrue(FireSyncCoordinator.shouldSkipBackgroundSync(notificationsEnabled: false))
    }

    func testDoesNotSkipBackgroundSyncWhenNotificationsEnabled() {
        XCTAssertFalse(FireSyncCoordinator.shouldSkipBackgroundSync(notificationsEnabled: true))
    }
}
