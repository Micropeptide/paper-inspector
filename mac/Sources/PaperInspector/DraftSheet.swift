import AppKit
import SwiftUI

/// A starting point for a comment to the authors or on PubPeer.  Edits are kept per run, so closing the sheet
/// loses nothing.
struct DraftSheet: View {
    let runID: String

    @Environment(EngineController.self) private var engine
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var original = ""
    @State private var loading = true
    @State private var error: AppError?
    @State private var copied = false
    @State private var confirmRegenerate = false

    private var draftFile: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        return base.appendingPathComponent("PaperInspector/drafts/\(FileFetcher.safe(runID)).md")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Draft Comment").font(.title2.weight(.semibold))
            Text("A starting point. Check every item yourself before sending it anywhere, and keep the tone a question, not an accusation.")
                .font(.callout).foregroundStyle(.secondary)
            Group {
                if loading {
                    ProgressView("Writing the draft…").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let error {
                    ErrorBanner(error: error, retry: { Task { await generate() } })
                        .frame(maxHeight: .infinity, alignment: .top)
                } else {
                    TextEditor(text: $text)
                        .font(.system(.body, design: .serif))
                        .scrollContentBackground(.hidden)
                        .padding(4)
                        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                        .accessibilityLabel("Draft comment")
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
                        .contentTransition(.symbolEffect(.replace))
                }
                ShareLink(item: text) { Label("Share", systemImage: "square.and.arrow.up") }
                Button("Save As…") { Exporter.saveText(text, suggestedName: "comment-\(runID).txt") }
                Button("Regenerate") {
                    if text != original { confirmRegenerate = true } else { Task { await generate() } }
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            .disabled(loading)
        }
        .padding(20)
        .frame(idealWidth: 680, idealHeight: 520)
        .task { await start() }
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            copied = false
        }
        .task(id: text) {
            // Save after a second of no typing.
            guard !loading, error == nil else { return }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard !Task.isCancelled else { return }
            save()
        }
        .onDisappear { if !loading && error == nil { save() } }
        .confirmationDialog("Replace your edits with a fresh draft?", isPresented: $confirmRegenerate) {
            Button("Replace", role: .destructive) { Task { await generate() } }
        }
    }

    private func start() async {
        if let saved = try? String(contentsOf: draftFile, encoding: .utf8), !saved.isEmpty {
            text = saved
            loading = false
            original = (try? await engine.client.pubpeer(runID)) ?? saved
            return
        }
        await generate()
    }

    private func generate() async {
        loading = true
        error = nil
        do {
            let t = try await engine.client.pubpeer(runID)
            text = t
            original = t
        } catch {
            self.error = AppError.from(error)
        }
        loading = false
    }

    private func save() {
        let fm = FileManager.default
        if text == original || text.isEmpty { try? fm.removeItem(at: draftFile); return }
        try? fm.createDirectory(at: draftFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? text.write(to: draftFile, atomically: true, encoding: .utf8)
    }
}
