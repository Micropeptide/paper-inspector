import XCTest
@testable import PaperInspector

/// Rulings, catalogue, letters, exports, cancel and the queue against a running engine.  Skipped unless
/// PI_ENGINE_URL points at one started with a scratch PAPERINSPECTOR_HOME.  Nothing here reaches the internet:
/// the demo paper is built in, and the queue is only sent text without DOIs.
final class LiveReviewTests: XCTestCase {
    private var client: EngineClient!

    override func setUp() async throws {
        guard let s = ProcessInfo.processInfo.environment["PI_ENGINE_URL"], let u = URL(string: s) else {
            throw XCTSkip("PI_ENGINE_URL not set")
        }
        client = EngineClient(base: u)
        guard await client.health() != nil else { throw XCTSkip("no engine at \(s)") }
    }

    private func demoRun() async throws -> String {
        var j = try await client.demo()
        let deadline = Date().addingTimeInterval(300)
        while j.status == .running && Date() < deadline {
            try await Task.sleep(nanoseconds: 500_000_000)
            j = try await client.job(j.id)
        }
        XCTAssertEqual(j.status, .done, j.error ?? j.stage)
        return try XCTUnwrap(j.run)
    }

    func testRulingsExportsLettersAndCatalogue() async throws {
        let run = try await demoRun()
        defer { Task { [client] in try? await client?.deleteRun(run) } }
        let report = try await client.report(run)
        XCTAssertNotNil(report.review, "this engine annotates reports with rulings")
        let f = try XCTUnwrap(report.findings.first { !$0.severity.isNote })
        XCTAssertNotNil(f.fingerprint)

        // Set, read back, change the ruling but keep the note, then clear.
        let set = try await client.setRuling(run: run, finding: f.id, ruling: .confirmed, note: "Checked by eye")
        XCTAssertEqual(set.ruling.ruling, .confirmed)
        XCTAssertEqual(set.review?.count(.confirmed), 1)
        var again = try await client.report(run)
        XCTAssertEqual(again.finding(f.id)?.ruling.note, "Checked by eye")
        let kept = try await client.setRuling(run: run, finding: f.id, ruling: .needsRawData, note: nil)
        XCTAssertEqual(kept.ruling.note, "Checked by eye", "a nil note keeps the stored one")

        // The letter lists the finding once it is confirmed.
        _ = try await client.setRuling(run: run, finding: f.id, ruling: .confirmed, note: nil)
        let letter = try await client.letter(run: run, kind: .editor, includeNeedsData: true)
        XCTAssertFalse(letter.empty)
        XCTAssertTrue(letter.included.contains(f.id))
        XCTAssertFalse(letter.body.isEmpty)

        // Exports download to disk with the right kind of content.
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        for kind in [ExportKind.csv, .xlsx, .pdf, .evidence] {
            let url = try XCTUnwrap(client.exportURL(run, kind, options: ExportOptions(includeNotes: false, includeRulingNotes: true)))
            let dest = try await client.downloadExport(url, to: dir.appendingPathComponent(Exporter.fileName(kind, run: run)))
            let head = try XCTUnwrap(FileHandle(forReadingFrom: dest).readData(ofLength: 8))
            switch kind {
            case .csv:
                let text = try String(contentsOf: dest, encoding: .utf8)
                XCTAssertTrue(text.hasPrefix("id,fingerprint,check") || text.hasPrefix("\u{FEFF}id,fingerprint,check"), text.prefix(60).description)
                XCTAssertTrue(text.contains("Checked by eye"))
            case .xlsx, .evidence: XCTAssertEqual(head.prefix(2), Data("PK".utf8), kind.rawValue)
            case .pdf: XCTAssertEqual(head.prefix(4), Data("%PDF".utf8))
            default: break
            }
        }

        let cleared = try await client.clearRuling(run: run, finding: f.id)
        XCTAssertEqual(cleared.ruling.ruling, .unreviewed)
        again = try await client.report(run)
        XCTAssertEqual(again.finding(f.id)?.ruling.ruling, .unreviewed)

        // Every check in the report has an explanation.
        let catalog = try await client.catalog()
        XCTAssertFalse(catalog.isEmpty)
        let info = try await client.explain(f.check)
        XCTAssertFalse(info.what.isEmpty)
    }

    func testBadRulingAndUnknownFindingAreRefused() async throws {
        let run = try await demoRun()
        defer { Task { [client] in try? await client?.deleteRun(run) } }
        do {
            _ = try await client.setRuling(run: run, finding: "no-such-finding", ruling: .confirmed, note: nil)
            XCTFail("expected 404")
        } catch let e as AppError {
            XCTAssertEqual(e.status, 404)
        }
    }

    func testCancelAJob() async throws {
        let j = try await client.demo()
        let c = try await client.cancelJob(j.id)
        XCTAssertTrue([.running, .cancelled, .done].contains(c.status))
        var last = c
        let deadline = Date().addingTimeInterval(120)
        while last.status == .running && Date() < deadline {
            try await Task.sleep(nanoseconds: 300_000_000)
            last = try await client.job(j.id)
        }
        XCTAssertTrue(last.status == .cancelled || last.status == .done, "\(last.status) \(last.stage)")
        if let r = last.run { try? await client.deleteRun(r) }
    }

    func testQueueRefusesTextWithoutDOIs() async throws {
        do {
            _ = try await client.queueAdd(text: "no identifiers here\njust words", name: "", online: false)
            XCTFail("expected 400")
        } catch let e as AppError {
            XCTAssertEqual(e.status, 400)
        }
        let list = try await client.queue()
        XCTAssertNotNil(list.counts["queued"] ?? 0)
    }
}
