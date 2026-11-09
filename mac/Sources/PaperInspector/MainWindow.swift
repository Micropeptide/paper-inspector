import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum InspectorTab: String, CaseIterable, Identifiable {
    case finding, paper, files
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

/// Opens a run's two panels in the separate blink comparator window.  Panel ids, not URLs, so a restored window
/// still works after the engine restarts on another port.
struct BlinkSpec: Codable, Hashable {
    var run: String
    var a: String
    var b: String
    var title: String
}

/// One main window: scans | findings | figure stage, with the finding inspector on the trailing edge.
struct MainWindow: View {
    let initialRun: String?

    @Environment(EngineController.self) private var engine
    @Environment(ScanQueue.self) private var queue
    @Environment(ExportTracker.self) private var exports
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    @Environment(\.undoManager) private var undoManager

    @SceneStorage("sel.run") private var storedRun = ""
    @SceneStorage("runState") private var runStateJSON = ""
    @SceneStorage("inspectorShown") private var inspectorShown = true
    @SceneStorage("inspectorTab") private var inspectorTabRaw = InspectorTab.finding.rawValue
    @SceneStorage("viewMode") private var viewModeRaw = ViewMode.original.rawValue
    @SceneStorage("category") private var category = "all"
    @SceneStorage("columns") private var columnsRaw = "all"
    @AppStorage("showNotes") private var showNotes = false
    @AppStorage("findingsSort") private var sortRaw = FindingSort.severity.rawValue
    @AppStorage("rulingFilter") private var rulingFilter = "all"

    @State private var sceneID = UUID()
    @State private var selection: Set<String> = []
    @State private var model: RunModel?
    @State private var reportFile: URL?
    @State private var runQuery = ""
    @State private var findingQuery = ""
    @State private var dropTargeted = false
    @State private var importing = false
    @State private var trashing: [RunSummary] = []
    @State private var showDraft = false
    @State private var letterKind: LetterKind?
    @State private var aiSetupNeeded = false
    @State private var doiFocusToken = 0
    @State private var restored = false

    private var selectedRun: String? { selection.count == 1 ? selection.first : nil }
    private var currentModel: RunModel? { model?.runID == selectedRun ? model : nil }
    private var viewMode: ViewMode { ViewMode(rawValue: viewModeRaw) ?? .original }

    private var filter: FindingFilter {
        FindingFilter(category: category, showNotes: showNotes, query: findingQuery,
                      ruling: Ruling(rawValue: rulingFilter) == nil ? "all" : rulingFilter,
                      sort: FindingSort(rawValue: sortRaw) ?? .severity)
    }

    private var columns: Binding<NavigationSplitViewVisibility> {
        Binding(get: {
            switch columnsRaw {
            case "doubleColumn": return .doubleColumn
            case "detailOnly": return .detailOnly
            default: return .all
            }
        }, set: { v in
            columnsRaw = v == .doubleColumn ? "doubleColumn" : v == .detailOnly ? "detailOnly" : "all"
        })
    }

    var body: some View {
        NavigationSplitView(columnVisibility: columns) {
            RunsSidebar(selection: $selection, query: $runQuery, onTrash: askTrash,
                        onOpenInNewWindow: { openWindow(id: "main", value: $0) },
                        onAskAI: { selection = [$0]; askAI($0) },
                        onReveal: reveal)
                .navigationSplitViewColumnWidth(min: 200, ideal: 240, max: 320)
        } content: {
            contentColumn
                .navigationSplitViewColumnWidth(min: 280, ideal: 320, max: 420)
        } detail: {
            detailColumn
        }
        .navigationTitle(currentModel?.report?.displayTitle ?? "Paper Inspector")
        .navigationSubtitle(currentModel?.report?.subtitle ?? "")
        .dropDestination(for: URL.self) { urls, _ in
            let files = urls.filter(\.isFileURL)
            guard !files.isEmpty else { return false }
            queue.importFiles(files, origin: sceneID, selection: selectedRun)
            return true
        } isTargeted: { dropTargeted = $0 }
        .overlay { if dropTargeted { DropOverlay() } }
        .fileImporter(isPresented: $importing, allowedContentTypes: ScanQueue.importTypes, allowsMultipleSelection: true) { result in
            if case .success(let urls) = result { queue.importFiles(urls, origin: sceneID, selection: selectedRun) }
        }
        .fileDialogMessage("Choose the paper’s PDF and any supplementary files, or a folder")
        .confirmationDialog(trashTitle, isPresented: Binding(get: { !trashing.isEmpty }, set: { if !$0 { trashing = [] } }),
                            titleVisibility: .visible) {
            Button("Move to Trash", role: .destructive) {
                let items = trashing
                Task { await trash(items) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The scan’s folder goes to the Trash. Choose Edit > Undo to put it back.")
        }
        .alert("Choose an AI model first", isPresented: $aiSetupNeeded) {
            Button("Open Settings…") { openSettings() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The AI investigator needs a model provider and, for most providers, an API key. Set them up in Settings > AI Investigator.")
        }
        .sheet(isPresented: $showDraft) {
            if let run = selectedRun { DraftSheet(runID: run) }
        }
        .sheet(item: $letterKind) { kind in
            if let run = selectedRun { LetterSheet(runID: run, kind: kind) }
        }
        .focusedSceneValue(\.windowActions, windowActions)
        .task { restore() }
        .task(id: modelKey) { await loadModel() }
        .onChange(of: selection) { _, _ in
            storedRun = selectedRun ?? ""
        }
        .onChange(of: currentModel?.viewState) { _, state in
            if let state, let run = selectedRun { saveRunState(run, state) }
        }
        .onChange(of: engine.runs) { _, runs in
            // A restored selection whose scan was deleted elsewhere is dropped quietly.
            guard engine.runsLoaded, engine.runsError == nil else { return }
            let ids = Set(runs.map(\.id))
            if !selection.isSubset(of: ids) { selection = selection.intersection(ids) }
        }
        .onChange(of: queue.finished) { _, f in claim(f) }
    }

    // MARK: columns

    @ViewBuilder private var contentColumn: some View {
        if let m = currentModel {
            if m.report != nil {
                FindingsColumn(model: m, filter: filter, category: $category, showNotes: $showNotes,
                               sortRaw: $sortRaw, rulingFilter: $rulingFilter, query: $findingQuery)
            } else if let e = m.error {
                ErrorBanner(error: e, retry: { Task { await m.load(restoring: nil) } }).padding()
                    .frame(maxHeight: .infinity, alignment: .top)
            } else {
                ProgressView("Opening scan…").frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else {
            ContentUnavailableView {
                Label("No Scan Selected", systemImage: "doc.text.magnifyingglass")
            } description: {
                Text(selection.count > 1 ? "\(selection.count) scans selected." : "Choose a scan in the sidebar, or start a new one.")
            }
        }
    }

    @ViewBuilder private var detailColumn: some View {
        Group {
            if !engine.isReady {
                EngineStatusView()
            } else if let m = currentModel, m.report != nil {
                FigureStage(model: m, viewMode: viewMode, aiJob: queue.investigation(for: m.runID))
                    .inspector(isPresented: $inspectorShown) {
                        FindingInspector(model: m, tab: Binding(get: { InspectorTab(rawValue: inspectorTabRaw) ?? .finding },
                                                                set: { inspectorTabRaw = $0.rawValue }))
                            .inspectorColumnWidth(min: 300, ideal: 360, max: 520)
                    }
            } else if selectedRun != nil {
                Color.clear
            } else {
                HomeView(sceneID: sceneID, focusToken: doiFocusToken, openFiles: { importing = true })
            }
        }
        .toolbar { toolbarItems }
    }

    @ToolbarContentBuilder private var toolbarItems: some ToolbarContent {
        if let m = currentModel, let r = m.report {
            ToolbarItem(placement: .principal) {
                Picker("Figure View", selection: Binding(get: { viewMode }, set: { viewModeRaw = $0.rawValue })) {
                    ForEach(ViewMode.allCases) { mode in
                        Text(mode.label).tag(mode).help(mode.help)
                    }
                }
                .pickerStyle(.segmented)
                .help("How the figure is drawn. Each view brings out a different kind of edit.")
                .disabled(m.pageTarget != nil || r.figures.isEmpty)
            }
            ToolbarItemGroup(placement: .primaryAction) {
                ControlGroup {
                    Button { zoomOut(m) } label: { Label("Zoom Out", systemImage: "minus.magnifyingglass") }
                    Button { m.zoom = nil } label: { Label("Zoom to Fit", systemImage: "arrow.up.left.and.down.right.magnifyingglass") }
                    Button { zoomIn(m) } label: { Label("Zoom In", systemImage: "plus.magnifyingglass") }
                }
                .disabled(m.pageTarget != nil || m.figure == nil)
                aiButton(m, r)
                Button { showDraft = true } label: { Label("Draft Comment", systemImage: "text.bubble") }
                    .help("Draft a polite comment for the authors or PubPeer")
                if let reportFile {
                    ShareLink(item: reportFile) { Label("Share Report", systemImage: "square.and.arrow.up") }
                        .help("Share the HTML report")
                }
                Menu {
                    ForEach(ExportKind.allCases, id: \.self) { kind in
                        Button(kind.label) { export(kind) }.help(kind.help)
                    }
                    Divider()
                    ForEach(LetterKind.allCases) { k in
                        Button(k.menuLabel) { letterKind = k }
                    }
                } label: {
                    if exports.isBusy {
                        Label { Text("Exporting") } icon: { ProgressView().controlSize(.small) }
                    } else {
                        Label("Export", systemImage: "square.and.arrow.up.on.square")
                    }
                }
                .help(exports.isBusy ? "Saving \(exports.active.joined(separator: ", "))" : "Export the report, the findings or an evidence pack")
                Button { inspectorShown.toggle() } label: { Label("Inspector", systemImage: "sidebar.trailing") }
                    .help(inspectorShown ? "Hide the inspector" : "Show the inspector")
            }
        }
        if currentModel == nil {
            ToolbarItem(placement: .primaryAction) {
                Button { importing = true } label: { Label("Open Files…", systemImage: "plus") }
                    .help("Inspect a PDF and its supplementary files (⌘O)")
                    .disabled(!engine.isReady)
            }
        }
    }

    @ViewBuilder private func aiButton(_ m: RunModel, _ r: Report) -> some View {
        if let job = queue.investigation(for: m.runID), job.isRunning {
            Menu {
                Text(job.stage)
                Button("Stop the AI Investigator") { queue.cancel(job.id) }
            } label: {
                Label {
                    Text("AI Working")
                } icon: {
                    ProgressView(value: job.progress).progressViewStyle(.circular).controlSize(.small)
                }
            }
            .help("The AI investigator is working: \(job.stage)")
        } else {
            Button { askAI(m.runID) } label: {
                Label(r.aiReview == nil ? "Ask AI" : "Ask AI Again", systemImage: "sparkles")
            }
            .help("Have an AI model review every finding and look for anything missed. Sends figures to the model you chose in Settings.")
            .disabled(r.isCase)
        }
    }

    // MARK: actions

    private var windowActions: WindowActions {
        WindowActions(
            newScan: { selection = []; doiFocusToken += 1 },
            openFiles: { importing = true },
            inspectDOI: { selection = []; doiFocusToken += 1 },
            selectRun: { selection = [$0] },
            run: currentModel.flatMap { m in m.report.map { runActions(m, $0) } })
    }

    private func runActions(_ m: RunModel, _ r: Report) -> RunActions {
        let f = filter
        let pairSpec: BlinkSpec? = {
            if m.picked.count == 2 { return BlinkSpec(run: m.runID, a: m.picked[0], b: m.picked[1], title: r.displayTitle) }
            if let fd = m.finding, fd.isPair { return BlinkSpec(run: m.runID, a: fd.panels[0], b: fd.panels[1], title: fd.title) }
            return nil
        }()
        let busyAI = queue.investigation(for: m.runID)?.isRunning ?? false
        return RunActions(
            nextFinding: { m.step(1, filter: f) },
            previousFinding: { m.step(-1, filter: f) },
            nextFigure: { m.stepFigure(1) },
            previousFigure: { m.stepFigure(-1) },
            viewMode: viewMode,
            setViewMode: { viewModeRaw = $0.rawValue },
            zoomIn: { zoomIn(m) },
            zoomOut: { zoomOut(m) },
            zoomToFit: { m.zoom = nil },
            actualSize: { m.zoom = 1 },
            askAI: busyAI || r.isCase ? nil : { askAI(m.runID) },
            draftComment: { showDraft = true },
            export: { export($0) },
            printReport: { ReportPrinter.print(run: m.runID) },
            reveal: EngineController.runFolder(m.runID) == nil ? nil : { reveal(m.runID) },
            copyDOI: r.paper.doi.isEmpty ? nil : {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(r.paper.doi, forType: .string)
            },
            trash: { askTrash([m.runID]) },
            rerun: r.isCase ? nil : { rerun(r) },
            mark: m.selectedFinding.flatMap { id -> ((Ruling) -> Void)? in
                m.supportsRulings ? { r in Task { await m.setRuling(r, for: id) } } : nil
            },
            editNote: m.selectedFinding == nil || !m.supportsRulings ? nil : {
                inspectorShown = true
                inspectorTabRaw = InspectorTab.finding.rawValue
                m.noteFocusToken += 1
            },
            letter: { letterKind = $0 },
            blink: pairSpec.map { spec -> () -> Void in { openWindow(id: "blink", value: spec) } },
            clearPicks: m.picked.isEmpty ? nil : { m.picked = [] })
    }

    private func zoomIn(_ m: RunModel) { m.zoom = min(8, (m.zoom ?? m.fitScale) * 1.25) }
    private func zoomOut(_ m: RunModel) {
        let z = (m.zoom ?? m.fitScale) / 1.25
        m.zoom = z <= m.fitScale ? nil : z
    }

    private func askAI(_ run: String) {
        Task {
            if let s = try? await engine.client.settings(), s.needsKey {
                aiSetupNeeded = true
                return
            }
            queue.start(.investigate(run: run), title: "AI investigator", origin: sceneID, selection: run)
        }
    }

    private func export(_ kind: ExportKind) {
        guard let m = currentModel, m.report != nil else { return }
        Exporter.export(kind, run: m.runID)
    }

    private func reveal(_ run: String) {
        if let folder = EngineController.runFolder(run) { NSWorkspace.shared.activateFileViewerSelecting([folder]) }
    }

    /// Re-screens the files this scan was made from (after an engine update, say).
    private func rerun(_ r: Report) {
        Task {
            do {
                let client = engine.client
                let cached = try await FileFetcher.source(run: r.id)
                // Upload under the paper's own file name, which the engine shows when it finds no title.
                let named = r.paper.source.isEmpty ? cached.lastPathComponent : FileFetcher.safe(r.paper.source)
                let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                let source = dir.appendingPathComponent(named)
                try FileManager.default.copyItem(at: cached, to: source)
                var supplements: [URL] = []
                for s in r.sources.dropFirst() {
                    if let u = try? await FileFetcher.local(client.supplementURL(r.id, s.name), run: r.id, name: s.name) {
                        supplements.append(u)
                    }
                }
                queue.start(.files(main: source, supplements: supplements), title: r.displayTitle,
                            origin: sceneID, selection: r.id)
            } catch {
                queue.present(AppError.from(error))
            }
        }
    }

    // MARK: trash

    private var trashTitle: String {
        trashing.count == 1 ? "Move “\(trashing[0].title)” to the Trash?" : "Move \(trashing.count) scans to the Trash?"
    }

    private func askTrash(_ ids: [String]) {
        trashing = engine.runs.filter { ids.contains($0.id) }
    }

    private func trash(_ items: [RunSummary]) async {
        var moved: [(from: URL, back: URL)] = []
        var failed: [String] = []
        for r in items {
            do {
                if let folder = EngineController.runFolder(r.id) {
                    let result = try await NSWorkspace.shared.recycle([folder])
                    if let dest = result[folder] { moved.append((dest, folder)) }
                } else {
                    try await engine.client.deleteRun(r.id)
                }
                ImageStore.shared.purge(run: r.id)
                FileFetcher.purge(run: r.id)
                selection.remove(r.id)
                engine.forgetRun(r.id)
            } catch {
                failed.append("\(r.title): \(AppError.from(error).message)")
            }
        }
        if !moved.isEmpty, let undoManager {
            let pairs = moved
            undoManager.registerUndo(withTarget: engine) { target in
                for p in pairs { try? FileManager.default.moveItem(at: p.from, to: p.back) }
                Task { await target.refreshRuns() }
            }
            undoManager.setActionName(moved.count == 1 ? "Move to Trash" : "Move Scans to Trash")
        }
        await engine.refreshRuns()
        if !failed.isEmpty {
            queue.present(AppError(title: "Some scans weren’t removed", message: failed.joined(separator: "\n")))
        }
    }

    // MARK: state

    private var modelKey: String {
        "\(selectedRun ?? "")#\(queue.revisions[selectedRun ?? ""] ?? 0)#\(engine.isReady)"
    }

    private func loadModel() async {
        guard let run = selectedRun else { model = nil; reportFile = nil; return }
        guard engine.isReady else { return }
        if model?.runID != run {
            model = RunModel(runID: run)
            reportFile = nil
        }
        guard let m = model else { return }
        await m.load(restoring: runStates[run])
        if m.report != nil, let file = try? await FileFetcher.report(run: run), model?.runID == run { reportFile = file }
    }

    private func restore() {
        guard !restored else { return }
        restored = true
        // A restored window returns to where it was; a window opened for a run starts on that run.
        if !storedRun.isEmpty { selection = [storedRun] } else if let initialRun { selection = [initialRun] }
    }

    private var runStates: [String: RunViewState] {
        (try? JSONDecoder().decode([String: RunViewState].self, from: Data(runStateJSON.utf8))) ?? [:]
    }

    private func saveRunState(_ run: String, _ state: RunViewState) {
        var all = runStates
        all[run] = state
        if all.count > 40 { all = Dictionary(uniqueKeysWithValues: all.filter { $0.key == run || selection.contains($0.key) }.map { ($0.key, $0.value) }) }
        if let d = try? JSONEncoder().encode(all) { runStateJSON = String(decoding: d, as: UTF8.self) }
    }

    /// A scan started from this window opens here when it finishes, unless the reader has moved on meanwhile.
    private func claim(_ f: ScanQueue.Finished?) {
        guard let f, queue.finished?.token == f.token, f.origin == nil || f.origin == sceneID else { return }
        queue.finished = nil
        if f.origin == nil || selectedRun == f.selectionAtStart || selectedRun == nil { selection = [f.run] }
    }
}

private struct DropOverlay: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 12)
            .strokeBorder(Color.accentColor, lineWidth: 3)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                Label("Drop a paper’s PDF and its supplementary files", systemImage: "arrow.down.doc")
                    .font(.title2)
                    .foregroundStyle(.primary)
            }
            .padding(8)
            .allowsHitTesting(false)
            .accessibilityLabel("Drop files to inspect them")
    }
}

/// What the detail area shows while the engine is starting or after it failed.
struct EngineStatusView: View {
    @Environment(EngineController.self) private var engine
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        switch engine.state {
        case .failed(let e):
            failure(title: e.title, message: e.message)
        case .crashed(let code):
            failure(title: "The engine stopped unexpectedly", message: "It exited with code \(code). Restart it, or check the log for the cause.")
        case .launching(let attempt):
            VStack(spacing: 12) {
                ProgressView()
                Text("Starting the analysis engine…").font(.headline)
                if attempt > 8 {
                    Text("The first start can take a minute while Python loads its libraries.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        default:
            ProgressView("Looking for the analysis engine…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func failure(title: String, message: String) -> some View {
        ContentUnavailableView {
            Label(title, systemImage: "exclamationmark.triangle")
        } description: {
            Text(message).textSelection(.enabled)
        } actions: {
            HStack {
                Button("Retry") { engine.restart() }.buttonStyle(.borderedProminent)
                Button("Show Log") { openWindow(id: "log") }
                Button("Choose Engine…") { openSettings() }
            }
        }
    }
}
