import AppKit
import SwiftUI
import UserNotifications

@main
struct PaperInspectorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var engine = EngineController.shared
    @State private var queue = ScanQueue.shared
    @State private var batch = BatchQueueModel.shared
    @State private var catalog = CatalogStore.shared

    var body: some Scene {
        WindowGroup("Paper Inspector", id: "main", for: String.self) { $run in
            MainWindow(initialRun: run)
                .environment(engine)
                .environment(queue)
                .environment(batch)
                .environment(catalog)
                .environment(ExportTracker.shared)
                .frame(minWidth: 900, minHeight: 600)
        }
        .defaultSize(width: 1440, height: 900)
        .commands { AppCommands(engine: engine, queue: queue) }

        WindowGroup("Blink Comparator", id: "blink", for: BlinkSpec.self) { $spec in
            if let spec {
                BlinkWindow(spec: spec)
            } else {
                ContentUnavailableView("Nothing to Compare", systemImage: "rectangle.on.rectangle",
                                       description: Text("Pick two panels, or a reused-image finding, then choose Inspect > Open in Blink Comparator."))
            }
        }
        .defaultSize(width: 900, height: 700)

        Window("Engine Log", id: "log") {
            EngineLogView().environment(engine)
        }
        .defaultSize(width: 760, height: 480)
        .keyboardShortcut("l", modifiers: [.command, .option])

        Settings {
            SettingsView().environment(engine)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        if Bundle.main.bundleURL.pathExtension == "app" { UNUserNotificationCenter.current().delegate = self }
        MainActor.assumeIsolated {
            EngineController.shared.start()
            BatchQueueModel.shared.startPolling()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !UserDefaults.standard.bool(forKey: "keepRunningWithoutWindows")
    }

    /// Finder "Open With" and drops on the Dock icon.
    func application(_ application: NSApplication, open urls: [URL]) {
        let files = urls.filter(\.isFileURL)
        let links = urls.filter { $0.scheme == "paperinspector" }
        MainActor.assumeIsolated {
            if !files.isEmpty { ScanQueue.shared.importFiles(files) }
            for l in links { Self.handle(l) }
        }
    }

    /// paperinspector://doi/10.1371/journal.pbio.1002106
    @MainActor static func handle(_ url: URL) {
        guard url.scheme == "paperinspector", url.host == "doi" else { return }
        let doi = url.path.drop { $0 == "/" }
        if !doi.isEmpty { ScanQueue.shared.inspectDOI(String(doi).removingPercentEncoding ?? String(doi), origin: nil, selection: nil) }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        MainActor.assumeIsolated {
            let scans = ScanQueue.shared.runningCount
            let queued = BatchQueueModel.shared.active
            guard scans > 0 || queued > 0 else { return .terminateNow }
            let alert = NSAlert()
            if scans > 0 {
                alert.messageText = scans == 1 ? "A scan is still running" : "\(scans) scans are still running"
                alert.informativeText = "Quitting stops \(scans == 1 ? "it" : "them"), and the progress is lost."
                    + (queued > 0 ? " The \(queued) papers in the batch queue continue the next time you open Paper Inspector." : "")
            } else {
                alert.messageText = "\(queued) paper\(queued == 1 ? " is" : "s are") still in the batch queue"
                alert.informativeText = "The queue pauses while Paper Inspector is closed and continues the next time you open it. "
                    + "The paper being scanned now starts again from the beginning."
            }
            alert.addButton(withTitle: "Quit Anyway")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
            ScanQueue.shared.cancelAllNow()
            return .terminateNow
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { EngineController.shared.stop() }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let run = response.notification.request.content.userInfo["run"] as? String
        DispatchQueue.main.async {
            if let run {
                MainActor.assumeIsolated {
                    NSApp.activate()
                    ScanQueue.shared.finished = ScanQueue.Finished(run: run)
                }
            }
            completionHandler()
        }
    }
}

struct EngineLogView: View {
    @Environment(EngineController.self) private var engine

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(engine.log.isEmpty ? "The engine hasn't written anything yet." : engine.log.joined(separator: "\n"))
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
                Color.clear.frame(height: 1).id("end")
            }
            .onChange(of: engine.log.count) { proxy.scrollTo("end") }
            .onAppear { proxy.scrollTo("end") }
        }
        .toolbar {
            ToolbarItem {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([EngineController.logFile])
                } label: { Label("Show Log File in Finder", systemImage: "folder") }
            }
            ToolbarItem {
                Button { engine.restart() } label: { Label("Restart Engine", systemImage: "arrow.clockwise") }
            }
        }
        .navigationTitle("Engine Log")
        .navigationSubtitle(engine.statusText)
    }
}
