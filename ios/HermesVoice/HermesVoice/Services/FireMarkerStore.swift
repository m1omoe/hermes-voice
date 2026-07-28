import Foundation

/// Per-profile cursor persistence for the schedule-fire sync feed
/// (`FirePoller`). One `seq` cursor per profile — NOT nested per-schedule:
/// per-profile priming on first contact records the highest `seq` seen across
/// every schedule at once, whereas per-schedule priming would silently
/// swallow the first fire of a schedule created after that first poll (see
/// local-notifications-spec's "Cursor correctness").
///
/// Deliberately a side table under its own `hv.fireMarkers` key, NOT a field
/// on `BackendProfile`: adding it there would force a Codable migration of
/// the persisted profiles blob, and would let the poller's background cursor
/// writes race `AppSettings`'s MainActor `didSet` persistence of that same
/// blob.
struct FireMarkerStore {
    private static let storageKey = "hv.fireMarkers"

    /// The UserDefaults suite backing persistence. Injected (default
    /// `.standard`) so tests never touch real defaults.
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// The highest `seq` already delivered (or primed) for `profileId`, or
    /// nil when this profile has never been seen — the "first contact"
    /// signal `FirePoller` uses to decide whether to prime silently.
    func cursor(for profileId: UUID) -> Int? {
        readAll()[profileId.uuidString]
    }

    /// Records `seq` as the new cursor for `profileId`, overwriting any
    /// existing value. Callers must only do this after accepting the events
    /// up through `seq` (or, for priming, immediately — there's nothing to
    /// accept).
    func commit(_ seq: Int, for profileId: UUID) {
        var all = readAll()
        all[profileId.uuidString] = seq
        writeAll(all)
    }

    /// Removes a single profile's cursor — e.g. when the user deletes that
    /// profile.
    func prune(profileId: UUID) {
        var all = readAll()
        all.removeValue(forKey: profileId.uuidString)
        writeAll(all)
    }

    /// Removes every cursor whose profile id is not in `existingIds` —
    /// sweeps up entries left behind by profiles that vanished (e.g. removed
    /// while a background poll held a stale snapshot).
    func pruneMissing(existingIds: Set<UUID>) {
        let keep = Set(existingIds.map(\.uuidString))
        writeAll(readAll().filter { keep.contains($0.key) })
    }

    private func readAll() -> [String: Int] {
        guard let data = defaults.data(forKey: Self.storageKey),
              let decoded = try? JSONDecoder().decode([String: Int].self, from: data)
        else { return [:] }
        return decoded
    }

    private func writeAll(_ markers: [String: Int]) {
        guard let data = try? JSONEncoder().encode(markers) else { return }
        defaults.set(data, forKey: Self.storageKey)
    }
}
