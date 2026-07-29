import SwiftUI
import UIKit

/// Bridge UIApplicationDelegate hooks into our SwiftUI lifecycle so we can
/// receive APNs token data. SwiftUI alone doesn't surface
/// `didRegisterForRemoteNotificationsWithDeviceToken:`.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions:
            [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Must happen before this method returns, or BGTaskScheduler throws.
        // See `FireSyncCoordinator.registerBackgroundTask` for why this has
        // no ordering dependency on `HermesVoiceApp.init()`'s
        // `configure(settings:)`.
        FireSyncCoordinator.shared.registerBackgroundTask()
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            NotificationManager.shared.handleAPNsToken(deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        Task { @MainActor in
            NotificationManager.shared.handleAPNsRegistrationFailure(error)
        }
    }
}
