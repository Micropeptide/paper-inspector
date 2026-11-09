import AppKit
import SwiftUI

struct RunsSidebar: View {
    @Binding var selection: Set<String>
    @Binding var query: String
    let onTrash: ([String]) -> Void
    let onOpenInNewWindow: (String) -> Void
    let onAskAI: (String) -> Void
    let onReveal: (String) -> Void

    @Environment(EngineController.self) private var engine
    @Environment(ScanQueue.self) private var queue
    @Environment(BatchQueueModel.self) private var batch
    @SceneStorage("queueExpanded") private var queueExpanded = true

    private var filtered: [RunSummary] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return engine.runs }
        return engine.runs.filter { "\($0.title) \($0.source) \($0.id) \($0.tags.joined(separator: " "))".lowercased().contains(q) }
    }

    /// Today / Yesterday / This Week / Earlier, newest first.
    private var groups: [(title: String, runs: [RunSummary])] {
        let cal = Calendar.current
        let now = Date()
        var buckets: [String: [RunSummary]] = [:]
        for r in filtered {
            let key: String
            if let d = r.date {
                if cal.isDateInToday(d) { key = "Today" }
                else if cal.isDateInYesterday(d) { key = "Yesterday" }
                else if let days = cal.dateComponents([.day], from: d, to: now).day, days < 7 { key = "This Week" }
                else { key = "Earlier" }
            } else {
                key = "Earlier"
            }
            buckets[key, default: []].append(r)
        }
        return ["Today", "Yesterday", "This Week", "Earlier"].compactMap { k in buckets[k].map { (k, $0) } }
    }

    var body: some View {
        List(selection: $selection) {
            if !queue.scans.isEmpty {
                Section("In Progress") {
                    ForEach(queue.scans) { job in
                        InProgressRow(job: job).selectionDisabled()
                    }
                }
            }
            if !batch.items.isEmpty {
                Section(isExpanded: $queueExpanded) {
                    ForEach(batch.sorted) { item in
                        QueueRow(item: item, open: { run in selection = [run] }).selectionDisabled()
                    }
                } header: {
                    QueueHeader()
                }
            }
            ForEach(groups, id: \.title) { group in
                Section(group.title) {
                    ForEach(group.runs) { run in
                        RunRow(run: run).tag(run.id)
                    }
                }
            }
        }
        .overlay {
            if filtered.isEmpty && queue.scans.isEmpty && batch.items.isEmpty {
                if !query.isEmpty {
                    ContentUnavailableView.search(text: query)
                } else if engine.isReady && engine.runsLoaded {
                    ContentUnavailableView("No Scans Yet", systemImage: "doc.text.magnifyingglass",
                                           description: Text("Paste a DOI or drop a PDF to start."))
                }
            }
        }
        .searchable(text: $query, placement: .sidebar, prompt: "Search scans")
        .contextMenu(forSelectionType: String.self) { ids in
            if ids.count == 1, let id = ids.first {
                Button("Open in New Window") { onOpenInNewWindow(id) }
                Button("Ask the AI Investigator") { onAskAI(id) }
                if let r = engine.runs.first(where: { $0.id == id }), r.source.lowercased().hasPrefix("10.") {
                    Button("Copy DOI") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(r.source, forType: .string)
                    }
                }
                if EngineController.runFolder(id) != nil {
                    Button("Show in Finder") { onReveal(id) }
                }
                Divider()
            }
            if !ids.isEmpty {
                Button(ids.count == 1 ? "Move to Trash…" : "Move \(ids.count) Scans to Trash…", role: .destructive) {
                    onTrash(Array(ids))
                }
            }
        } primaryAction: { ids in
            if ids.count == 1, let id = ids.first { onOpenInNewWindow(id) }
        }
        .onDeleteCommand { if !selection.isEmpty { onTrash(Array(selection)) } }
        .safeAreaInset(edge: .bottom, spacing: 0) { EngineStatusFooter() }
    }
}

struct RunRow: View {
    let run: RunSummary

    var body: some View {
        HStack(spacing: 10) {
            ScoreBadge(score: run.displayScore, aiAdjusted: run.scoreAI != nil)
            VStack(alignment: .leading, spacing: 2) {
                Text(run.title).lineLimit(2)
                HStack(spacing: 4) {
                    if run.isCase {
                        Image(systemName: "person.2").accessibilityLabel("Several papers compared")
                    }
                    if let d = run.date {
                        Text("\(run.source) · \(d, format: .relative(presentation: .named))")
                    } else {
                        Text(run.source)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
        .help(run.title)
        .accessibilityElement(children: .combine)
    }
}

struct InProgressRow: View {
    let job: ScanQueue.Job
    @Environment(ScanQueue.self) private var queue

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(job.title).lineLimit(1)
                Spacer()
                if job.isRunning {
                    Button { queue.cancel(job.id) } label: { Label("Cancel", systemImage: "xmark.circle.fill") }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .help(job.cancelling ? "Stopping after the current step" : "Stop this scan")
                        .disabled(job.cancelling)
                }
            }
            if let failure = job.failure {
                Label(failure.message, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Theme.critical)
                    .lineLimit(3)
                HStack {
                    Button("Retry") { queue.retry(job.id) }
                    Button("Dismiss") { queue.dismiss(job.id) }
                }
                .controlSize(.small)
            } else {
                ProgressView(value: job.progress).controlSize(.small)
                Text(job.stage).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(job.failure == nil ? "\(job.title), \(job.stage), \(Int(job.progress * 100)) percent" : "\(job.title) failed")
    }
}

struct EngineStatusFooter: View {
    @Environment(EngineController.self) private var engine
    @Environment(\.openWindow) private var openWindow

    private var color: Color {
        switch engine.state {
        case .ready: return Theme.calm
        case .failed, .crashed: return Theme.critical
        default: return Theme.medium
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let e = engine.runsError, engine.isReady {
                HStack {
                    Text("Couldn’t load scans: \(e.message)").font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    Button("Reload") { Task { await engine.refreshRuns() } }.controlSize(.small)
                }
            }
            HStack(spacing: 6) {
                if engine.isBusy {
                    ProgressView().controlSize(.mini)
                } else {
                    Circle().fill(color).frame(width: 8, height: 8).accessibilityHidden(true)
                }
                Text(engine.statusText).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Menu {
                    Button("Restart Engine") { engine.restart() }
                    Button("Show Engine Log") { openWindow(id: "log") }
                    SettingsLink { Text("Settings…") }
                } label: {
                    Label("Engine Options", systemImage: "ellipsis.circle")
                }
                .labelStyle(.iconOnly)
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Analysis engine: \(engine.statusText)")
    }
}

/// "Queue · 3 of 10 done", with the queue's actions.
struct QueueHeader: View {
    @Environment(BatchQueueModel.self) private var batch

    var body: some View {
        HStack {
            Text(title)
            Spacer()
            Menu {
                if batch.counts["queued"] ?? 0 > 0 {
                    Button("Cancel Waiting Papers") { batch.cancelWaiting() }
                }
                Button("Clear Finished") { batch.clearFinished() }
                    .disabled(batch.finishedCount == 0)
                if !batch.batches.isEmpty {
                    Divider()
                    Menu("Export Results as CSV") {
                        ForEach(batch.batches) { b in
                            Button(b.displayName) { Exporter.exportBatch(b) }
                        }
                    }
                }
            } label: {
                Label("Queue Options", systemImage: "ellipsis.circle")
            }
            .labelStyle(.iconOnly)
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Queue options")
        }
    }

    private var title: String {
        let total = batch.items.count
        if batch.hasActive { return "Queue · \(batch.finishedCount) of \(total) finished" }
        return "Queue · \(batch.doneCount) of \(total) done"
    }
}

/// One paper in the batch queue: its state, and what can be done with it.
struct QueueRow: View {
    let item: QueueItem
    let open: (String) -> Void
    @Environment(BatchQueueModel.self) private var batch

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: item.status.symbol)
                .foregroundStyle(color)
                .frame(width: 16)
                .padding(.top, 1)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayTitle).lineLimit(2)
                if item.status == .running {
                    ProgressView(value: min(max(item.progress, 0), 1)).controlSize(.small)
                }
                Text(item.caption)
                    .font(.caption)
                    .foregroundStyle(item.status == .error ? Theme.critical : .secondary)
                    .lineLimit(item.status == .error ? 3 : 1)
            }
            Spacer(minLength: 0)
            actions
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { if let r = item.run, item.status == .done { open(r) } }
        .contextMenu { menu }
        .help(item.doi.map { "\(item.input)\n\($0)" } ?? item.input)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(item.displayTitle), \(item.status.label)")
    }

    private var color: Color {
        switch item.status {
        case .done: return Theme.calm
        case .error: return Theme.critical
        case .running: return .accentColor
        default: return .secondary
        }
    }

    @ViewBuilder private var actions: some View {
        switch item.status {
        case .queued, .running:
            Button { batch.cancel(item) } label: { Label("Cancel", systemImage: "xmark.circle.fill") }
                .labelStyle(.iconOnly).buttonStyle(.borderless).foregroundStyle(.secondary)
                .help(item.status == .running ? "Stop this scan" : "Take this paper out of the queue")
                .disabled(item.stage.lowercased().hasPrefix("cancelling"))
        case .error, .cancelled:
            Button { batch.retry(item) } label: { Label("Retry", systemImage: "arrow.clockwise") }
                .labelStyle(.iconOnly).buttonStyle(.borderless)
                .help("Put this paper back in the queue")
        case .done:
            if let r = item.run {
                Button { open(r) } label: { Label("Show", systemImage: "arrow.right.circle") }
                    .labelStyle(.iconOnly).buttonStyle(.borderless)
                    .help("Show this scan")
            }
        case .unknown:
            EmptyView()
        }
    }

    @ViewBuilder private var menu: some View {
        if item.status == .done, let r = item.run { Button("Show Scan") { open(r) } }
        if item.status.isActive { Button("Cancel") { batch.cancel(item) } }
        if item.status == .error || item.status == .cancelled { Button("Retry") { batch.retry(item) } }
        if let d = item.doi {
            Button("Copy DOI") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(d, forType: .string)
            }
        }
        if !item.status.isActive {
            Divider()
            Button("Remove from Queue") { batch.remove(item) }
        }
    }
}
