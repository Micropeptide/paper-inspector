import AppKit
import SwiftUI

/// The starting point when no scan is selected.
struct HomeView: View {
    let sceneID: UUID
    let focusToken: Int
    let openFiles: () -> Void

    @Environment(ScanQueue.self) private var queue
    @Environment(BatchQueueModel.self) private var batch
    @Environment(EngineController.self) private var engine
    @State private var doi = ""
    @FocusState private var doiFocused: Bool
    @SceneStorage("homeMode") private var several = false
    @State private var batchText = ""
    @State private var batchName = ""
    @AppStorage("batchOnline") private var batchOnline = true
    @State private var adding = false
    @State private var batchResult: String?
    @State private var batchError: AppError?

    private var trimmed: String { doi.trimmingCharacters(in: .whitespacesAndNewlines) }
    /// Several DOIs pasted into the single field go to the queue instead.
    private var pastedMany: Int { let n = BatchInput.identifiers(in: doi).count; return n > 1 ? n : 0 }
    private var batchCount: Int { BatchInput.identifiers(in: batchText).count }

    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                Image(nsImage: NSApp.applicationIconImage)
                    .resizable()
                    .frame(width: 64, height: 64)
                    .accessibilityHidden(true)
                Text("Inspect a Paper").font(.largeTitle.weight(.semibold))
                Text("Paste a DOI, or drop the PDF with its supplementary files. Paper Inspector checks figures, numbers, "
                     + "data and references the way a careful reviewer would, and shows the evidence for every flag.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Picker("Papers", selection: $several) {
                    Text("One Paper").tag(false)
                    Text("Several Papers").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()

                if several { batchForm } else { singleForm }

                HStack(spacing: 16) {
                    Button("Open Files…", action: openFiles)
                        .buttonStyle(.bordered)
                        .help("Choose a PDF and its supplementary files (⌘O)")
                    Button("Try the Demo Paper") { queue.start(.demo, title: "Demo paper", origin: sceneID) }
                        .buttonStyle(.link)
                }
                .disabled(!engine.isReady)

                if !queue.scans.isEmpty {
                    VStack(spacing: 10) {
                        ForEach(queue.scans) { job in
                            InProgressRow(job: job)
                                .padding(12)
                                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }

                Text("Files you add stay on this Mac. A DOI is looked up in public databases (Crossref, OpenAlex, "
                     + "PubMed Central). The AI investigator sends figures to the model you choose, and only when you ask.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: 560)
            .padding(40)
            .frame(maxWidth: .infinity)
        }
        .onChange(of: focusToken) { _, _ in several = false; doiFocused = true }
    }

    private var singleForm: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                TextField("DOI", text: $doi, prompt: Text("10.1371/journal.pbio.1002106 or a doi.org link"))
                    .textFieldStyle(.roundedBorder)
                    .controlSize(.large)
                    .focused($doiFocused)
                    .onSubmit(inspect)
                    .accessibilityLabel("DOI")
                let recent = RecentDOIs.all
                if !recent.isEmpty {
                    Menu {
                        ForEach(recent, id: \.self) { d in Button(d) { doi = d } }
                    } label: {
                        Label("Recent DOIs", systemImage: "clock.arrow.circlepath")
                    }
                    .labelStyle(.iconOnly)
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .help("Recent DOIs")
                }
                Button(pastedMany > 0 ? "Queue \(pastedMany)" : "Inspect", action: inspect)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(trimmed.isEmpty || !engine.isReady)
            }
            if pastedMany > 0 {
                Text("That looks like \(pastedMany) papers. They’ll go to the batch queue and be scanned one at a time.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var batchForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Paste DOIs, PubMed or arXiv ids, or article links, one per line. They are scanned one at a time, "
                 + "and the queue carries on after the app restarts.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $batchText)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 120, maxHeight: 220)
                .scrollContentBackground(.hidden)
                .padding(4)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.quaternary))
                .accessibilityLabel("Papers to queue, one per line")
            HStack(spacing: 8) {
                TextField("Batch name", text: $batchName, prompt: Text("Name this batch (optional)"))
                    .textFieldStyle(.roundedBorder)
                Button("Choose a List…", action: chooseList)
                    .help("Read DOIs from a text or CSV file")
            }
            Toggle("Look up each paper’s publication record online", isOn: $batchOnline)
                .help("Checks Crossref, OpenAlex and Retraction Watch for corrections, retractions and the references.")
            HStack {
                if adding { ProgressView().controlSize(.small) }
                if let batchResult { Text(batchResult).font(.caption).foregroundStyle(.secondary).lineLimit(3) }
                if let batchError { Text(batchError.message).font(.caption).foregroundStyle(Theme.critical).lineLimit(3) }
                Spacer()
                Button(batchCount > 0 ? "Add \(batchCount) to Queue" : "Add to Queue") { Task { await addBatch(batchText) } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(batchCount == 0 || adding || !engine.isReady || batchCount > 300)
            }
            if batchCount > 300 {
                Text("At most 300 papers at a time. Split the list and add the rest afterwards.")
                    .font(.caption).foregroundStyle(Theme.critical)
            }
        }
    }

    private func chooseList() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.plainText, .commaSeparatedText, .tabSeparatedText]
        panel.message = "Choose a text or CSV file with one DOI per line"
        guard panel.runModal() == .OK, let u = panel.url,
              let text = try? String(contentsOf: u, encoding: .utf8) else { return }
        batchText = batchText.isEmpty ? text : batchText + "\n" + text
        if batchName.isEmpty { batchName = u.deletingPathExtension().lastPathComponent }
    }

    private func addBatch(_ text: String) async {
        adding = true
        batchError = nil
        batchResult = nil
        defer { adding = false }
        do {
            let r = try await batch.add(text: text, name: batchName.trimmingCharacters(in: .whitespaces), online: batchOnline)
            batchResult = r.summary
            if !r.added.isEmpty { batchText = ""; batchName = "" }
        } catch {
            batchError = AppError.from(error)
        }
    }

    private func inspect() {
        guard !trimmed.isEmpty, engine.isReady else { return }
        if pastedMany > 0 {
            let text = doi
            several = true
            batchText = text
            doi = ""
            Task { await addBatch(text) }
            return
        }
        queue.inspectDOI(trimmed, origin: sceneID, selection: nil)
        doi = ""
    }
}
