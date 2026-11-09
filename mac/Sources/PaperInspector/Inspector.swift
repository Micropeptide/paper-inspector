import AppKit
import QuickLook
import SwiftUI

/// The trailing inspector: the selected finding (or the reader's own comparison), the paper's record, and the files.
struct FindingInspector: View {
    let model: RunModel
    @Binding var tab: InspectorTab

    var body: some View {
        VStack(spacing: 0) {
            Picker("Inspector", selection: $tab) {
                ForEach(InspectorTab.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(10)
            Divider()
            if let r = model.report {
                switch tab {
                case .finding:
                    ScrollView {
                        Group {
                            if model.picked.count == 2 {
                                ManualCompare(runID: model.runID, a: model.picked[0], b: model.picked[1]) { model.picked = [] }
                            } else if let f = model.finding {
                                FindingDetail(model: model, finding: f, report: r).id(f.id)
                            } else {
                                Text("Choose a finding, or click two panels in the figure to compare them.")
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(16)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                case .paper:
                    PaperTab(report: r, review: model.review)
                case .files:
                    FilesTab(runID: model.runID, report: r)
                }
            }
        }
        .onChange(of: model.picked.count) { _, n in if n == 2 { tab = .finding } }
    }
}

struct FindingDetail: View {
    let model: RunModel
    let finding: Finding
    let report: Report

    @Environment(EngineController.self) private var engine
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let f = finding
        let client = engine.client
        VStack(alignment: .leading, spacing: 12) {
            SeverityLabel(severity: f.severity, text: "\(f.severity.label) · \(f.kindName)")
                .font(.callout)
                .foregroundStyle(f.severity.color)
            Text(f.title).font(.title3.weight(.semibold)).textSelection(.enabled)
            if f.confidence > 0 {
                Text("Confidence \(Int((f.confidence * 100).rounded()))%").font(.caption).foregroundStyle(.secondary)
            }
            if f.isPair {
                BlinkView(a: client.frameURL(model.runID, "a", f.panels[0], f.panels[1]),
                          b: client.frameURL(model.runID, "b", f.panels[0], f.panels[1]),
                          diff: client.frameURL(model.runID, "diff", f.panels[0], f.panels[1]),
                          labelA: f.panels[0], labelB: "\(f.panels[1]), aligned")
                Button("Open in Blink Comparator") {
                    openWindow(id: "blink", value: BlinkSpec(run: model.runID, a: f.panels[0], b: f.panels[1], title: f.title))
                }
                .controlSize(.small)
            }
            Text(f.detail).textSelection(.enabled)
            MeasurementsSection(finding: f)
            if let ai = f.ai {
                Label {
                    Text("**AI investigator: \(ai.label).** \(ai.reason)").textSelection(.enabled)
                } icon: {
                    Image(systemName: ai.isFalsePositive ? "xmark.seal" : "checkmark.seal")
                }
                .font(.callout)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background((ai.isFalsePositive ? Theme.calm : Color.secondary).opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            }
            if f.figures.count > 1 {
                HStack {
                    Text("Figures").foregroundStyle(.secondary)
                    ForEach(f.figures, id: \.self) { fid in
                        Button(report.figure(fid)?.label ?? fid) { model.showFigure(fid) }.controlSize(.small)
                    }
                }
            }
            if !f.isPair, let gif = f.evidence.first(where: { $0.hasSuffix(".gif") }) {
                EvidenceImage(runID: model.runID, name: gif, animated: true)
            }
            ForEach(f.evidence.filter { $0.hasSuffix(".png") && !$0.contains("-diff") }, id: \.self) { e in
                EvidenceImage(runID: model.runID, name: e, animated: false)
            }
            locations(f)
            Divider()
            RulingSection(model: model, finding: f)
            if !f.check.isEmpty {
                Divider()
                CheckExplanation(check: f.check)
            }
        }
    }

    @ViewBuilder private func locations(_ f: Finding) -> some View {
        ForEach(Array(f.locations.enumerated()), id: \.offset) { _, l in
            if let q = l.quote {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        if let p = l.page { Text("Page \(p)").font(.caption).foregroundStyle(.secondary) }
                        Spacer()
                        Button("Show on Page") { model.pageTarget = l }.controlSize(.small)
                    }
                    Text(q).font(.system(.callout, design: .serif)).textSelection(.enabled)
                }
                .padding(.leading, 8)
                .overlay(alignment: .leading) { Rectangle().fill(.quaternary).frame(width: 3) }
            }
            if l.file != nil || l.sheet != nil {
                SupplementRow(runID: model.runID, loc: l)
            }
        }
    }
}

struct MeasurementsSection: View {
    let finding: Finding

    private static let known: Set<String> = ["ncc_texture", "ncc", "inliers", "overlap_a", "overlap_b", "mirrored", "rotation", "scale"]

    private var rows: [(String, String)] {
        let d = finding.data
        var out: [(String, String)] = []
        if let v = d["ncc_texture"]?.double ?? d["ncc"]?.double { out.append(("Texture match", String(format: "%.2f", v))) }
        if let v = d["inliers"] { out.append(("Keypoints", v.display)) }
        if let a = d["overlap_a"]?.double, let b = d["overlap_b"]?.double {
            out.append(("Shared area", "\(Int((a * 100).rounded()))% / \(Int((b * 100).rounded()))%"))
        }
        if d["mirrored"] != nil || d["rotation"] != nil || d["scale"] != nil {
            var t: [String] = []
            if d["mirrored"]?.bool == true { t.append("mirrored") }
            if let r = d["rotation"]?.double, abs(r) > 3 { t.append("\(Int(r.rounded()))°") }
            if let s = d["scale"]?.double, abs(s - 1) > 0.05 { t.append(String(format: "×%.2f", s)) }
            out.append(("Transform", t.isEmpty ? "none" : t.joined(separator: ", ")))
        }
        for k in d.keys.sorted() where !Self.known.contains(k) {
            guard let v = d[k] else { continue }
            switch v {
            case .null, .other: continue
            case .array(let a) where a.count > 6: out.append((Self.name(k), "\(a.count) values"))
            default: out.append((Self.name(k), String(v.display.prefix(80))))
            }
        }
        return out
    }

    private static func name(_ key: String) -> String {
        let s = key.replacingOccurrences(of: "_", with: " ")
        return s.prefix(1).uppercased() + s.dropFirst()
    }

    var body: some View {
        let rows = rows
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("Measurements").font(.headline)
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                    ForEach(rows, id: \.0) { r in
                        GridRow {
                            Text(r.0).foregroundStyle(.secondary)
                            Text(r.1).monospacedDigit().textSelection(.enabled)
                        }
                    }
                }
                .font(.callout)
            }
        }
    }
}

/// An evidence picture: Quick Look on double-click, plus copy, save and drag.
struct EvidenceImage: View {
    let runID: String
    let name: String
    let animated: Bool

    @Environment(EngineController.self) private var engine
    @State private var image: NSImage?
    @State private var failed = false
    @State private var quickLook: URL?

    var body: some View {
        Group {
            if let image {
                Group {
                    if animated {
                        AnimatedImage(image: image)
                            .aspectRatio(image.size.width / max(image.size.height, 1), contentMode: .fit)
                    } else {
                        Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
                            .draggable(Image(nsImage: image))
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .onTapGesture(count: 2) { preview() }
                .contextMenu {
                    Button("Quick Look") { preview() }
                    Button("Copy Image") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.writeObjects([image])
                    }
                    Button("Save Image As…") { save() }
                }
                .help("Double-click for Quick Look")
            } else if failed {
                Label("Evidence image unavailable", systemImage: "photo.badge.exclamationmark")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 80)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(animated ? "Animation alternating the two regions" : "Evidence image \(name)")
        .accessibilityAddTraits(.isImage)
        .accessibilityAction(named: "Quick Look") { preview() }
        .quickLookPreview($quickLook)
        .task(id: name) {
            let img = await ImageStore.shared.load(engine.client.evidenceURL(runID, name))
            image = img
            failed = img == nil
        }
    }

    private func preview() {
        Task { quickLook = try? await FileFetcher.local(engine.client.evidenceURL(runID, name), run: runID, name: name) }
    }

    private func save() {
        Task {
            guard let local = try? await FileFetcher.local(engine.client.evidenceURL(runID, name), run: runID, name: name) else { return }
            Exporter.saveCopy(of: local, suggestedName: name)
        }
    }
}

struct AnimatedImage: NSViewRepresentable {
    let image: NSImage
    func makeNSView(context: Context) -> NSImageView {
        let v = NSImageView()
        v.animates = !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        v.imageScaling = .scaleProportionallyUpOrDown
        v.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        v.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        return v
    }
    func updateNSView(_ v: NSImageView, context: Context) { if v.image !== image { v.image = image } }
}

/// A spreadsheet, document or other supplementary file a finding points into.
struct SupplementRow: View {
    let runID: String
    let loc: Loc
    @Environment(EngineController.self) private var engine
    @State private var quickLook: URL?
    @State private var error: AppError?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: "tablecells").foregroundStyle(.secondary).accessibilityHidden(true)
                Text(loc.file ?? "Article").lineLimit(1).truncationMode(.middle)
                Spacer()
                if loc.file != nil {
                    Button { open(quickLookOnly: true) } label: { Label("Quick Look", systemImage: "eye") }
                        .labelStyle(.iconOnly).buttonStyle(.borderless).help("Quick Look")
                    Button { open(quickLookOnly: false) } label: { Label("Open", systemImage: "arrow.up.forward.app") }
                        .labelStyle(.iconOnly).buttonStyle(.borderless).help("Open in its app")
                }
            }
            let place = [loc.sheet.map { s in s.hasPrefix("page ") || s.hasPrefix("Table") ? "“\(s)”" : "sheet “\(s)”" },
                         loc.cells.map { "cells \($0)" }].compactMap { $0 }.joined(separator: ", ")
            if !place.isEmpty { Text(place).font(.caption).foregroundStyle(.secondary) }
            if let error { Text(error.message).font(.caption).foregroundStyle(Theme.critical) }
        }
        .font(.callout)
        .quickLookPreview($quickLook)
    }

    private func open(quickLookOnly: Bool) {
        guard let name = loc.file else { return }
        Task {
            do {
                let local = try await FileFetcher.local(engine.client.supplementURL(runID, name), run: runID, name: name)
                error = nil
                if quickLookOnly { quickLook = local } else { NSWorkspace.shared.open(local) }
            } catch {
                self.error = AppError.from(error)
            }
        }
    }
}

struct ManualCompare: View {
    let runID: String
    let a: String
    let b: String
    let clear: () -> Void

    @Environment(EngineController.self) private var engine
    @Environment(\.openWindow) private var openWindow
    @State private var result: CompareResult?
    @State private var error: AppError?

    var body: some View {
        let client = engine.client
        VStack(alignment: .leading, spacing: 10) {
            Label("Your comparison", systemImage: "rectangle.on.rectangle").font(.caption).foregroundStyle(.secondary)
            Text("\(a) against \(b)").font(.title3.weight(.semibold))
            if let result {
                BlinkView(a: client.frameURL(runID, "a", a, b), b: client.frameURL(runID, "b", a, b),
                          diff: result.aligned ? client.frameURL(runID, "diff", a, b) : nil,
                          labelA: a, labelB: result.aligned ? "\(b), aligned" : b)
                Text(result.aligned
                     ? "These panels line up: \(result.inliers ?? 0) keypoints agree\(result.mirrored == true ? " after mirroring" : ""), and the fine texture correlates at \(String(format: "%.2f", result.ncc_texture ?? 0)) (copies score above 0.5, unrelated images near 0)."
                     : "No shared content found: the panels don’t line up under any shift, rotation, mirror or rescale.")
                    .textSelection(.enabled)
            } else if let error {
                ErrorBanner(error: error, retry: { self.error = nil; Task { await run() } })
            } else {
                ProgressView("Aligning…")
            }
            HStack {
                Button("Clear Comparison", action: clear)
                Button("Open in Blink Comparator") {
                    openWindow(id: "blink", value: BlinkSpec(run: runID, a: a, b: b, title: "\(a) against \(b)"))
                }
                .disabled(result == nil)
            }
            .controlSize(.small)
            Text("Click a picked panel again to drop it.").font(.caption).foregroundStyle(.secondary)
        }
        .task(id: "\(a)|\(b)") { await run() }
    }

    private func run() async {
        result = nil
        error = nil
        do { result = try await engine.client.compare(runID, a, b) } catch { self.error = AppError.from(error) }
    }
}

struct PaperTab: View {
    let report: Report
    var review: ReviewSummary?

    var body: some View {
        Form {
            Section("Publication") {
                LabeledContent("Title") { Text(report.meta?.title ?? report.displayTitle).textSelection(.enabled) }
                if let m = report.meta {
                    if m.isRetracted {
                        LabeledContent("Status") {
                            Label("Retracted", systemImage: "exclamationmark.octagon.fill").foregroundStyle(Theme.critical)
                        }
                    }
                    if let j = m.journal, !j.isEmpty { LabeledContent("Journal", value: j) }
                    if let i = m.issued, !i.isEmpty { LabeledContent("Published", value: i) }
                    if !m.authors.isEmpty {
                        LabeledContent("Authors") { Text(m.authors.map(\.name).joined(separator: ", ")).textSelection(.enabled) }
                    }
                    if let c = m.citedBy { LabeledContent("Cited by", value: "\(c)") }
                    ForEach(Array(m.notices.enumerated()), id: \.offset) { _, n in
                        LabeledContent("Notice", value: [n.type, n.date].compactMap { $0 }.joined(separator: ", "))
                    }
                }
                if !report.paper.doi.isEmpty {
                    LabeledContent("DOI") { Text(report.paper.doi).textSelection(.enabled) }
                }
                if report.paper.pages > 0 { LabeledContent("Pages", value: "\(report.paper.pages)") }
                if !report.tags.isEmpty { LabeledContent("Tags", value: report.tags.joined(separator: ", ")) }
            }
            Section("Screening") {
                LabeledContent("Integrity score") { ScoreBadge(score: report.score) }
                if let ai = report.scoreAI { LabeledContent("After AI review") { ScoreBadge(score: ai, aiAdjusted: true) } }
                if let rv = review, rv.reviewed > 0 {
                    LabeledContent("Your review", value: "\(rv.reviewed) of \(rv.total) findings")
                    if let s = rv.scoreReviewed { LabeledContent("After your review") { ScoreBadge(score: s) } }
                }
                if !report.verdict.isEmpty { Text(report.verdict).font(.callout).foregroundStyle(.secondary) }
                LabeledContent("Figures", value: "\(report.figures.count) with \(report.panels) panels")
                if report.tables > 0 { LabeledContent("Tables", value: "\(report.tables)") }
            }
            if let r = report.references, let n = r.count, n > 0 {
                Section("References") {
                    LabeledContent("Listed", value: "\(n)")
                    LabeledContent("Found in OpenAlex", value: "\(r.resolved ?? 0)")
                    LabeledContent("Self-citations",
                                   value: "\(r.self_citations ?? 0) (\(Int(((r.self_citation_rate ?? 0) * 100).rounded()))%)")
                    LabeledContent("Retracted", value: "\(r.retracted ?? 0)")
                    LabeledContent("Couldn’t be found", value: "\(r.unmatched ?? 0)")
                }
            }
            if let members = report.caseMembers, !members.isEmpty {
                Section("Papers in This Case") {
                    ForEach(Array(members.enumerated()), id: \.offset) { i, m in
                        LabeledContent("Paper \(m.index ?? i + 1)", value: m.title ?? m.source ?? "")
                    }
                }
            }
            if !report.paper.doi.isEmpty, let url = URL(string: "https://doi.org/\(report.paper.doi)") {
                Section {
                    Link("Open DOI in Browser", destination: url)
                    Button("Copy Citation") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(citation, forType: .string)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private var citation: String {
        let m = report.meta
        var parts: [String] = []
        if let authors = m?.authors, !authors.isEmpty {
            let names = authors.prefix(3).compactMap { $0.family ?? $0.given }
            parts.append(names.joined(separator: ", ") + (authors.count > 3 ? " et al." : ""))
        }
        if let y = m?.issued?.prefix(4), !y.isEmpty { parts.append("(\(y))") }
        parts.append(report.displayTitle + ".")
        if let j = m?.journal, !j.isEmpty { parts.append(j + ".") }
        parts.append("https://doi.org/\(report.paper.doi)")
        return parts.joined(separator: " ")
    }
}

/// Every file the scan examined, with Quick Look (Space), open (double-click) and save.
struct FilesTab: View {
    let runID: String
    let report: Report

    @Environment(EngineController.self) private var engine
    @State private var selection: String?
    @State private var quickLook: URL?
    @State private var error: AppError?

    var body: some View {
        VStack(spacing: 0) {
            List(selection: $selection) {
                ForEach(Array(report.sources.enumerated()), id: \.element.id) { i, s in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(i == 0 ? "\(s.label): \(s.name)" : s.name).lineLimit(1).truncationMode(.middle)
                        HStack(spacing: 4) {
                            if s.notExamined {
                                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.medium)
                                    .accessibilityLabel("Not examined")
                            }
                            Text(detail(s)).lineLimit(2)
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    .tag(Optional(s.id))
                }
            }
            .contextMenu(forSelectionType: String.self) { ids in
                if let id = ids.first {
                    Button("Quick Look") { fetch(id) { quickLook = $0 } }
                    Button("Open") { fetch(id) { NSWorkspace.shared.open($0) } }
                    Button("Save a Copy As…") { fetch(id) { Exporter.saveCopy(of: $0, suggestedName: id) } }
                    Button("Show in Finder") { fetch(id) { NSWorkspace.shared.activateFileViewerSelecting([$0]) } }
                }
            } primaryAction: { ids in
                if let id = ids.first { fetch(id) { NSWorkspace.shared.open($0) } }
            }
            .onKeyPress(.space) {
                guard let id = selection else { return .ignored }
                fetch(id) { quickLook = $0 }
                return .handled
            }
            .quickLookPreview($quickLook)
            .overlay {
                if report.sources.isEmpty {
                    ContentUnavailableView("No File List", systemImage: "doc", description: Text("This scan didn’t record which files it examined."))
                }
            }
            if let error {
                ErrorBanner(error: error, dismiss: { self.error = nil }).padding(8)
            }
        }
    }

    private func detail(_ s: SourceInfo) -> String {
        var parts = [s.status]
        if s.figures > 0 { parts.append("\(s.figures) images") }
        if s.tables > 0 { parts.append("\(s.tables) tables") }
        if !s.origin.isEmpty { parts.append(s.origin) }
        return parts.joined(separator: ", ")
    }

    private func fetch(_ name: String, then: @escaping (URL) -> Void) {
        let isArticle = report.sources.first?.name == name
        Task {
            do {
                let local = isArticle ? try await FileFetcher.source(run: runID)
                    : try await FileFetcher.local(engine.client.supplementURL(runID, name), run: runID, name: name)
                error = nil
                then(local)
            } catch {
                self.error = AppError.from(error)
            }
        }
    }
}
