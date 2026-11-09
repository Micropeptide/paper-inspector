import AppKit
import SwiftUI

enum ExportKind: String, CaseIterable {
    case csv, xlsx, pdf, evidence, html, json
    var label: String {
        switch self {
        case .csv: return "Findings as CSV…"
        case .xlsx: return "Findings as Excel Workbook…"
        case .pdf: return "Report as PDF…"
        case .evidence: return "Evidence Pack…"
        case .html: return "Report as HTML…"
        case .json: return "Raw Report (JSON)…"
        }
    }
    var help: String {
        switch self {
        case .csv: return "One row per finding, with your rulings"
        case .xlsx: return "A workbook with the findings and the paper's details"
        case .pdf: return "A printable report of the screen and your rulings"
        case .evidence: return "A zip of the report, figures, evidence and checksums, for an integrity office"
        case .html: return "The report as a web page"
        case .json: return "The engine's full report, for other tools"
        }
    }
    /// Whether the export can include context notes (info findings).
    var takesInfo: Bool { self == .csv || self == .xlsx || self == .pdf }
    /// Whether the export can include the reviewer's notes on rulings.
    var takesRulingNotes: Bool { self == .csv || self == .pdf || self == .evidence }
}

/// What the focused main window can do; menu commands act on the window in front through this, never by
/// broadcasting to every window.
struct WindowActions {
    var newScan: () -> Void
    var openFiles: () -> Void
    var inspectDOI: () -> Void
    var selectRun: (String) -> Void
    var run: RunActions?
}

struct RunActions {
    var nextFinding: () -> Void
    var previousFinding: () -> Void
    var nextFigure: () -> Void
    var previousFigure: () -> Void
    var viewMode: ViewMode
    var setViewMode: (ViewMode) -> Void
    var zoomIn: () -> Void
    var zoomOut: () -> Void
    var zoomToFit: () -> Void
    var actualSize: () -> Void
    var askAI: (() -> Void)?
    var draftComment: () -> Void
    var export: (ExportKind) -> Void
    var printReport: () -> Void
    var reveal: (() -> Void)?
    var copyDOI: (() -> Void)?
    var trash: () -> Void
    var rerun: (() -> Void)?
    var mark: ((Ruling) -> Void)?
    var editNote: (() -> Void)?
    var letter: (LetterKind) -> Void
    var blink: (() -> Void)?
    var clearPicks: (() -> Void)?
}

struct WindowActionsKey: FocusedValueKey { typealias Value = WindowActions }

extension FocusedValues {
    var windowActions: WindowActions? {
        get { self[WindowActionsKey.self] }
        set { self[WindowActionsKey.self] = newValue }
    }
}

struct AppCommands: Commands {
    let engine: EngineController
    let queue: ScanQueue
    @FocusedValue(\.windowActions) private var actions
    @Environment(\.openWindow) private var openWindow
    @AppStorage("showNotes") private var showNotes = false

    private var run: RunActions? { actions?.run }

    var body: some Commands {
        SidebarCommands()
        InspectorCommands()
        ToolbarCommands()
        TextEditingCommands()

        CommandGroup(replacing: .appInfo) {
            Button("About Paper Inspector") { about() }
        }

        CommandGroup(replacing: .newItem) {
            Button("New Scan") {
                if let actions { actions.newScan() } else { openWindow(id: "main") }
            }
            .keyboardShortcut("n")
            Button("New Window") { openWindow(id: "main") }
                .keyboardShortcut("n", modifiers: [.command, .option])
            Divider()
            Button("Open Files…") {
                if let actions { actions.openFiles() } else { openWithoutWindow() }
            }
            .keyboardShortcut("o")
            Button("Inspect DOI…") {
                if let actions { actions.inspectDOI() } else { openWindow(id: "main") }
            }
            .keyboardShortcut("d", modifiers: [.command, .shift])
            Menu("Open Recent") {
                ForEach(engine.runs.prefix(10)) { r in
                    Button(r.title) {
                        if let actions { actions.selectRun(r.id) } else { openWindow(id: "main", value: r.id) }
                    }
                }
            }
            .disabled(engine.runs.isEmpty)
        }

        CommandGroup(after: .saveItem) {
            Menu("Export") {
                Button(ExportKind.pdf.label) { run?.export(.pdf) }.keyboardShortcut("e", modifiers: [.command, .shift])
                Button(ExportKind.csv.label) { run?.export(.csv) }
                Button(ExportKind.xlsx.label) { run?.export(.xlsx) }
                Button(ExportKind.evidence.label) { run?.export(.evidence) }
                    .keyboardShortcut("e", modifiers: [.command, .shift, .option])
                Divider()
                Button(ExportKind.html.label) { run?.export(.html) }
                Button(ExportKind.json.label) { run?.export(.json) }
                Divider()
                ForEach(LetterKind.allCases) { k in
                    Button(k.menuLabel) { run?.letter(k) }
                }
            }
            .disabled(run == nil)
            Button("Re-run Scan") { run?.rerun?() }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(run?.rerun == nil)
            Button("Show Run Folder in Finder") { run?.reveal?() }
                .keyboardShortcut("r", modifiers: [.command, .option])
                .disabled(run?.reveal == nil)
        }

        CommandGroup(replacing: .printItem) {
            Button("Print Report…") { run?.printReport() }
                .keyboardShortcut("p")
                .disabled(run == nil)
        }

        CommandGroup(after: .pasteboard) {
            Divider()
            Button("Copy DOI") { run?.copyDOI?() }
                .keyboardShortcut("c", modifiers: [.command, .shift])
                .disabled(run?.copyDOI == nil)
            Button("Move Scan to Trash…") { run?.trash() }
                .keyboardShortcut(.delete, modifiers: .command)
                .disabled(run == nil)
        }

        CommandGroup(after: .sidebar) {
            Toggle("Show Notes", isOn: $showNotes)
                .keyboardShortcut("n", modifiers: [.command, .shift])
            Menu("Figure View") {
                ForEach(Array(ViewMode.allCases.enumerated()), id: \.element) { i, m in
                    Toggle(m.label, isOn: Binding(get: { run?.viewMode == m }, set: { if $0 { run?.setViewMode(m) } }))
                        .keyboardShortcut(KeyEquivalent(Character(String(i + 1))), modifiers: [.command, .option])
                }
            }
            .disabled(run == nil)
            Divider()
            Button("Zoom In") { run?.zoomIn() }.keyboardShortcut("+").disabled(run == nil)
            Button("Zoom Out") { run?.zoomOut() }.keyboardShortcut("-").disabled(run == nil)
            Button("Actual Size") { run?.actualSize() }.keyboardShortcut("0").disabled(run == nil)
            Button("Zoom to Fit") { run?.zoomToFit() }.keyboardShortcut("9").disabled(run == nil)
            Divider()
            Button("Previous Figure") { run?.previousFigure() }.keyboardShortcut("[").disabled(run == nil)
            Button("Next Figure") { run?.nextFigure() }.keyboardShortcut("]").disabled(run == nil)
            Divider()
        }

        CommandMenu("Inspect") {
            Button("Next Finding") { run?.nextFinding() }
                .keyboardShortcut(.downArrow, modifiers: [.command, .option])
                .disabled(run == nil)
            Button("Previous Finding") { run?.previousFinding() }
                .keyboardShortcut(.upArrow, modifiers: [.command, .option])
                .disabled(run == nil)
            Divider()
            ForEach(Array(Ruling.decisions.enumerated()), id: \.element) { i, r in
                Button("Rule \(r.label)") { run?.mark?(r) }
                    .keyboardShortcut(KeyEquivalent(Character(String(i + 1))), modifiers: .command)
                    .disabled(run?.mark == nil)
            }
            Button("Clear Ruling") { run?.mark?(.unreviewed) }
                .keyboardShortcut("4", modifiers: .command)
                .disabled(run?.mark == nil)
            Button("Edit Ruling Note") { run?.editNote?() }
                .keyboardShortcut("'", modifiers: .command)
                .disabled(run?.editNote == nil)
            Divider()
            Button("Open in Blink Comparator") { run?.blink?() }
                .keyboardShortcut("b", modifiers: [.command, .option])
                .disabled(run?.blink == nil)
            Button("Clear Picked Panels") { run?.clearPicks?() }
                .disabled(run?.clearPicks == nil)
            Divider()
            Button("Ask the AI Investigator") { run?.askAI?() }
                .keyboardShortcut("a", modifiers: [.command, .shift])
                .disabled(run?.askAI == nil)
            Button("Draft Comment…") { run?.draftComment() }
                .keyboardShortcut("m", modifiers: [.command, .shift])
                .disabled(run == nil)
        }

        CommandGroup(replacing: .help) {
            Button("Show Engine Log") { openWindow(id: "log") }
            Button("Show Data Folder in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([EngineController.dataFolder])
            }
        }
    }

    private func about() {
        let engineLine = engine.version.map { "Analysis engine \($0)" } ?? "Analysis engine: \(engine.statusText.lowercased())"
        let credits = NSAttributedString(
            string: "\(engineLine)\nScreens papers for image reuse, impossible numbers, problem references and missing "
                + "information. Files stay on this Mac unless you ask the AI investigator.",
            attributes: [.font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
                         .foregroundColor: NSColor.secondaryLabelColor])
        NSApp.orderFrontStandardAboutPanel(options: [.credits: credits])
    }

    /// ⌘O with no main window in front (say, only Settings is open).
    private func openWithoutWindow() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.allowedContentTypes = ScanQueue.importTypes
        panel.message = "Choose the paper's PDF and any supplementary files"
        guard panel.runModal() == .OK else { return }
        openWindow(id: "main")
        queue.importFiles(panel.urls)
    }
}
