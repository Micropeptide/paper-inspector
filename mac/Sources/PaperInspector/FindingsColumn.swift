import AppKit
import SwiftUI

/// The middle column: the run's findings, grouped and filterable.
struct FindingsColumn: View {
    let model: RunModel
    let filter: FindingFilter
    @Binding var category: String
    @Binding var showNotes: Bool
    @Binding var sortRaw: String
    @Binding var rulingFilter: String
    @Binding var query: String

    var body: some View {
        let groups = model.grouped(filter)
        List(selection: Binding(get: { model.selectedFinding },
                                set: { id in model.select(model.report?.finding(id)) })) {
            if let ai = model.report?.aiReview, !ai.summary.isEmpty {
                Section {
                    DisclosureGroup("Summary") {
                        Text(ai.summary).font(.callout).textSelection(.enabled)
                    }
                } header: {
                    Label("AI Investigator · \(ai.model)", systemImage: "sparkles")
                }
            }
            ForEach(groups, id: \.group) { g in
                Section {
                    ForEach(g.findings) { f in
                        FindingRow(finding: f, ruling: model.ruling(for: f.id).ruling)
                            .tag(Optional(f.id))
                            .id(f.id)
                    }
                } header: {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(g.group.title)
                        Text(g.group.caption).font(.caption).foregroundStyle(.secondary).textCase(nil)
                    }
                }
            }
        }
        .overlay { if groups.isEmpty { empty } }
        .safeAreaInset(edge: .top, spacing: 0) { filterBar }
        .searchable(text: $query, placement: .toolbar, prompt: "Search findings")
        .onCopyCommand {
            guard let f = model.finding else { return [] }
            return [NSItemProvider(object: f.plainText as NSString)]
        }
        .accessibilityRotor("Integrity Signals") {
            ForEach(groups.first { $0.group == .integrity }?.findings ?? []) { f in
                AccessibilityRotorEntry(f.title, id: f.id)
            }
        }
        .onChange(of: model.runID, initial: true) { _, _ in
            if category != "all", model.categoryCounts(showNotes: true)[category] == nil { category = "all" }
        }
    }

    @ViewBuilder private var empty: some View {
        let total = model.report?.findings.filter { showNotes || !$0.severity.isNote }.count ?? 0
        if total == 0 {
            ContentUnavailableView("Nothing Flagged", systemImage: "checkmark.shield",
                                   description: Text("The automatic checks found nothing here. That is not proof the paper is clean."))
        } else {
            ContentUnavailableView("No Matching Findings", systemImage: "line.3.horizontal.decrease.circle",
                                   description: Text("Change the search or filters to see the other \(total)."))
        }
    }

    private var rulingCounts: [Ruling: Int] { model.rulingCounts(filter) }

    private var filterBar: some View {
        VStack(spacing: 6) {
            filterRow
            if let review = model.review, review.total > 0 {
                ReviewProgress(review: review, score: model.report?.score ?? 0)
            }
            if rulingFilter != "all", let r = Ruling(rawValue: rulingFilter) {
                HStack {
                    Label("Showing only: \(r.label)", systemImage: r.symbol).font(.caption)
                    Spacer()
                    Button("Show All") { rulingFilter = "all" }.controlSize(.small).buttonStyle(.link)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.bar)
    }

    private var filterRow: some View {
        let counts = model.categoryCounts(showNotes: showNotes)
        let total = counts.values.reduce(0, +)
        return HStack(spacing: 8) {
            Picker("Category", selection: $category) {
                Text("All Kinds (\(total))").tag("all")
                ForEach(categoryNames.filter { counts[$0.id] != nil || $0.id == category }, id: \.id) { c in
                    Text("\(c.name) (\(counts[c.id] ?? 0))").tag(c.id)
                }
            }
            .labelsHidden()
            .fixedSize()
            Spacer()
            Menu {
                Toggle("Show Notes", isOn: $showNotes)
                Picker("Your Ruling", selection: $rulingFilter) {
                    Text("All Rulings").tag("all")
                    ForEach(Ruling.allCases) { r in
                        Text("\(r.label) (\(rulingCounts[r] ?? 0))").tag(r.rawValue)
                    }
                }
                Picker("Sort By", selection: $sortRaw) {
                    ForEach(FindingSort.allCases) { s in Text(s.label).tag(s.rawValue) }
                }
            } label: {
                Label("Filter and Sort", systemImage: filtering ? "line.3.horizontal.decrease.circle.fill"
                                                                : "line.3.horizontal.decrease.circle")
            }
            .labelStyle(.iconOnly)
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Filter and sort the findings")
        }
    }

    private var filtering: Bool { showNotes || rulingFilter != "all" || sortRaw != FindingSort.severity.rawValue }
}

struct FindingRow: View {
    let finding: Finding
    let ruling: Ruling

    private var dimmed: Bool { ruling == .falsePositive || (ruling == .unreviewed && finding.ai?.isFalsePositive == true) }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: finding.severity.symbol)
                .foregroundStyle(finding.severity.color)
                .frame(width: 16)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(finding.title).lineLimit(3)
                    .strikethrough(ruling == .falsePositive, color: .secondary)
                HStack(spacing: 6) {
                    RulingBadge(ruling: ruling)
                    Text(caption).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            if let ai = finding.ai {
                Image(systemName: ai.isFalsePositive ? "xmark.seal" : "checkmark.seal")
                    .foregroundStyle(ai.isFalsePositive ? Theme.calm : Color.secondary)
                    .help("AI investigator: \(ai.label)")
            }
        }
        .padding(.vertical, 2)
        .opacity(dimmed ? 0.55 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var caption: String {
        finding.confidence > 0 ? "\(finding.kindName) · \(Int((finding.confidence * 100).rounded()))%" : finding.kindName
    }

    private var accessibilityText: String {
        var s = "\(finding.severity.label), \(finding.kindName): \(finding.title)"
        if finding.confidence > 0 { s += ", confidence \(Int((finding.confidence * 100).rounded())) percent" }
        if let ai = finding.ai { s += ", AI verdict \(ai.label)" }
        if ruling != .unreviewed { s += ", your ruling \(ruling.label)" }
        return s
    }
}
