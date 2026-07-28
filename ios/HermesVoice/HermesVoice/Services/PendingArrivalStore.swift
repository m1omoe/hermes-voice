import Foundation

/// One notification actually posted for a schedule fire — the full record,
/// not just the generic notification body. Needed because `NotificationManager
/// .lastScheduledArrival` (`NotificationManager.swift:328-385`) is populated
/// only by `willPresent` or a notification tap, so a notification posted in
/// the background that the user never taps — they just open the app from the
/// home screen — reconstructs nothing today. Persisting arrivals here,
/// independently of `FireMarkerStore`'s poll cursor, is what lets an ordinary
/// app-icon launch recover them (local-notifications-spec, "Arrivals must
/// survive an ordinary app-icon launch").
struct PendingArrival: Codable, Equatable, Identifiable {
    /// Matches the posted notification's identifier (`"\(scheduleId)-\(fireId)"`),
    /// so a consumer can reconcile a tapped/foreground notification with its
    /// full-text record.
    var id: String { "\(scheduleId)-\(fireId)" }
    let scheduleId: String
    let fireId: String
    let profileId: UUID
    /// Optional — a fire's `session_id` is legitimately null; never used as
    /// identity, only for an optional "resume" affordance.
    let sessionId: String?
    let displayName: String
    /// The full reply text. The posted notification's body is deliberately
    /// generic (never agent output) — this is what the arrival/auto-play
    /// path actually speaks.
    let fullText: String
    let completedAt: Date
}

/// Persists posted arrivals to UserDefaults under `hv.pendingArrivals`,
/// independent of `FireMarkerStore`'s per-profile cursor. Bounded to the
/// newest `maxCount` so it cannot grow forever.
///
/// A plain struct over an injectable `UserDefaults` suite, mirroring
/// `FireMarkerStore`'s shape: no in-memory state, so it's safe to construct
/// fresh wherever needed and reads always reflect the persisted truth.
struct PendingArrivalStore {
    private static let storageKey = "hv.pendingArrivals"
    private static let maxCount = 20

    /// The UserDefaults suite backing persistence. Injected (default
    /// `.standard`) so tests never touch real defaults.
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// Every persisted arrival, oldest first.
    func all() -> [PendingArrival] {
        readAll()
    }

    /// Appends one arrival, trimming the OLDEST entries first once the
    /// bound is exceeded — callers only ever care about the most recent
    /// arrivals.
    func append(_ arrival: PendingArrival) {
        var all = readAll()
        all.append(arrival)
        if all.count > Self.maxCount {
            all.removeFirst(all.count - Self.maxCount)
        }
        writeAll(all)
    }

    /// Removes exactly the arrivals identified in `ids` (e.g. once consumed
    /// by the arrival/auto-play path).
    func remove(ids: Set<String>) {
        writeAll(readAll().filter { !ids.contains($0.id) })
    }

    /// Drops every persisted arrival.
    func clear() {
        writeAll([])
    }

    private func readAll() -> [PendingArrival] {
        guard let data = defaults.data(forKey: Self.storageKey),
              let decoded = try? JSONDecoder().decode([PendingArrival].self, from: data)
        else { return [] }
        return decoded
    }

    private func writeAll(_ arrivals: [PendingArrival]) {
        guard let data = try? JSONEncoder().encode(arrivals) else { return }
        defaults.set(data, forKey: Self.storageKey)
    }
}
