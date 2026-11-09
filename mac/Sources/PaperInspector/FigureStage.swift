import AppKit
import PDFKit
import SwiftUI

/// The detail column: a strip of every figure, then the selected figure (or the paper page a text finding is
/// about) on a dark stage.
struct FigureStage: View {
    let model: RunModel
    let viewMode: ViewMode
    let aiJob: ScanQueue.Job?

    @Environment(EngineController.self) private var engine
    @Environment(ScanQueue.self) private var queue
    @State private var captionExpanded = false

    var body: some View {
        VStack(spacing: 0) {
            if let job = aiJob, let failure = job.failure {
                ErrorBanner(error: failure, retry: { queue.retry(job.id) }, dismiss: { queue.dismiss(job.id) })
                    .padding(8)
            }
            VStack(spacing: 0) {
                if let r = model.report, !r.figures.isEmpty {
                    FigureStrip(model: model, report: r)
                    Divider()
                }
                if let target = model.pageTarget {
                    PageView(runID: model.runID, target: target)
                } else if let fig = model.figure {
                    FigureCanvas(runID: model.runID, figure: fig, mode: viewMode, finding: model.finding,
                                 picked: model.picked, zoom: model.zoom,
                                 onFit: { if model.fitScale != $0 { model.fitScale = $0 } },
                                 onPinch: { m in model.zoom = min(8, max(0.05, (model.zoom ?? model.fitScale) * m)) },
                                 onPanel: { model.togglePick($0) })
                        .padding(10)
                    footer(fig)
                } else {
                    ContentUnavailableView("No Figures", systemImage: "photo.on.rectangle",
                                           description: Text("No figures were found in this paper. The findings are about its text, numbers and references."))
                }
            }
            .background(Theme.stage)
            .environment(\.colorScheme, .dark)
        }
        .onExitCommand { if !model.picked.isEmpty { model.picked = [] } }
        .onChange(of: model.figureID) { _, _ in captionExpanded = false }
    }

    private func footer(_ fig: FigureInfo) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(model.picked.count == 1
                 ? "\(model.picked[0]) picked. Click a second panel, in any figure, to compare them."
                 : "Click any two panels to compare them yourself.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if !fig.caption.isEmpty {
                HStack(alignment: .firstTextBaseline) {
                    Text(fig.caption)
                        .font(.system(.callout, design: .serif))
                        .foregroundStyle(.secondary)
                        .lineLimit(captionExpanded ? nil : 2)
                        .textSelection(.enabled)
                    Button(captionExpanded ? "Less" : "More") { captionExpanded.toggle() }
                        .buttonStyle(.link)
                        .font(.caption)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
    }
}

struct FigureStrip: View {
    let model: RunModel
    let report: Report
    @Environment(EngineController.self) private var engine

    var body: some View {
        let counts = model.countsByFigure
        ScrollViewReader { proxy in
            ScrollView(.horizontal) {
                LazyHStack(spacing: 8) {
                    ForEach(report.figures) { f in
                        let selected = f.id == model.figureID && model.pageTarget == nil
                        Button { model.showFigure(f.id) } label: {
                            FigureThumb(url: engine.client.figureURL(model.runID, f.id), label: f.label,
                                        count: counts[f.id] ?? 0, selected: selected)
                        }
                        .buttonStyle(.plain)
                        .id(f.id)
                        .help(f.caption.isEmpty ? f.label : String(f.caption.prefix(200)))
                        .accessibilityLabel("\(f.label), \(counts[f.id] ?? 0) findings")
                        .accessibilityAddTraits(selected ? .isSelected : [])
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }
            .scrollIndicators(.hidden)
            .onChange(of: model.figureID) { _, id in if let id { proxy.scrollTo(id, anchor: .center) } }
        }
        .frame(height: 86)
    }
}

private struct FigureThumb: View {
    let url: URL
    let label: String
    let count: Int
    let selected: Bool
    @State private var image: NSImage?

    var body: some View {
        VStack(spacing: 3) {
            ZStack(alignment: .topTrailing) {
                Group {
                    if let image {
                        Image(nsImage: image).resizable().interpolation(.medium).scaledToFit()
                    } else {
                        Color.white.opacity(0.06)
                    }
                }
                .frame(width: 72, height: 54)
                .clipShape(RoundedRectangle(cornerRadius: 4))
                .overlay {
                    RoundedRectangle(cornerRadius: 4)
                        .strokeBorder(selected ? Color.accentColor : Color.white.opacity(0.15), lineWidth: selected ? 2 : 1)
                }
                if count > 0 {
                    Text("\(count)")
                        .font(.caption2.weight(.bold))
                        .monospacedDigit()
                        .foregroundStyle(Theme.onBadge)
                        .padding(.horizontal, 5)
                        .background(Theme.high, in: Capsule())
                        .offset(x: 4, y: -4)
                }
            }
            Text(label).font(.caption2).lineLimit(1).foregroundStyle(selected ? .primary : .secondary)
        }
        .frame(width: 80)
        .contentShape(Rectangle())
        .task(id: url) { image = await ImageStore.shared.load(url) }
    }
}

/// A figure with its panels outlined and the selected finding's regions marked: A solid cyan, B dashed magenta.
/// Panels can be clicked (or activated with VoiceOver) to pick them for a manual comparison.
struct FigureCanvas: View {
    let runID: String
    let figure: FigureInfo
    let mode: ViewMode
    let finding: Finding?
    let picked: [String]
    let zoom: CGFloat?
    let onFit: (CGFloat) -> Void
    let onPinch: (CGFloat) -> Void
    let onPanel: (String) -> Void

    @Environment(EngineController.self) private var engine
    @State private var image: NSImage?
    @State private var loadedFigure: String?
    @State private var failed = false
    @State private var loading = false
    @State private var attempt = 0
    @GestureState private var pinch: CGFloat = 1

    private var url: URL { engine.client.figureURL(runID, figure.id, mode: mode) }

    var body: some View {
        GeometryReader { geo in
            let fit = fitScale(geo.size)
            let s = max(0.01, (zoom ?? fit) * pinch)
            let w = figure.hasSize ? figure.width * s : geo.size.width
            let h = figure.hasSize ? figure.height * s : geo.size.height
            ScrollView([.horizontal, .vertical]) {
                canvas(scale: s, width: w, height: h)
                    .frame(width: max(w, geo.size.width), height: max(h, geo.size.height))
            }
            .scrollIndicators(zoom == nil ? .hidden : .automatic)
            .onAppear { onFit(fit) }
            .onChange(of: fit) { _, v in onFit(v) }
        }
        .gesture(MagnifyGesture()
            .updating($pinch) { value, state, _ in state = value.magnification }
            .onEnded { onPinch($0.magnification) })
        .task(id: "\(url.absoluteString)#\(attempt)") { await load() }
    }

    private func fitScale(_ size: CGSize) -> CGFloat {
        guard figure.hasSize, size.width > 1, size.height > 1 else { return 1 }
        let s = min(size.width / figure.width, size.height / figure.height)
        return s.isFinite && s > 0 ? s : 1
    }

    @ViewBuilder private func canvas(scale s: CGFloat, width w: CGFloat, height h: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            if let image {
                if figure.hasSize {
                    Image(nsImage: image).resizable().interpolation(.medium).frame(width: w, height: h)
                } else {
                    Image(nsImage: image).resizable().interpolation(.medium).scaledToFit().frame(width: w, height: h)
                }
            } else {
                Color.white.opacity(0.03).frame(width: w, height: h)
            }
            if figure.hasSize {
                ForEach(figure.panels) { p in
                    if let b = p.box.rect { panel(p, b, s) }
                }
                ForEach(Array(marks.enumerated()), id: \.offset) { i, m in mark(i, m, s) }
            }
        }
        .frame(width: w, height: h)
        .overlay {
            if image == nil {
                if failed {
                    VStack(spacing: 8) {
                        Text("Couldn’t load the figure").foregroundStyle(.secondary)
                        Button("Retry") { attempt += 1 }
                    }
                } else {
                    ProgressView()
                }
            }
        }
        .overlay(alignment: .topTrailing) {
            if loading && image != nil { ProgressView().controlSize(.small).padding(6) }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(figure.label), \(mode.label) view")
    }

    private func panel(_ p: PanelInfo, _ b: CGRect, _ s: CGFloat) -> some View {
        let isPicked = picked.contains(p.id)
        return Rectangle()
            .strokeBorder(isPicked ? Color.white : Color.white.opacity(0.18),
                          style: StrokeStyle(lineWidth: isPicked ? 2.5 : 1, dash: isPicked ? [6, 4] : []))
            .frame(width: b.width * s, height: b.height * s)
            .contentShape(Rectangle())
            .offset(x: b.minX * s, y: b.minY * s)
            .onTapGesture { onPanel(p.id) }
            .help("Panel \(p.id) (\(p.kind)). Click to pick it for a comparison.")
            .accessibilityElement()
            .accessibilityLabel("Panel \(p.id), \(p.kind)")
            .accessibilityHint("Picks the panel for a side-by-side comparison")
            .accessibilityAddTraits(isPicked ? [.isButton, .isSelected] : .isButton)
            .accessibilityAction { onPanel(p.id) }
    }

    private func mark(_ i: Int, _ m: CGRect, _ s: CGFloat) -> some View {
        let isA = i % 2 == 0
        let color = isA ? Theme.markA : Theme.markB
        return Rectangle()
            .stroke(color, style: StrokeStyle(lineWidth: 3, dash: isA ? [] : [8, 5]))
            .frame(width: m.width * s, height: m.height * s)
            .overlay(alignment: .topLeading) {
                if marks.count > 1 {
                    Text(isA ? "A" : "B")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.black)
                        .padding(.horizontal, 3)
                        .background(color)
                }
            }
            .offset(x: m.minX * s, y: m.minY * s)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    /// The finding's regions in figure coordinates.  Boxes are panel-relative when a panel is named.
    private var marks: [CGRect] {
        guard let finding else { return [] }
        return finding.locations.compactMap { l in
            guard l.figure == figure.id else { return nil }
            if let pid = l.panel {
                guard let pb = figure.panels.first(where: { $0.id == pid })?.box.rect else { return nil }
                let b = l.box?.rect ?? CGRect(origin: .zero, size: pb.size)
                return b.offsetBy(dx: pb.minX, dy: pb.minY)
            }
            return l.box?.rect
        }
    }

    /// A new figure starts blank; a new view of the same figure keeps the old picture until the new one arrives.
    private func load() async {
        let u = url
        if loadedFigure != figure.id { image = nil }
        failed = false
        loading = true
        let img = await ImageStore.shared.load(u)
        guard !Task.isCancelled else { return }
        loading = false
        if let img {
            image = img
            loadedFigure = figure.id
        } else {
            image = nil
            loadedFigure = nil
            failed = true
        }
    }
}

/// The page of the paper a text finding points to, with the quoted passage highlighted.
struct PageView: View {
    let runID: String
    let target: Loc
    @State private var file: URL?
    @State private var error: AppError?

    var body: some View {
        VStack(spacing: 0) {
            if let q = target.quote, !q.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    if let p = target.page { Text("Page \(p)").font(.caption).foregroundStyle(.secondary) }
                    Text(q).font(.system(.callout, design: .serif)).textSelection(.enabled).lineLimit(4)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
            }
            Group {
                if let file, file.pathExtension.lowercased() == "pdf" {
                    PDFPageView(url: file, page: target.page, quote: target.quote)
                } else if file != nil {
                    ContentUnavailableView("No Page to Show", systemImage: "doc.text",
                                           description: Text("This scan was made from an image, not a PDF."))
                } else if let error {
                    ErrorBanner(error: error, retry: { self.error = nil; Task { await fetch() } }).padding()
                } else {
                    ProgressView("Opening the paper…")
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .task(id: runID) { await fetch() }
    }

    private func fetch() async {
        do { file = try await FileFetcher.source(run: runID) } catch { self.error = AppError.from(error) }
    }
}

struct PDFPageView: NSViewRepresentable {
    let url: URL
    let page: Int?
    let quote: String?

    final class Coordinator { var shown: String? }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.displayMode = .singlePageContinuous
        v.displaysPageBreaks = true
        v.setAccessibilityLabel("Paper page")
        return v
    }

    func updateNSView(_ v: PDFView, context: Context) {
        if v.document?.documentURL != url { v.document = PDFDocument(url: url); context.coordinator.shown = nil }
        guard let doc = v.document, doc.pageCount > 0 else { return }
        let key = "\(page ?? -1)|\(quote ?? "")"
        guard context.coordinator.shown != key else { return }
        context.coordinator.shown = key
        let index = max(0, min(doc.pageCount - 1, (page ?? 1) - 1))  // quote pages are 1-based
        var hit: PDFSelection?
        if let needle = Self.needle(quote) {
            let found = doc.findString(needle, withOptions: [.caseInsensitive])
            hit = found.first { s in s.pages.contains { doc.index(for: $0) == index } } ?? found.first
        }
        if let hit {
            hit.color = .systemYellow
            v.highlightedSelections = [hit]
            v.go(to: hit)
        } else {
            v.highlightedSelections = nil
            if let p = doc.page(at: index) { v.go(to: p) }
        }
    }

    /// The longest unbroken run of the quote (the engine adds "…" around context), short enough to match.
    static func needle(_ quote: String?) -> String? {
        guard let quote else { return nil }
        let piece = quote.components(separatedBy: CharacterSet(charactersIn: "…\n"))
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .max { $0.count < $1.count } ?? ""
        let words = piece.split(separator: " ").prefix(8).joined(separator: " ")
        return words.count >= 6 ? words : nil
    }
}
