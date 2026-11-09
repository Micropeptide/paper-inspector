import AppKit
import Darwin
import Foundation
import Observation

/// Owns the analysis engine process: finds it, launches it once per app on a free local port, watches its health,
/// restarts it once after a crash, and keeps its output for the log window.
@MainActor @Observable
final class EngineController {
    static let shared = EngineController()

    enum State: Equatable {
        case idle, locating
        case launching(attempt: Int)
        case ready(version: String)
        case failed(AppError)
        case crashed(code: Int32)
    }

    private(set) var state: State = .idle
    private(set) var base = URL(string: "http://127.0.0.1:8765")!
    private(set) var executable: String?
    private(set) var pid: Int32?
    private(set) var log: [String] = []
    /// The scans the engine has on disk.  Kept after the engine stops so the sidebar does not go blank.
    private(set) var runs: [RunSummary] = []
    private(set) var runsError: AppError?
    /// True once the list has loaded from the running engine, so a missing run really is gone.
    private(set) var runsLoaded = false

    @ObservationIgnored private var process: Process?
    @ObservationIgnored private var stopping = false
    @ObservationIgnored private var autoRestarts = 0
    @ObservationIgnored private var launches = 0
    @ObservationIgnored private var monitor: Task<Void, Never>?
    @ObservationIgnored private let sink = LogSink()

    var client: EngineClient { EngineClient(base: base) }
    var isReady: Bool { if case .ready = state { return true }; return false }
    var isBusy: Bool {
        switch state {
        case .locating, .launching: return true
        default: return false
        }
    }
    var version: String? { if case .ready(let v) = state { return v }; return nil }
    var port: Int? { base.port }
    static var logFile: URL { LogSink.file }

    var statusText: String {
        switch state {
        case .idle: return "Not started"
        case .locating: return "Looking for the engine…"
        case .launching: return "Starting…"
        case .ready(let v): return "Engine \(v) · Ready"
        case .failed: return "Offline"
        case .crashed: return "Stopped unexpectedly"
        }
    }

    /// Idempotent: does nothing while the engine is starting or running.
    func start() {
        switch state {
        case .idle, .failed, .crashed: break
        default: return
        }
        state = .locating  // synchronously, so a second call before the task runs is a no-op
        Task { await launch() }
    }

    func restart() {
        stop()
        state = .idle
        autoRestarts = 0
        start()
    }

    private func launch() async {
        stopping = false
        let env = ProcessInfo.processInfo.environment
        if let attach = env["PAPERINSPECTOR_URL"], let u = URL(string: attach) {
            // Developer mode: talk to an engine someone else runs (it must be on this machine).
            base = u
            if let v = await client.health() { becomeReady(v); return }
            state = .failed(AppError(title: "No engine at \(attach)", message: "PAPERINSPECTOR_URL points to an engine that isn't answering."))
            return
        }
        let searched = Self.candidates()
        var exe = searched.first { FileManager.default.isExecutableFile(atPath: $0) }
        if exe == nil { exe = await Task.detached { Self.loginShellLookup() }.value }
        guard let exe else {
            state = .failed(AppError(
                title: "Paper Inspector's engine isn't installed",
                message: "Run `pip install -e .` in the project folder, or choose the `paperinspector` command in "
                    + "Settings > Engine. Looked in:\n" + searched.joined(separator: "\n"),
                suggestsSettings: true))
            return
        }
        executable = exe
        await Task.detached { Self.reapOrphan() }.value
        let stored = UserDefaults.standard.integer(forKey: "enginePort")
        let port = Int(env["PAPERINSPECTOR_PORT"] ?? "") ?? (stored > 0 ? stored : (Self.freePort() ?? 8765))
        base = URL(string: "http://127.0.0.1:\(port)")!
        launches += 1
        state = .launching(attempt: 0)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = ["serve", "--port", String(port), "--host", "127.0.0.1"]
        var penv = env
        // Apps launched from Finder get launchd's minimal PATH; the engine may need Homebrew tools.
        penv["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        penv["PYTHONUNBUFFERED"] = "1"
        p.environment = penv
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        let sink = self.sink
        sink.write("--- \(Date().formatted(date: .numeric, time: .standard)) starting \(exe) on port \(port)\n")
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if d.isEmpty { h.readabilityHandler = nil; return }
            sink.write(d)
            let text = String(decoding: d, as: UTF8.self)
            Task { @MainActor in self?.appendLog(text) }
        }
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            Task { @MainActor in self?.exited(proc, code: code) }
        }
        do {
            try p.run()
        } catch {
            state = .failed(AppError(title: "The engine didn't start", message: error.localizedDescription,
                                     suggestsLog: true, suggestsSettings: true))
            return
        }
        process = p
        pid = p.processIdentifier
        UserDefaults.standard.set(Int(p.processIdentifier), forKey: "enginePID")

        let limit: TimeInterval = launches == 1 ? 60 : 20
        let started = Date()
        var delay: UInt64 = 250_000_000
        var attempt = 0
        while Date().timeIntervalSince(started) < limit {
            try? await Task.sleep(nanoseconds: delay)
            guard process === p, p.isRunning else { return }  // exited: `exited` has set the state
            attempt += 1
            state = .launching(attempt: attempt)
            if let v = await client.health() { becomeReady(v); return }
            delay = min(delay * 2, 2_000_000_000)
        }
        terminate(p)
        state = .failed(AppError(title: "The engine didn't answer",
                                 message: "It started but did not respond within \(Int(limit)) seconds.",
                                 suggestsLog: true))
    }

    private func becomeReady(_ version: String) {
        state = .ready(version: version)
        Task { await refreshRuns() }
        monitor?.cancel()
        monitor = Task { [weak self] in
            var misses = 0
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard let self, self.isReady else { return }
                if await self.client.health() != nil { misses = 0; continue }
                misses += 1
                if misses >= 2 { self.lost(code: -1); return }
            }
        }
    }

    private func exited(_ p: Process, code: Int32) {
        guard p === process else { return }
        process = nil
        pid = nil
        if stopping { return }
        if case .launching = state {
            state = .failed(AppError(title: "The engine stopped while starting",
                                     message: "It exited with code \(code). The log usually says why.",
                                     suggestsLog: true, suggestsSettings: true))
            return
        }
        lost(code: code)
    }

    private func lost(code: Int32) {
        monitor?.cancel()
        if let p = process { terminate(p); process = nil }
        state = .crashed(code: code)
        if autoRestarts < 1 {
            autoRestarts += 1
            appendLog("Paper Inspector: the engine stopped (code \(code)); restarting it once.\n")
            start()
        }
    }

    /// Ends the engine this app launched: SIGTERM, then SIGKILL after two seconds.
    func stop() {
        stopping = true
        monitor?.cancel()
        if let p = process {
            terminate(p)
            UserDefaults.standard.removeObject(forKey: "enginePID")
        }
        process = nil
        pid = nil
    }

    private func terminate(_ p: Process) {
        guard p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(2)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    private func appendLog(_ text: String) {
        log.append(contentsOf: text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init))
        if log.last == "" { log.removeLast() }
        if log.count > 2000 { log.removeFirst(log.count - 2000) }
    }

    func refreshRuns() async {
        do {
            runs = try await client.runs()
            runsError = nil
            runsLoaded = true
        } catch {
            runsError = AppError.from(error)
        }
    }

    func forgetRun(_ id: String) { runs.removeAll { $0.id == id } }

    /// An engine left behind when the app last crashed or was force-quit: stop it, but only if that process id
    /// still belongs to a `paperinspector serve`.
    nonisolated static func reapOrphan() {
        let old = Int32(UserDefaults.standard.integer(forKey: "enginePID"))
        UserDefaults.standard.removeObject(forKey: "enginePID")
        guard old > 0, kill(old, 0) == 0 else { return }
        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-p", String(old), "-o", "command="]
        let out = Pipe()
        ps.standardOutput = out
        ps.standardError = FileHandle.nullDevice
        guard (try? ps.run()) != nil else { return }
        ps.waitUntilExit()
        let command = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        if command.contains("paperinspector") && command.contains(" serve") { kill(old, SIGTERM) }
    }

    // MARK: locating

    nonisolated static func candidates() -> [String] {
        var c: [String] = []
        if let o = UserDefaults.standard.string(forKey: "enginePath"), !o.isEmpty { c.append(o) }
        if let e = ProcessInfo.processInfo.environment["PAPERINSPECTOR_ENGINE"], !e.isEmpty { c.append(e) }
        if let p = Bundle.main.object(forInfoDictionaryKey: "PaperInspectorEngine") as? String, !p.isEmpty { c.append(p) }
        var dir = Bundle.main.bundleURL
        for _ in 0..<6 {
            dir = dir.deletingLastPathComponent()
            c.append(dir.appendingPathComponent(".venv/bin/paperinspector").path)
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        c += ["\(home)/.local/bin/paperinspector", "/opt/homebrew/bin/paperinspector", "/usr/local/bin/paperinspector"]
        var seen = Set<String>()
        return c.filter { seen.insert($0).inserted }
    }

    /// Ask a login shell where `paperinspector` is (it may live in a pyenv/conda/pipx path the app can't guess).
    nonisolated static func loginShellLookup() -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "command -v paperinspector"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { p.terminate(); return nil }
        let path = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }

    /// A port nothing is listening on, so the app never talks to some other process on a fixed port.
    nonisolated static func freePort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, len) == 0 && getsockname(fd, sa, &len) == 0
            }
        }
        guard bound else { return nil }
        let port = Int(UInt16(bigEndian: addr.sin_port))
        return port > 0 ? port : nil
    }

    /// Where the engine keeps its data (mirrors pipeline.HOME).
    nonisolated static var dataFolder: URL {
        if let h = ProcessInfo.processInfo.environment["PAPERINSPECTOR_HOME"], !h.isEmpty { return URL(fileURLWithPath: h) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".paperinspector")
    }

    nonisolated static func runFolder(_ id: String) -> URL? {
        guard !id.contains("/"), !id.contains("..") else { return nil }
        let u = dataFolder.appendingPathComponent("runs").appendingPathComponent(id)
        return FileManager.default.fileExists(atPath: u.appendingPathComponent("report.json").path) ? u : nil
    }
}

/// Appends engine output to ~/Library/Logs/PaperInspector/engine.log from the pipe's background queue.
final class LogSink: @unchecked Sendable {
    static let file: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/PaperInspector/engine.log")
    private let lock = NSLock()
    private var handle: FileHandle?

    init() {
        let fm = FileManager.default
        try? fm.createDirectory(at: Self.file.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let attrs = try? fm.attributesOfItem(atPath: Self.file.path), let size = attrs[.size] as? Int, size > 5_000_000 {
            try? fm.removeItem(at: Self.file)
        }
        if !fm.fileExists(atPath: Self.file.path) { fm.createFile(atPath: Self.file.path, contents: nil) }
        handle = try? FileHandle(forWritingTo: Self.file)
        _ = try? handle?.seekToEnd()
    }

    func write(_ text: String) { write(Data(text.utf8)) }

    func write(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        try? handle?.write(contentsOf: data)
    }
}
