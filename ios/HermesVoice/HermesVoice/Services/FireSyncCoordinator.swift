import BackgroundTasks
import Foundation

/// Wires `FirePoller` / `FireSyncService` into the app's two entry points —
/// `BGAppRefreshTask` (background) and `scenePhase == .active` plus a light
/// timer (foreground) — behind a SINGLE shared `FireSyncService` instance, so
/// the two entry points never race on `FireMarkerStore`'s cursors
/// (local-notifications-spec, "Polling").
///
/// Configured once from `HermesVoiceApp.init()` (mirrors `NotificationManager
/// .configure(settings:)`); its background launch handler is registered from
/// `AppDelegate.didFinishLaunchingWithOptions`. Both reach the same
/// singleton, so the one-shared-instance guarantee holds regardless of which
/// entry point runs first.
@MainActor
final class FireSyncCoordinator {
    static let shared = FireSyncCoordinator()

    /// MUST match the identifier declared in `project.yml`'s
    /// `BGTaskSchedulerPermittedIdentifiers` — this constant is the single
    /// place that string is spelled out in Swift; both `registerBackgroundTask`
    /// and `submitBackgroundTaskRequest` reference it rather than repeating
    /// the literal. `FireSyncCoordinatorTests
    /// .testBackgroundTaskIdentifierMatchesInfoPlist` checks the two agree.
    /// `nonisolated` — it's a plain constant, and a plist-consistency test
    /// must be able to read it synchronously without hopping to MainActor.
    nonisolated static let backgroundTaskIdentifier = "dev.finklea.harnessvoice.firesync"

    /// ~30s is the empirical `BGAppRefreshTask` execution budget. This stays
    /// comfortably under it. `FirePoller` already applies a ~10s PER-REQUEST
    /// timeout, but that alone doesn't bound the whole background window —
    /// notification posts for surviving profiles run sequentially inside
    /// `FireSyncService.sync`, and first-contact priming walks every page of
    /// a backlog — so a top-level deadline is still needed.
    static let backgroundSyncDeadlineSeconds: UInt64 = 25

    /// "Light repeating timer" while foreground, per the spec — frequent
    /// enough that a second Mac's schedule feels responsive with the app
    /// open, without hammering the backend.
    static let foregroundPollIntervalSeconds: UInt64 = 45

    private weak var settings: AppSettings?
    private var service: FireSyncService?
    private var foregroundLoopTask: Task<Void, Never>?

    private init() {}

    /// Called once from `HermesVoiceApp.init()`. Builds the ONE shared
    /// `FireSyncService` (with its `FirePoller`/stores) that both the
    /// background handler and the foreground loop below use — never two
    /// separate instances, which would race on `FireMarkerStore`'s cursors.
    func configure(settings: AppSettings) {
        self.settings = settings
        let markerStore = FireMarkerStore(defaults: settings.defaults)
        let pendingArrivals = PendingArrivalStore(defaults: settings.defaults)
        let poller = FirePoller(markerStore: markerStore)
        self.service = FireSyncService(
            poller: poller,
            pendingArrivals: pendingArrivals,
            currentProfile: { [weak settings] id in
                settings?.backendProfiles.first(where: { $0.id == id })
            }
        )
    }

    // MARK: - Background entry point

    /// Registers the `BGAppRefreshTask` launch handler. Called from
    /// `AppDelegate.didFinishLaunchingWithOptions` — MUST run before that
    /// method returns, or iOS throws at registration. The handler itself only
    /// reaches for `FireSyncCoordinator.shared`'s configured state once the OS
    /// actually invokes it, and iOS only ever does that against a
    /// fully-launched app process (i.e. after `configure(settings:)` has
    /// already run) — so there's no ordering dependency between this call and
    /// `configure`.
    func registerBackgroundTask() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.backgroundTaskIdentifier, using: nil
        ) { task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            Task { @MainActor in
                FireSyncCoordinator.shared.handleBackgroundTask(refreshTask)
            }
        }
    }

    /// Pure decision extracted for testability (`FireSyncCoordinatorTests`):
    /// whether a background invocation should skip sync work entirely. A
    /// skip is not a failure — the task still completes successfully.
    /// `nonisolated` so the test can call it synchronously, with no
    /// dependency on `FireSyncCoordinator`'s actor-isolated state.
    nonisolated static func shouldSkipBackgroundSync(notificationsEnabled: Bool) -> Bool {
        !notificationsEnabled
    }

    /// The `BGAppRefreshTask` launch handler body.
    func handleBackgroundTask(_ task: BGAppRefreshTask) {
        // Resubmit the NEXT request FIRST: a crash or early return below must
        // not silently end the schedule forever.
        submitBackgroundTaskRequest()

        guard let settings, let service,
              !Self.shouldSkipBackgroundSync(notificationsEnabled: settings.notificationsEnabled)
        else {
            task.setTaskCompleted(success: true)
            return
        }

        let completion = BackgroundTaskCompletion(task)
        let profiles = settings.backendProfiles
        let workTask = Task {
            await self.performBoundedSync(service: service, profiles: profiles)
        }

        // The expiration handler ONLY cancels the in-flight work — cancelling
        // `workTask` propagates into `service.sync`'s awaited network calls,
        // which unwind promptly, so the single completion path below (after
        // `await workTask.value`) still runs quickly. It must NEVER also call
        // `setTaskCompleted` itself: that would race the completion below.
        // Exactly one path is allowed to complete the task, enforced by
        // `BackgroundTaskCompletion`'s single-use guard.
        task.expirationHandler = {
            workTask.cancel()
        }

        Task {
            let finishedCleanly = await workTask.value
            completion.complete(success: finishedCleanly)
        }
    }

    /// Races `service.sync(profiles:)` against the top-level deadline above.
    /// Returns whether the sync finished on its own (true) or was cut short
    /// by the deadline (false) — a rough success signal for
    /// `setTaskCompleted`, on top of the expiration-handler cancellation path.
    private func performBoundedSync(
        service: FireSyncService, profiles: [BackendProfile]
    ) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask {
                _ = await service.sync(profiles: profiles)
                return true
            }
            group.addTask {
                try? await Task.sleep(
                    nanoseconds: Self.backgroundSyncDeadlineSeconds * 1_000_000_000
                )
                return false
            }
            let finishedCleanly = await group.next() ?? false
            group.cancelAll()
            return finishedCleanly
        }
    }

    /// Schedules the next `BGAppRefreshTask`. Called at the top of every
    /// background invocation (resubmit-first), when the app backgrounds, and
    /// after the user enables notifications. No `earliestBeginDate` is set —
    /// per the spec, this is explicitly NOT a delivery guarantee, and iOS
    /// treats that field as an unenforced lower bound either way.
    func submitBackgroundTaskRequest() {
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundTaskIdentifier)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // Best-effort — mirrors NotificationManager's silent-failure
            // logging for background scheduling, which the user never sees.
            print("FireSyncCoordinator: BGAppRefreshTaskRequest submit failed: \(error)")
        }
    }

    // MARK: - Foreground entry point

    /// Starts an immediate sync plus a light repeating timer while the app is
    /// foreground. ALWAYS polls every profile (never just the active one):
    /// `BGAppRefreshTask` never runs while foregrounded, so active-only would
    /// make a second Mac's arrivals invisible for as long as the app stays
    /// open (local-notifications-spec, "Foreground").
    func appDidBecomeActive() {
        guard foregroundLoopTask == nil else { return }
        foregroundLoopTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.syncNow()
                guard !Task.isCancelled else { break }
                try? await Task.sleep(
                    nanoseconds: Self.foregroundPollIntervalSeconds * 1_000_000_000
                )
            }
        }
    }

    /// Cancels the foreground timer AND submits a background request, so the
    /// schedule keeps running while the app isn't open.
    func appDidEnterBackground() {
        stopForegroundPolling()
        submitBackgroundTaskRequest()
    }

    /// Cancels the foreground timer alone. Used for `.active` -> `.inactive`
    /// too — leaving `.active` at all stops the timer — without submitting a
    /// background request for a merely transient state like the app switcher.
    func stopForegroundPolling() {
        foregroundLoopTask?.cancel()
        foregroundLoopTask = nil
    }

    private func syncNow() async {
        guard let settings, let service else { return }
        _ = await service.sync(profiles: settings.backendProfiles)
    }
}

/// Guarantees `BGAppRefreshTask.setTaskCompleted` is called AT MOST once.
/// `BGTask.expirationHandler` is not documented to run on any particular
/// queue, so this uses a lock rather than relying on `FireSyncCoordinator`'s
/// `@MainActor` isolation.
private final class BackgroundTaskCompletion: @unchecked Sendable {
    private let task: BGAppRefreshTask
    private let lock = NSLock()
    private var completed = false

    init(_ task: BGAppRefreshTask) { self.task = task }

    func complete(success: Bool) {
        lock.lock()
        defer { lock.unlock() }
        guard !completed else { return }
        completed = true
        task.setTaskCompleted(success: success)
    }
}
