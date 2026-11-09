import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers
import UserNotifications

/// Every scan and AI investigation in flight, app-wide: several can run at once, each can be cancelled or retried,
/// and windows learn when one finishes.
@MainActor @Observable
final class ScanQueue {
    static let shared = ScanQueue()

    enum Input: Equatable {
        case files(main: URL, supplements: [URL])
        case compare([URL])
        case doi(String)
        case demo
        case investigate(run: String)
    }

    struct Job: Identifiable {
        let id = UUID()
        var title: String
        var input: Input
        var stage = "Starting…"
        var progress = 0.0
        var engineID: String?
        var failure: AppError?
        var origin: UUID?
        var selectionAtStart: String?
        var started = Date()
        /// Asked to stop; the engine finishes the step in progress first.
        var cancelling = false
        var isRunning: Bool { failure == nil }
        var investigatedRun: String? { if case .investigate(let r) = input { return r }; return nil }
    }

    /// A finished job's run, for the window that started it (or any window when nil).
    struct Finished: Equatable {
        let token = UUID()
        var run: String
        var origin: UUID?
        var selectionAtStart: String?
    }

    private(set) var jobs: [Job] = []
    var finished: Finished?
    /// Bumped when a run's report changes (an AI investigation finished), so open views reload it.
    private(set) var revisions: [String: Int] = [:]
    @ObservationIgnored private var tasks: [UUID: Task<Void, Never>] = [:]

    var hasRunning: Bool { jobs.contains { $0.isRunning } }
    var scans: [Job] { jobs.filter { $0.investigatedRun == nil } }
    func investigation(for run: String) -> Job? { jobs.last { $0.investigatedRun == run } }

    // MARK: starting

    func start(_ input: Input, title: String, origin: UUID? = nil, selection: String? = nil) {
        var job = Job(title: title, input: input)
        job.origin = origin
        job.selectionAtStart = selection
        jobs.append(job)
        run(job.id)
    }

    func inspectDOI(_ raw: String, origin: UUID?, selection: String?) {
        let doi = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !doi.isEmpty else { return }
        RecentDOIs.add(doi)
        start(.doi(doi), title: doi, origin: origin, selection: selection)
    }

    /// Files from Open, drag and drop, Finder or the Dock.  Folders are searched; one PDF (or a figure image) is the
    /// paper and the rest are its supplementary files; several PDFs ask whether they are separate papers.
    func importFiles(_ urls: [URL], origin: UUID? = nil, selection: String? = nil) {
        let files = Self.expand(urls)
        guard !files.isEmpty else {
            present(AppError(title: "Nothing to inspect",
                             message: "Paper Inspector reads PDFs, images, spreadsheets, CSV, Word (.docx) files and zip archives."))
            return
        }
        let pdfs = files.filter { $0.pathExtension.lowercased() == "pdf" }
        let others = files.filter { $0.pathExtension.lowercased() != "pdf" }
        if pdfs.count > 1 {
            chooseImport(pdfs: pdfs, others: others, origin: origin, selection: selection)
            return
        }
        guard let main = pdfs.first ?? files.first(where: { Self.isImage($0) }) else {
            present(AppError(title: "Add the paper itself",
                             message: "Supplementary files can't be screened on their own. Drop them together with the paper's PDF."))
            return
        }
        start(.files(main: main, supplements: files.filter { $0 != main }), title: main.deletingPathExtension().lastPathComponent,
              origin: origin, selection: selection)
    }

    private func chooseImport(pdfs: [URL], others: [URL], origin: UUID?, selection: String?) {
        let remembered = UserDefaults.standard.string(forKey: "multiPDFDefault") ?? "ask"
        func single(_ main: URL) {
            start(.files(main: main, supplements: (pdfs + others).filter { $0 != main }),
                  title: main.deletingPathExtension().lastPathComponent, origin: origin, selection: selection)
        }
        func compare() {
            start(.compare(pdfs), title: "Case: \(pdfs.count) papers", origin: origin, selection: selection)
        }
        switch remembered {
        case "single": single(pdfs[0]); return
        case "compare": compare(); return
        default: break
        }
        let alert = NSAlert()
        alert.messageText = "How should these \(pdfs.count) PDFs be inspected?"
        alert.informativeText = "Choose One Paper if the other PDFs are its supplementary files. Choose Compare Papers "
            + "to screen separate papers and look for images reused between them."
        alert.addButton(withTitle: "One Paper")
        alert.addButton(withTitle: "Compare Papers")
        alert.addButton(withTitle: "Cancel")
        let popup = NSPopUpButton(frame: .zero, pullsDown: false)
        popup.addItems(withTitles: pdfs.map(\.lastPathComponent))
        let label = NSTextField(labelWithString: "Main paper:")
        let row = NSStackView(views: [label, popup])
        row.orientation = .horizontal
        row.frame = NSRect(x: 0, y: 0, width: 360, height: 26)
        alert.accessoryView = row
        alert.showsSuppressionButton = true
        alert.suppressionButton?.title = "Remember my choice"
        let handle: (NSApplication.ModalResponse) -> Void = { response in
            let remember = alert.suppressionButton?.state == .on
            switch response {
            case .alertFirstButtonReturn:
                if remember { UserDefaults.standard.set("single", forKey: "multiPDFDefault") }
                single(pdfs[max(0, popup.indexOfSelectedItem)])
            case .alertSecondButtonReturn:
                if remember { UserDefaults.standard.set("compare", forKey: "multiPDFDefault") }
                compare()
            default: break
            }
        }
        if let w = NSApp.keyWindow ?? NSApp.mainWindow {
            alert.beginSheetModal(for: w, completionHandler: handle)
        } else {
            handle(alert.runModal())
        }
    }

    func present(_ error: AppError) {
        let alert = NSAlert()
        alert.messageText = error.title
        alert.informativeText = error.message
        alert.alertStyle = .warning
        if let w = NSApp.keyWindow ?? NSApp.mainWindow { alert.beginSheetModal(for: w) } else { alert.runModal() }
    }

    // MARK: running

    /// Stops a job.  Before the engine has it (still uploading or waiting for the engine) it ends at once.  After,
    /// the engine is asked to stop and the row says "Cancelling…" until the scan reaches its next step; if the
    /// engine can't be asked, or takes more than a minute, the app stops waiting anyway.
    func cancel(_ id: UUID) {
        guard let job = jobs.first(where: { $0.id == id }), !job.cancelling else { return }
        guard let engineID = job.engineID, tasks[id] != nil else {
            drop(id)
            return
        }
        update(id) { $0.cancelling = true; $0.stage = "Cancelling…" }
        let client = EngineController.shared.client
        Task {
            do {
                let ej = try await client.cancelJob(engineID)
                if ej.status == .cancelled { drop(id) }
            } catch {
                drop(id)
                return
            }
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            if jobs.contains(where: { $0.id == id && $0.cancelling }) { drop(id) }
        }
    }

    /// Forgets a job and stops watching it.
    private func drop(_ id: UUID) {
        tasks.removeValue(forKey: id)?.cancel()
        jobs.removeAll { $0.id == id }
        updateDock()
    }

    /// Cancels without waiting for the engine (when the app quits).
    func cancelAllNow() {
        let client = EngineController.shared.client
        for j in jobs where j.isRunning {
            if let e = j.engineID { Task { _ = try? await client.cancelJob(e) } }
            drop(j.id)
        }
    }

    func dismiss(_ id: UUID) {
        jobs.removeAll { $0.id == id && !$0.isRunning }
    }

    func retry(_ id: UUID) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        let old = jobs.remove(at: i)
        start(old.input, title: old.title, origin: old.origin, selection: old.selectionAtStart)
    }

    func cancelAll() {
        for j in jobs where j.isRunning { cancel(j.id) }
    }

    var runningCount: Int { jobs.filter(\.isRunning).count }

    private func update(_ id: UUID, _ change: (inout Job) -> Void) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        change(&jobs[i])
    }

    private func run(_ id: UUID) {
        updateDock()
        tasks[id] = Task { [weak self] in
            guard let self, let job = self.jobs.first(where: { $0.id == id }) else { return }
            let uploads: Bool
            do {
                try await self.waitForEngine(id)
                let client = EngineController.shared.client
                var ej: EngineJob
                switch job.input {
                case .files(let main, let supp):
                    uploads = true
                    self.update(id) { $0.stage = "Uploading" }
                    ej = try await client.upload(main: main, others: supp, asCase: false) { p in
                        Task { @MainActor in self.update(id) { $0.progress = p * 0.1 } }
                    }
                case .compare(let files):
                    uploads = true
                    self.update(id) { $0.stage = "Uploading" }
                    ej = try await client.upload(main: nil, others: files, asCase: true) { p in
                        Task { @MainActor in self.update(id) { $0.progress = p * 0.1 } }
                    }
                case .doi(let doi):
                    uploads = false
                    ej = try await client.scanDOI(doi)
                case .demo:
                    uploads = false
                    ej = try await client.demo()
                case .investigate(let run):
                    uploads = false
                    ej = try await client.investigate(run)
                }
                self.update(id) { $0.engineID = ej.id }
                while ej.status == .running {
                    try await Task.sleep(nanoseconds: 400_000_000)
                    do {
                        ej = try await client.job(ej.id)
                    } catch let e as AppError where e.status == 404 {
                        throw AppError(title: "The scan was lost",
                                       message: "The engine restarted while it was working. Retry to start again.")
                    }
                    let current = ej
                    self.update(id) {
                        if !$0.cancelling { $0.stage = current.stage }
                        $0.progress = uploads ? 0.1 + 0.9 * current.progress : current.progress
                    }
                }
                if ej.status == .cancelled {
                    self.drop(id)
                    return
                }
                if ej.status != .done {
                    throw AppError(title: job.investigatedRun == nil ? "The scan stopped" : "The AI investigator stopped",
                                   message: ej.error ?? "The engine reported “\(ej.stage)”.",
                                   suggestsLog: true, suggestsSettings: job.investigatedRun != nil)
                }
                await self.complete(id, run: ej.run)
            } catch is CancellationError {
                return
            } catch {
                if Task.isCancelled { return }
                self.update(id) { $0.failure = AppError.from(error) }
                self.tasks[id] = nil
                self.updateDock()
            }
        }
    }

    /// Files opened from Finder can arrive before the engine has started.
    private func waitForEngine(_ id: UUID) async throws {
        let engine = EngineController.shared
        var ticks = 0
        while !engine.isReady {
            switch engine.state {
            case .failed(let e): throw e
            case .crashed where ticks > 20, .idle where ticks > 20:
                throw AppError(title: "The analysis engine isn't running", message: "Restart it from the sidebar, then retry.",
                               suggestsLog: true)
            default: break
            }
            if ticks == 0 { update(id) { $0.stage = "Waiting for the analysis engine…" } }
            if ticks > 240 { throw AppError(title: "The analysis engine didn't start", message: "Check the engine log, then retry.", suggestsLog: true) }
            try await Task.sleep(nanoseconds: 500_000_000)
            ticks += 1
        }
    }

    private func complete(_ id: UUID, run: String?) async {
        guard let job = jobs.first(where: { $0.id == id }) else { return }
        jobs.removeAll { $0.id == id }
        tasks[id] = nil
        updateDock()
        await EngineController.shared.refreshRuns()
        guard let run else { return }
        if job.investigatedRun != nil {
            revisions[run, default: 0] += 1
            ImageStore.shared.purge(run: run)
        } else {
            finished = Finished(run: run, origin: job.origin, selectionAtStart: job.selectionAtStart)
        }
        notify(job, run: run)
    }

    // MARK: feedback

    private func updateDock() { refreshDock() }

    /// The Dock badge counts this app's scans plus the papers in the batch queue.
    func refreshDock() {
        let n = jobs.filter(\.isRunning).count + BatchQueueModel.shared.active
        NSApp?.dockTile.badgeLabel = n > 0 ? "\(n)" : nil
    }

    private func notify(_ job: Job, run: String) {
        guard !NSApp.isActive, UserDefaults.standard.object(forKey: "notifyOnCompletion") as? Bool ?? true,
              Bundle.main.bundleURL.pathExtension == "app" else { return }
        let summary = EngineController.shared.runs.first { $0.id == run }
        let title = job.investigatedRun == nil ? "Scan finished" : "AI investigation finished"
        let body = summary.map { "\($0.title) – integrity score \($0.displayScore)" } ?? job.title
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.userInfo = ["run": run]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: run + "-" + UUID().uuidString, content: content, trigger: nil))
        }
    }

    // MARK: files

    nonisolated static let importExtensions: Set<String> = [
        "pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif",
        "xlsx", "xlsm", "xls", "csv", "tsv", "txt", "docx", "doc", "zip", "gz", "tgz",
    ]

    static var importTypes: [UTType] {
        [.pdf, .image, .spreadsheet, .commaSeparatedText, .tabSeparatedText, .plainText, .zip, .gzip, .folder]
            + ["docx", "doc", "xlsx", "xls", "xlsm", "tgz"].compactMap { UTType(filenameExtension: $0) }
    }

    nonisolated static func isImage(_ u: URL) -> Bool {
        ["png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif"].contains(u.pathExtension.lowercased())
    }

    /// Files and folders as a flat, de-duplicated list of the files the engine can read (hidden files skipped).
    nonisolated static func expand(_ urls: [URL]) -> [URL] {
        var out: [URL] = []
        let fm = FileManager.default
        for u in urls {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: u.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                let e = fm.enumerator(at: u, includingPropertiesForKeys: [.isRegularFileKey],
                                      options: [.skipsHiddenFiles, .skipsPackageDescendants])
                let found = (e?.allObjects as? [URL] ?? []).filter { importExtensions.contains($0.pathExtension.lowercased()) }
                out += found.sorted { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
            } else if importExtensions.contains(u.pathExtension.lowercased()) {
                out.append(u)
            }
        }
        var seen = Set<String>()
        return out.filter { seen.insert($0.standardizedFileURL.path).inserted }
    }
}

/// The last ten DOIs inspected, newest first.
enum RecentDOIs {
    static let key = "recentDOIs"
    static var all: [String] { UserDefaults.standard.stringArray(forKey: key) ?? [] }
    static func add(_ doi: String) {
        UserDefaults.standard.set(Array(([doi] + all.filter { $0 != doi }).prefix(10)), forKey: key)
    }
}
