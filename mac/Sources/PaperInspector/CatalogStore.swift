import Foundation
import Observation

/// The engine's plain-language explanation of every check, fetched once per engine start and kept in memory.
@MainActor @Observable
final class CatalogStore {
    static let shared = CatalogStore()

    private(set) var checks: [String: CheckInfo] = [:]
    @ObservationIgnored private var loadedFrom: URL?
    @ObservationIgnored private var loading: Task<Void, Never>?
    @ObservationIgnored private var asked: Set<String> = []

    func info(_ check: String) -> CheckInfo? { checks[check] }

    /// Loads the whole catalogue, then any check it doesn't list (the engine answers those with a generic entry).
    func ensure(_ check: String) async {
        let client = EngineController.shared.client
        if loadedFrom != client.base {
            if loading == nil {
                loading = Task {
                    if let all = try? await client.catalog() {
                        var map: [String: CheckInfo] = [:]
                        for c in all { map[c.id] = c }
                        checks = map
                        loadedFrom = client.base
                        asked = []
                    }
                    loading = nil
                }
            }
            await loading?.value
        }
        guard checks[check] == nil, !check.isEmpty, !asked.contains(check) else { return }
        asked.insert(check)
        if let one = try? await client.explain(check) { checks[check] = one }
    }

    /// For tests.
    func store(_ info: CheckInfo) { checks[info.id] = info }
}
