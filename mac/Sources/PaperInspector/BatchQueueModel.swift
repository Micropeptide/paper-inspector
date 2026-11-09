import AppKit
import Foundation
import Observation
import UserNotifications

/// The engine's persistent batch queue: many DOIs, scanned one at a time, even across restarts of the app.  The app
/// only mirrors it, polling quickly while something is waiting or running and slowly otherwise.
@MainActor @Observable
final class BatchQueueModel {
    static let shared = BatchQueueModel()

    private(set) var items: [QueueItem] = []
    private(set) var counts: [String: Int] = [:]
    private(set) var batches: [QueueBatch] = []
    private(set) var error: AppError?
    /// Whether the engine has a queue at all (older engines answer 404).
    private(set) var available = true
    @ObservationIgnored private var poller: Task<Void, Never>?
    @ObservationIgnored private var seenDone: Set<String> = []
    @ObservationIgnored private var primed = false
    @ObservationIgnored private var wasActive = false

    var active: Int { (counts["queued"] ?? 0) + (counts["running"] ?? 0) }
    var hasActive: Bool { active > 0 }

    /// Items in sidebar order: running, waiting, failed, cancelled, then done (latest first).
    var sorted: [QueueItem] {
        items.enumerated().sorted { a, b in
            let (x, y) = (a.element, b.element)
            if x.status.rank != y.status.rank { return x.status.rank < y.status.rank }
            if x.status == .done { return (x.finished ?? "") > (y.finished ?? "") }
            return a.offset < b.offset
        }.map(\.element)
    }

    var doneCount: Int { counts["done"] ?? 0 }
    var finishedCount: Int { (counts["done"] ?? 0) + (counts["error"] ?? 0) + (counts["cancelled"] ?? 0) }

    func batch(_ id: String) -> QueueBatch? { batches.first { $0.id == id } }

    // MARK: polling

    func startPolling() {
        guard poller == nil else { return }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if EngineController.shared.isReady { await self.refresh() }
                let fast = self.hasActive
                try? await Task.sleep(nanoseconds: fast ? 1_500_000_000 : 8_000_000_000)
            }
        }
    }

    func refresh() async {
        do {
            let list = try await EngineController.shared.client.queue()
            apply(list)
            available = true
            error = nil
        } catch let e as AppError where e.status == 404 {
            available = false
        } catch {
            self.error = AppError.from(error)
        }
    }

    func apply(_ list: QueueList) {
        items = list.items
        counts = list.counts
        batches = list.batches
        let done = Set(list.items.filter { $0.status == .done }.map(\.id))
        let fresh = done.subtracting(seenDone)
        seenDone = done
        if primed, !fresh.isEmpty { Task { await EngineController.shared.refreshRuns() } }
        if primed, wasActive, !hasActive { notifyFinished() }
        wasActive = hasActive
        primed = true
        ScanQueue.shared.refreshDock()
    }

    // MARK: actions

    /// Sends pasted text (DOIs, PubMed or arXiv ids, links; one per line) to the queue.
    func add(text: String, name: String, online: Bool) async throws -> QueueAddResult {
        let result = try await EngineController.shared.client.queueAdd(text: text, name: name, online: online)
        for d in result.added.compactMap(\.doi) { RecentDOIs.add(d) }
        await refresh()
        return result
    }

    func cancel(_ item: QueueItem) { act { try await $0.queueCancel(item.id) } }
    func retry(_ item: QueueItem) { act { try await $0.queueRetry(item.id) } }
    func remove(_ item: QueueItem) { act { try await $0.queueRemove(item.id) } }
    func clearFinished() { act { try await $0.queueClear() } }

    func cancelWaiting() {
        let waiting = items.filter { $0.status == .queued }
        act { c in for w in waiting { _ = try await c.queueCancel(w.id) } }
    }

    private func act(_ op: @escaping (EngineClient) async throws -> Void) {
        Task {
            do { try await op(EngineController.shared.client) } catch {
                ScanQueue.shared.present(AppError(title: "The queue didn’t change", message: AppError.from(error).message))
            }
            await refresh()
        }
    }

    private func notifyFinished() {
        guard let app = NSApp, !app.isActive, UserDefaults.standard.object(forKey: "notifyOnCompletion") as? Bool ?? true,
              Bundle.main.bundleURL.pathExtension == "app" else { return }
        let done = counts["done"] ?? 0
        let failed = counts["error"] ?? 0
        let body = "\(done) done" + (failed > 0 ? ", \(failed) failed" : "")
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "Batch finished"
            content.body = body
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "batch-" + UUID().uuidString,
                                                                         content: content, trigger: nil))
        }
    }
}
