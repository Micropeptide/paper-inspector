import AppKit
import Observation
import UniformTypeIdentifiers
import WebKit

/// What an export includes beyond the findings themselves.
struct ExportOptions: Equatable {
    /// Context notes (info findings such as the publication timeline).
    var includeNotes = false
    /// The reviewer's own notes on each ruling.  Off by default for files meant to be shared.
    var includeRulingNotes = false

    static func remembered(for kind: ExportKind) -> ExportOptions {
        let d = UserDefaults.standard
        return ExportOptions(includeNotes: d.object(forKey: "export.\(kind.rawValue).info") as? Bool ?? false,
                             includeRulingNotes: d.object(forKey: "export.\(kind.rawValue).notes") as? Bool
                                ?? (kind == .csv))
    }

    func remember(for kind: ExportKind) {
        UserDefaults.standard.set(includeNotes, forKey: "export.\(kind.rawValue).info")
        UserDefaults.standard.set(includeRulingNotes, forKey: "export.\(kind.rawValue).notes")
    }
}

/// Exports in progress, so the toolbar can show that something is being saved.
@MainActor @Observable
final class ExportTracker {
    static let shared = ExportTracker()
    private(set) var active: [String] = []
    func begin(_ name: String) { active.append(name) }
    func end(_ name: String) { if let i = active.firstIndex(of: name) { active.remove(at: i) } }
    var isBusy: Bool { !active.isEmpty }
}

/// Save panels for exports, attached to the front window as sheets.
@MainActor
enum Exporter {
    static func fileName(_ kind: ExportKind, run: String) -> String {
        let base = "paperinspector-\(FileFetcher.safe(run))"
        switch kind {
        case .csv: return "\(base)-findings.csv"
        case .xlsx: return "\(base)-findings.xlsx"
        case .pdf: return "\(base)-report.pdf"
        case .evidence: return "\(base)-evidence.zip"
        case .html: return "\(base).html"
        case .json: return "\(base).json"
        }
    }

    static func contentType(_ kind: ExportKind) -> UTType? {
        switch kind {
        case .csv: return .commaSeparatedText
        case .xlsx: return UTType(filenameExtension: "xlsx")
        case .pdf: return .pdf
        case .evidence: return .zip
        case .html: return .html
        case .json: return .json
        }
    }

    static func export(_ kind: ExportKind, run: String) {
        let client = EngineController.shared.client
        var options = ExportOptions.remembered(for: kind)
        let boxes = optionBoxes(kind, options)
        ask(name: fileName(kind, run: run), type: contentType(kind), accessory: boxes.view, before: {
            options.includeNotes = boxes.info?.state == .on
            options.includeRulingNotes = boxes.notes?.state == .on
            options.remember(for: kind)
        }) { dest in
            guard let remote = client.exportURL(run, kind, options: options) else { return }
            _ = try await client.downloadExport(remote, to: dest)
        }
    }

    /// Checkboxes under the save panel for the options this kind of export takes.
    private static func optionBoxes(_ kind: ExportKind, _ o: ExportOptions) -> (view: NSView?, info: NSButton?, notes: NSButton?) {
        var info: NSButton?
        var notes: NSButton?
        if kind.takesInfo {
            info = NSButton(checkboxWithTitle: "Include context notes (such as the publication timeline)", target: nil, action: nil)
            info?.state = o.includeNotes ? .on : .off
        }
        if kind.takesRulingNotes {
            notes = NSButton(checkboxWithTitle: "Include your notes on each ruling", target: nil, action: nil)
            notes?.state = o.includeRulingNotes ? .on : .off
        }
        let boxes = [info, notes].compactMap { $0 }
        guard !boxes.isEmpty else { return (nil, nil, nil) }
        let stack = NSStackView(views: boxes)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 20, bottom: 10, right: 20)
        stack.setFrameSize(stack.fittingSize)
        return (stack, info, notes)
    }

    /// The engine's batch results as CSV: one row per paper.
    static func exportBatch(_ batch: QueueBatch) {
        let client = EngineController.shared.client
        let name = "paperinspector-batch-\(FileFetcher.safe(batch.name.isEmpty ? batch.id : batch.name)).csv"
        ask(name: name, type: .commaSeparatedText) { dest in
            _ = try await client.downloadExport(client.batchCSVURL(batch.id), to: dest)
        }
    }

    static func saveCopy(of file: URL, suggestedName: String) {
        ask(name: suggestedName, type: UTType(filenameExtension: file.pathExtension)) { dest in
            let fm = FileManager.default
            if fm.fileExists(atPath: dest.path) { try fm.removeItem(at: dest) }
            try fm.copyItem(at: file, to: dest)
        }
    }

    static func saveText(_ text: String, suggestedName: String) {
        ask(name: suggestedName, type: .plainText) { dest in try Data(text.utf8).write(to: dest, options: .atomic) }
    }

    private static func ask(name: String, type: UTType?, accessory: NSView? = nil, before: @escaping () -> Void = {},
                            write: @escaping (URL) async throws -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = name
        if let type { panel.allowedContentTypes = [type] }
        panel.canCreateDirectories = true
        panel.accessoryView = accessory
        let done: (NSApplication.ModalResponse) -> Void = { response in
            guard response == .OK, let dest = panel.url else { return }
            before()
            Task { @MainActor in
                ExportTracker.shared.begin(dest.lastPathComponent)
                defer { ExportTracker.shared.end(dest.lastPathComponent) }
                do { try await write(dest) } catch {
                    ScanQueue.shared.present(AppError(title: "Couldn’t save “\(dest.lastPathComponent)”",
                                                      message: AppError.from(error).message))
                }
            }
        }
        if let w = NSApp.keyWindow ?? NSApp.mainWindow { panel.beginSheetModal(for: w, completionHandler: done) }
        else { done(panel.runModal()) }
    }
}

/// Renders the engine's HTML report off screen to print it.
@MainActor
final class ReportPrinter: NSObject, WKNavigationDelegate {
    private static var active: Set<ReportPrinter> = []
    private let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 820, height: 1100))
    private var loaded: CheckedContinuation<Void, Error>?

    static func print(run: String) {
        Task {
            do {
                let p = try await ReportPrinter.load(run: run)
                p.run(save: nil) { _ in }
            } catch {
                ScanQueue.shared.present(AppError(title: "Couldn’t print the report", message: AppError.from(error).message))
            }
        }
    }

    private static func load(run: String) async throws -> ReportPrinter {
        let html = try await EngineController.shared.client.reportHTML(run)
        let p = ReportPrinter()
        active.insert(p)
        p.web.navigationDelegate = p
        do {
            try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
                p.loaded = c
                p.web.loadHTMLString(html, baseURL: EngineController.shared.client.reportURL(run))
            }
        } catch {
            active.remove(p)
            throw error
        }
        return p
    }

    private var finish: ((Bool) -> Void)?

    private func run(save dest: URL?, finish: @escaping (Bool) -> Void) {
        self.finish = finish
        let info = (NSPrintInfo.shared.copy() as? NSPrintInfo) ?? NSPrintInfo()
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.isVerticallyCentered = false
        if let dest {
            info.jobDisposition = .save
            info.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = dest
        }
        let op = web.printOperation(with: info)
        op.showsPrintPanel = dest == nil
        op.showsProgressPanel = dest == nil
        op.view?.frame = web.bounds
        if let w = NSApp.keyWindow ?? NSApp.mainWindow {
            op.runModal(for: w, delegate: self, didRun: #selector(printDidRun(_:success:contextInfo:)), contextInfo: nil)
        } else {
            done(op.run())
        }
    }

    @objc private func printDidRun(_ op: NSPrintOperation, success: Bool, contextInfo: UnsafeMutableRawPointer?) {
        done(success)
    }

    private func done(_ ok: Bool) {
        finish?(ok)
        finish = nil
        ReportPrinter.active.remove(self)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded?.resume()
        loaded = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        loaded?.resume(throwing: error)
        loaded = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loaded?.resume(throwing: error)
        loaded = nil
    }
}
