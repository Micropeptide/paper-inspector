import XCTest
@testable import PaperInspector

/// Exercises the client against a running engine.  Skipped unless PI_ENGINE_URL points at one (start it with a
/// scratch PAPERINSPECTOR_HOME so the test's scans don't land in your library).
final class LiveEngineTests: XCTestCase {
    private var client: EngineClient!

    override func setUp() async throws {
        guard let s = ProcessInfo.processInfo.environment["PI_ENGINE_URL"], let u = URL(string: s) else {
            throw XCTSkip("PI_ENGINE_URL not set")
        }
        client = EngineClient(base: u)
        guard await client.health() != nil else { throw XCTSkip("no engine at \(s)") }
    }

    private func finish(_ job: EngineJob) async throws -> EngineJob {
        var j = job
        let deadline = Date().addingTimeInterval(300)
        while j.status == .running && Date() < deadline {
            try await Task.sleep(nanoseconds: 500_000_000)
            j = try await client.job(j.id)
        }
        XCTAssertEqual(j.status, .done, j.error ?? j.stage)
        return j
    }

    func testDemoScanRoundTrip() async throws {
        let settings = try await client.settings()
        XCTAssertFalse(settings.presets.isEmpty)

        let demo = try await finish(try await client.demo())
        let run = try XCTUnwrap(demo.run)
        let report = try await client.report(run)
        XCTAssertFalse(report.figures.isEmpty)
        XCTAssertFalse(report.findings.isEmpty)
        let listed = try await client.runs()
        XCTAssertTrue(listed.contains { $0.id == run })

        let fig = report.figures[0]
        for mode in ViewMode.allCases {
            let (data, resp) = try await URLSession.shared.data(from: client.figureURL(run, fig.id, mode: mode))
            XCTAssertEqual((resp as? HTTPURLResponse)?.statusCode, 200, mode.rawValue)
            XCTAssertNotNil(NSImage(data: data), mode.rawValue)
        }
        if let pair = report.findings.first(where: \.isPair) {
            let c = try await client.compare(run, pair.panels[0], pair.panels[1])
            XCTAssertTrue(c.aligned)
        }
        let draft = try await client.pubpeer(run)
        XCTAssertFalse(draft.isEmpty)
        let html = try await client.reportHTML(run)
        XCTAssertTrue(html.contains("<html"))

        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let source = try await client.download(client.sourceURL(run), into: dir, baseName: "source")
        XCTAssertEqual(source.pathExtension, "pdf")

        // Re-upload the paper with its spreadsheet, as Re-run Scan does.
        let name = try XCTUnwrap(report.sources.dropFirst().first?.name)
        let supp = try await client.download(client.supplementURL(run, name),
                                             to: dir.appendingPathComponent("Supplementary \"Data\" 1.xlsx"))
        let job = try await client.upload(main: source, others: [supp], asCase: false) { _ in }
        let done = try await finish(job)
        let rerun = try XCTUnwrap(done.run)
        let again = try await client.report(rerun)
        XCTAssertEqual(again.findings.count, report.findings.count)

        try await client.deleteRun(run)
        try await client.deleteRun(rerun)
        let left = try await client.runs()
        XCTAssertFalse(left.contains { $0.id == run || $0.id == rerun })
    }

    func testSettingsRoundTripNeedsNoKeyEcho() async throws {
        let before = try await client.settings()
        let after = try await client.saveSettings(json: try JSONSerialization.data(withJSONObject: ["online": !before.online]))
        XCTAssertEqual(after.online, !before.online)
        _ = try await client.saveSettings(json: try JSONSerialization.data(withJSONObject: ["online": before.online]))
    }

    func testPostWithoutGuardHeaderIsRefused() async throws {
        var req = URLRequest(url: client.url("/api/demo"))
        req.httpMethod = "POST"
        let (_, resp) = try await URLSession.shared.data(for: req)
        XCTAssertEqual((resp as? HTTPURLResponse)?.statusCode, 403)
    }
}

/// Starts and stops a real engine through EngineController.  Opt-in (PI_LAUNCH_TEST=1) because it spawns Python.
@MainActor
final class EngineLaunchTests: XCTestCase {
    func testLaunchesOnAFreePortAndStops() async throws {
        guard ProcessInfo.processInfo.environment["PI_LAUNCH_TEST"] == "1" else { throw XCTSkip("PI_LAUNCH_TEST not set") }
        XCTAssertNotNil(EngineController.freePort())
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        setenv("PAPERINSPECTOR_HOME", home.path, 1)
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        setenv("PAPERINSPECTOR_ENGINE", root.appendingPathComponent(".venv/bin/paperinspector").path, 0)
        defer { try? FileManager.default.removeItem(at: home) }
        let engine = EngineController.shared
        engine.start()
        engine.start()  // idempotent
        let deadline = Date().addingTimeInterval(90)
        while !engine.isReady && Date() < deadline {
            if case .failed(let e) = engine.state { XCTFail("\(e.title): \(e.message)"); return }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        XCTAssertTrue(engine.isReady, engine.statusText)
        XCTAssertNotEqual(engine.port, 8765)
        let pid = try XCTUnwrap(engine.pid)
        engine.stop()
        XCTAssertNotEqual(kill(pid, 0), 0, "the engine process is gone after stop()")
    }
}
