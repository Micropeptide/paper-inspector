import AppKit
import SwiftUI

/// The reviewer's ruling on the selected finding, with a note.  Saved to the engine, which keeps it with the paper.
struct RulingSection: View {
    let model: RunModel
    let finding: Finding

    @State private var note = ""
    @State private var savedNote = ""
    @FocusState private var noteFocused: Bool

    var body: some View {
        let current = model.ruling(for: finding.id)
        VStack(alignment: .leading, spacing: 8) {
            Text("Your Ruling").font(.headline)
            Picker("Your ruling", selection: Binding(get: { current.ruling },
                                                     set: { r in Task { await model.setRuling(r, for: finding.id) } })) {
                ForEach(Ruling.allCases) { r in
                    Text(r == .unreviewed ? "None" : r.label).tag(r).help(r.help)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .disabled(!model.supportsRulings)
            Text(current.ruling.help).font(.caption).foregroundStyle(.secondary)
            TextField("Note", text: $note, prompt: Text("What you saw, or what you'd ask for"), axis: .vertical)
                .lineLimit(2...6)
                .focused($noteFocused)
                .onSubmit { commit() }
                .disabled(!model.supportsRulings)
                .accessibilityLabel("Note on this ruling")
            if let e = model.rulingError {
                Label(e.message, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.critical)
            }
            Text(footer).font(.caption).foregroundStyle(.secondary)
        }
        .onAppear { note = current.note; savedNote = current.note }
        .onChange(of: current.note) { _, v in
            // A note saved elsewhere (or the engine's trimmed copy) replaces the field unless the reader is typing.
            if !noteFocused || note == savedNote { note = v }
            savedNote = v
        }
        .onChange(of: noteFocused) { _, focused in if !focused { commit() } }
        .onChange(of: model.noteFocusToken) { _, _ in noteFocused = true }
        .task(id: note) {
            // Save after a pause in typing; every save is kept in the ruling's history, so not on each key.
            guard note != savedNote else { return }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard !Task.isCancelled else { return }
            commit()
        }
        .onDisappear { commit() }
    }

    private var footer: String {
        guard model.supportsRulings else {
            return "This engine doesn’t keep rulings. Update Paper Inspector to record them."
        }
        if let updated = model.ruling(for: finding.id).updated, let d = parseEngineDate(updated) {
            return "Saved \(d.formatted(.relative(presentation: .named))). Kept with the paper, so a rescan keeps it. ⌘1 ⌘2 ⌘3 set a ruling."
        }
        return "Kept with the paper, so a rescan keeps it. ⌘1 Confirmed, ⌘2 False Positive, ⌘3 Needs Raw Data."
    }

    private func commit() {
        let v = note
        guard v.trimmingCharacters(in: .whitespacesAndNewlines) != savedNote else { return }
        savedNote = v.trimmingCharacters(in: .whitespacesAndNewlines)
        Task { await model.setNote(v, for: finding.id) }
    }
}

/// "What this means": the catalogue's plain explanation of the check behind a finding.
struct CheckExplanation: View {
    let check: String
    @Environment(CatalogStore.self) private var catalog
    @AppStorage("showExplanation") private var expanded = true

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            if let info = catalog.info(check) {
                VStack(alignment: .leading, spacing: 10) {
                    part("What the check does", info.what)
                    part("Innocent explanations", info.falseAlarms)
                    part("What to look at", info.lookAt)
                    part("What you could ask the authors", info.ask)
                    Label(info.scored ? "Counts toward the integrity score." : "Listed for context. It does not change the score.",
                          systemImage: info.scored ? "gauge.with.dots.needle.33percent" : "info.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 6)
                .textSelection(.enabled)
            } else {
                Text("No explanation is available for this check.").font(.callout).foregroundStyle(.secondary)
                    .padding(.top, 6)
            }
        } label: {
            Text("What This Means").font(.headline)
        }
        .task(id: check) { await catalog.ensure(check) }
    }

    @ViewBuilder private func part(_ title: String, _ text: String) -> some View {
        if !text.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// A small capsule naming the reviewer's ruling, for the findings list.
struct RulingBadge: View {
    let ruling: Ruling

    var body: some View {
        if ruling != .unreviewed {
            Label(ruling.label, systemImage: ruling.symbol)
                .labelStyle(.titleAndIcon)
                .font(.caption2.weight(.medium))
                .foregroundStyle(ruling.color)
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(ruling.color.opacity(0.12), in: Capsule())
                .fixedSize()
                .help("You ruled this \(ruling.phrase)")
                .accessibilityLabel("Your ruling: \(ruling.label)")
        }
    }
}

/// "Reviewed 3 of 12 · score after review 40", above the findings.
struct ReviewProgress: View {
    let review: ReviewSummary
    let score: Int

    var body: some View {
        HStack(spacing: 8) {
            ProgressView(value: Double(review.reviewed), total: Double(max(review.total, 1)))
                .frame(width: 60)
                .controlSize(.small)
            Text("Reviewed \(review.reviewed) of \(review.total)")
            if let s = review.scoreReviewed, review.reviewed > 0, s != score {
                Text("· score after review \(s)").foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if review.unmatched > 0 {
                Image(systemName: "arrow.triangle.branch")
                    .foregroundStyle(.secondary)
                    .help("\(review.unmatched) earlier ruling\(review.unmatched == 1 ? "" : "s") matched nothing in this scan. A newer engine may have changed what it flags.")
            }
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}

/// A drafted letter to the editor or the authors, built from the findings the reviewer confirmed.
struct LetterSheet: View {
    let runID: String
    let kind: LetterKind

    @Environment(EngineController.self) private var engine
    @Environment(\.dismiss) private var dismiss
    @AppStorage("letterIncludesNeedsData") private var includeNeedsData = true
    @State private var letter: Letter?
    @State private var text = ""
    @State private var edited = false
    @State private var error: AppError?
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(kind.title).font(.title2.weight(.semibold))
            Text("Built from the findings you marked Confirmed. Fill in the [bracketed] parts, check every point "
                 + "against the paper, and keep the tone a question, not an accusation.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Toggle("Also list findings that need the raw data", isOn: $includeNeedsData)
            if let letter, letter.empty {
                Label("No finding is marked Confirmed yet, so the letter has no points to raise. Rule on the findings first (⌘1).",
                      systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(Theme.medium)
            }
            Group {
                if let error {
                    ErrorBanner(error: error, retry: { Task { await load() } })
                        .frame(maxHeight: .infinity, alignment: .top)
                } else if letter == nil {
                    ProgressView("Writing the letter…").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    TextEditor(text: $text)
                        .font(.system(.body, design: .serif))
                        .scrollContentBackground(.hidden)
                        .padding(4)
                        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                        .accessibilityLabel(kind.title)
                        .onChange(of: text) { _, v in edited = v != letter?.text }
                }
            }
            .frame(minWidth: 620, minHeight: 360)
            HStack {
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                    copied = true
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                }
                Button("Save As…") {
                    Exporter.saveText(text, suggestedName: "paperinspector-\(FileFetcher.safe(runID))-letter-\(kind.rawValue).txt")
                }
                if edited {
                    Button("Start Over") { if let l = letter { text = l.text } }
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            .disabled(letter == nil)
        }
        .padding(20)
        .frame(idealWidth: 680, idealHeight: 560)
        .task(id: includeNeedsData) { await load() }
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            copied = false
        }
    }

    private func load() async {
        error = nil
        do {
            let l = try await engine.client.letter(run: runID, kind: kind, includeNeedsData: includeNeedsData)
            // A regenerated letter replaces the text only if the reader hasn't edited it ("Start Over" applies it).
            let first = letter == nil
            letter = l
            if first || !edited { text = l.text }
            edited = text != l.text
        } catch {
            self.error = AppError.from(error)
        }
    }
}
