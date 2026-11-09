import Foundation
import Observation

/// What earlier versions of the app kept on this Mac per run (Application Support/PaperInspector/triage).  Read once
/// to move those reviews into the engine's rulings, then deleted.
enum TriageStore {
    struct Entry: Decodable, Equatable {
        var state: String = "unreviewed"
        var note: String = ""
        enum CodingKeys: String, CodingKey { case state, note }
        init(state: String = "unreviewed", note: String = "") { self.state = state; self.note = note }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            state = c.value(.state, "unreviewed"); note = c.value(.note, "")
        }
    }

    static var folder: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("PaperInspector/triage", isDirectory: true)
    }
    static func file(_ run: String) -> URL { folder.appendingPathComponent(FileFetcher.safe(run) + ".json") }

    static func load(_ run: String) -> [String: Entry] {
        guard let d = try? Data(contentsOf: file(run)) else { return [:] }
        return (try? JSONDecoder().decode([String: Entry].self, from: d)) ?? [:]
    }

    static func remove(_ run: String) { try? FileManager.default.removeItem(at: file(run)) }

    /// The rulings to send for a legacy review: only where the engine has nothing yet, so a ruling made elsewhere
    /// (the web app, another Mac) always wins.
    static func migration(_ legacy: [String: Entry], current: [String: RulingInfo]) -> [(id: String, ruling: Ruling, note: String)] {
        legacy.keys.sorted().compactMap { id in
            guard let e = legacy[id], let now = current[id], now.ruling == .unreviewed, now.note.isEmpty else { return nil }
            let r = Ruling.fromLegacy(e.state) ?? .unreviewed
            let note = e.note.trimmingCharacters(in: .whitespacesAndNewlines)
            return r == .unreviewed && note.isEmpty ? nil : (id, r, note)
        }
    }
}

/// Where the reader was in a run, restored per window.
struct RunViewState: Codable, Equatable {
    var finding: String?
    var figure: String?
}

struct FindingFilter: Equatable {
    var category = "all"
    var showNotes = false
    var query = ""
    /// "all", or a Ruling raw value.
    var ruling = "all"
    var sort: FindingSort = .severity
}

/// One open scan: its report, what is selected, and the figure stage's state.
@MainActor @Observable
final class RunModel {
    let runID: String
    private(set) var report: Report?
    private(set) var error: AppError?
    var selectedFinding: String?
    var figureID: String?
    var picked: [String] = []
    /// When set, the stage shows this place in the paper instead of a figure.
    var pageTarget: Loc?
    /// Points per figure pixel; nil means fit the figure to the stage.
    var zoom: CGFloat?
    var fitScale: CGFloat = 1
    /// Rulings by finding id: the engine's, with the reviewer's latest changes applied straight away.
    private(set) var rulings: [String: RulingInfo] = [:]
    private(set) var review: ReviewSummary?
    /// The last ruling that could not be saved.
    var rulingError: AppError?
    /// Bumped to move keyboard focus to the note field in the inspector.
    var noteFocusToken = 0
    @ObservationIgnored private var generation: [String: Int] = [:]
    /// Where rulings are saved; tests replace it.
    @ObservationIgnored var client: () -> EngineClient = { EngineController.shared.client }

    init(runID: String) {
        self.runID = runID
    }

    /// For tests and previews: a model around a report already in hand.
    init(runID: String, report: Report) {
        self.runID = runID
        self.report = report
        adopt(report)
    }

    private func adopt(_ r: Report) {
        var map: [String: RulingInfo] = [:]
        for f in r.findings { map[f.id] = f.ruling }
        // Keep a change still on its way to the engine.
        for (id, g) in generation where g > 0 { if let mine = rulings[id] { map[id] = mine } }
        rulings = map
        review = r.review
    }

    func load(restoring state: RunViewState?) async {
        do {
            let r = try await EngineController.shared.client.report(runID)
            let first = report == nil
            report = r
            adopt(r)
            error = nil
            if first { await migrateLegacyReview() }
            guard first else {
                if r.finding(selectedFinding) == nil { selectedFinding = nil }
                return
            }
            if let s = state, let f = r.finding(s.finding) {
                select(f)
                if let fig = s.figure, r.figure(fig) != nil, f.figures.isEmpty { figureID = fig }
            } else if let s = state, let fig = s.figure, r.figure(fig) != nil {
                figureID = fig
            } else if let f = r.findings.sorted(by: { $0.severity < $1.severity }).first(where: { !$0.severity.isNote }) {
                select(f)
            }
            if figureID == nil { figureID = r.figures.first?.id }
        } catch {
            self.error = AppError.from(error)
        }
    }

    var finding: Finding? { report?.finding(selectedFinding) }
    var figure: FigureInfo? { report?.figure(figureID) }
    var viewState: RunViewState { RunViewState(finding: selectedFinding, figure: figureID) }

    /// Choosing a finding shows its figure; a finding about the text shows its page but keeps the last figure, so
    /// the figure strip stays usable.
    func select(_ f: Finding?) {
        selectedFinding = f?.id
        picked = []
        guard let f else { return }
        if let fig = f.figures.first(where: { report?.figure($0) != nil }) {
            if fig != figureID { zoom = nil }
            figureID = fig
            pageTarget = nil
        } else if let loc = f.textLocation {
            pageTarget = loc
        } else {
            pageTarget = nil
        }
    }

    func showFigure(_ id: String) {
        if id != figureID { zoom = nil }
        figureID = id
        pageTarget = nil
    }

    func stepFigure(_ delta: Int) {
        guard let figs = report?.figures, !figs.isEmpty else { return }
        let i = figs.firstIndex { $0.id == figureID } ?? 0
        showFigure(figs[(i + delta + figs.count) % figs.count].id)
    }

    func togglePick(_ pid: String) {
        if picked.contains(pid) { picked.removeAll { $0 == pid } } else { picked = Array((picked + [pid]).suffix(2)) }
    }

    // MARK: rulings

    /// Whether the engine keeps rulings (older engines send no review block).
    var supportsRulings: Bool { report?.review != nil }

    func ruling(for id: String) -> RulingInfo { rulings[id] ?? RulingInfo() }

    /// Sets a ruling at once in the app and saves it to the engine.  A nil note keeps the stored note.
    func setRuling(_ ruling: Ruling, for id: String, note: String? = nil) async {
        guard report?.finding(id) != nil else { return }
        let before = self.ruling(for: id)
        rulings[id] = RulingInfo(ruling: ruling, note: note ?? before.note, updated: before.updated)
        let g = (generation[id] ?? 0) + 1
        generation[id] = g
        do {
            let client = self.client()
            let resp = ruling == .unreviewed && (note ?? before.note).isEmpty
                ? try await client.clearRuling(run: runID, finding: id)
                : try await client.setRuling(run: runID, finding: id, ruling: ruling, note: note)
            guard generation[id] == g else { return }
            generation[id] = 0
            rulings[id] = resp.ruling
            if let r = resp.review { review = r }
            rulingError = nil
        } catch {
            guard generation[id] == g else { return }
            generation[id] = 0
            rulings[id] = before
            rulingError = AppError(title: "Your ruling wasn’t saved", message: AppError.from(error).message)
        }
    }

    func setNote(_ note: String, for id: String) async {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed != ruling(for: id).note else { return }
        await setRuling(ruling(for: id).ruling, for: id, note: trimmed)
    }

    /// Reviews made with an earlier version of the app move into the engine once, then the local file goes.
    private func migrateLegacyReview() async {
        let legacy = TriageStore.load(runID)
        guard !legacy.isEmpty, supportsRulings else { return }
        for m in TriageStore.migration(legacy, current: rulings) {
            await setRuling(m.ruling, for: m.id, note: m.note)
            if rulingError != nil { return }
        }
        TriageStore.remove(runID)
    }

    /// Findings per ruling among those the filter would show apart from the ruling itself.
    func rulingCounts(_ filter: FindingFilter) -> [Ruling: Int] {
        var f = filter
        f.ruling = "all"
        var out: [Ruling: Int] = [:]
        for x in ordered(f) { out[ruling(for: x.id).ruling, default: 0] += 1 }
        return out
    }

    // MARK: findings list

    enum Group: String, CaseIterable {
        case integrity, quality, notes
        var title: String {
            switch self {
            case .integrity: return "Integrity Signals"
            case .quality: return "Quality Notes"
            case .notes: return "Notes"
            }
        }
        var caption: String {
            switch self {
            case .integrity: return "Could indicate manipulation. These set the score."
            case .quality: return "Honest problems and missing information. Not in the score."
            case .notes: return "Context such as the publication timeline."
            }
        }
    }

    func group(of f: Finding) -> Group {
        f.severity.isNote ? .notes : (f.kind == "quality" ? .quality : .integrity)
    }

    /// The findings in display order, grouped.  Notes appear only when asked for.
    func grouped(_ filter: FindingFilter) -> [(group: Group, findings: [Finding])] {
        guard let r = report else { return [] }
        let q = filter.query.trimmingCharacters(in: .whitespaces).lowercased()
        let figOrder = Dictionary(uniqueKeysWithValues: r.figures.enumerated().map { ($1.id, $0) })
        let visible = r.findings.filter { f in
            (filter.showNotes || !f.severity.isNote)
                && (filter.category == "all" || f.category == filter.category)
                && (filter.ruling == "all" || ruling(for: f.id).ruling.rawValue == filter.ruling)
                && (q.isEmpty || [f.title, f.detail, f.check, f.kindName, ruling(for: f.id).note]
                    .contains { $0.lowercased().contains(q) })
        }
        let sorted: [Finding]
        switch filter.sort {
        case .severity:
            sorted = visible.enumerated().sorted { ($0.element.severity, $0.offset) < ($1.element.severity, $1.offset) }
                .map(\.element)
        case .confidence:
            sorted = visible.sorted { $0.confidence > $1.confidence }
        case .figure:
            sorted = visible.enumerated().sorted {
                let a = $0.element.figures.first.flatMap { figOrder[$0] } ?? Int.max
                let b = $1.element.figures.first.flatMap { figOrder[$0] } ?? Int.max
                return (a, $0.offset) < (b, $1.offset)
            }.map(\.element)
        }
        return Group.allCases.compactMap { g in
            let fs = sorted.filter { group(of: $0) == g }
            return fs.isEmpty ? nil : (g, fs)
        }
    }

    func ordered(_ filter: FindingFilter) -> [Finding] { grouped(filter).flatMap(\.findings) }

    func step(_ delta: Int, filter: FindingFilter) {
        let list = ordered(filter)
        guard !list.isEmpty else { return }
        guard let i = list.firstIndex(where: { $0.id == selectedFinding }) else {
            select(delta > 0 ? list.first : list.last)
            return
        }
        let j = i + delta
        if list.indices.contains(j) { select(list[j]) }
    }

    /// Non-note findings per figure, for the figure strip's badges.
    var countsByFigure: [String: Int] {
        var out: [String: Int] = [:]
        for f in report?.findings ?? [] where !f.severity.isNote {
            for fig in f.figures { out[fig, default: 0] += 1 }
        }
        return out
    }

    func categoryCounts(showNotes: Bool) -> [String: Int] {
        var out: [String: Int] = [:]
        for f in report?.findings ?? [] where showNotes || !f.severity.isNote { out[f.category ?? "", default: 0] += 1 }
        return out
    }
}
