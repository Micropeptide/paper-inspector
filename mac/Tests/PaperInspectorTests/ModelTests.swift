import XCTest
@testable import PaperInspector

final class ModelTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    func testReportWithMissingKeysStillOpens() throws {
        let r = try decode(Report.self, """
        {"id": "r1", "paper": {"source": "a.pdf"},
         "figures": [{"id": "F1", "panels": [{"id": "F1.a", "box": [1, 2]}]}],
         "findings": [{"id": "001", "severity": "high", "title": "t", "locations": [{"figure": "F1", "page": "x"}]},
                      {"severity": "low"},
                      {"id": "002", "severity": "catastrophic", "confidence": "high"}],
         "score": 42}
        """)
        XCTAssertEqual(r.id, "r1")
        XCTAssertEqual(r.paper.source, "a.pdf")
        XCTAssertEqual(r.displayTitle, "a.pdf")
        XCTAssertEqual(r.figures.first?.label, "F1")
        XCTAssertEqual(r.figures.first?.hasSize, false)
        XCTAssertNil(r.figures.first?.panels.first?.box.rect)
        XCTAssertEqual(r.findings.map(\.id), ["001", "002"], "a finding without an id is skipped, not fatal")
        XCTAssertEqual(r.findings[0].kind, "integrity")
        XCTAssertNil(r.findings[0].locations.first?.page, "a mistyped field falls back instead of failing")
        XCTAssertEqual(r.findings[1].severity, .unknown)
        XCTAssertEqual(r.findings[1].confidence, 0)
        XCTAssertTrue(r.sources.isEmpty)
        XCTAssertFalse(r.isCase)
    }

    func testCaseReportAndSummary() throws {
        let r = try decode(Report.self, """
        {"id": "c", "paper": {"title": "Case"}, "case": [{"index": 1, "source": "a.pdf"}, {"index": 2, "title": "B"}],
         "score": 10, "score_ai": 4}
        """)
        XCTAssertTrue(r.isCase)
        XCTAssertEqual(r.displayScore, 4)
        let runs = try decode([RunSummary].self, """
        [{"id": "x", "title": "T", "source": "s", "score": 3, "created": "2025-01-01T22:12:30", "counts": {}, "case": true}]
        """)
        XCTAssertTrue(runs[0].isCase)
        XCTAssertNotNil(runs[0].date)
    }

    func testBoxes() {
        XCTAssertEqual([1, 2, 3, 4].rect, CGRect(x: 1, y: 2, width: 3, height: 4))
        XCTAssertNil([1, 2, 3].rect)
        XCTAssertNil([1, 2, .nan, 4].rect)
        XCTAssertNil([1, 2, -3, 4].rect)
    }

    func testSeverityOrderAndFallback() throws {
        XCTAssertTrue(Severity.critical < .high)
        XCTAssertTrue(Severity.info < .unknown)
        XCTAssertEqual(try decode([Severity].self, #"["HIGH", "weird", 3]"#), [.high, .unknown, .unknown])
    }

    func testMultipartFilenameIsEscaped() throws {
        XCTAssertEqual(EngineClient.dispositionFilename("a\"b\\c\r\nd.pdf"), "a'b_c_d.pdf")
        XCTAssertEqual(EngineClient.dispositionFilename("图 1.pdf"), "图 1.pdf")

        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let pdf = dir.appendingPathComponent("my \"paper\".pdf")
        let xlsx = dir.appendingPathComponent("data.xlsx")
        try Data("%PDF-1.4 body".utf8).write(to: pdf)
        try Data(repeating: 7, count: 3 << 20).write(to: xlsx)
        let body = try EngineClient.writeMultipart([("file", pdf), ("supplements", xlsx)], boundary: "B")
        defer { try? FileManager.default.removeItem(at: body) }
        let data = try Data(contentsOf: body)
        let head = String(decoding: data.prefix(400), as: UTF8.self)
        XCTAssertTrue(head.hasPrefix("--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"my 'paper'.pdf\""))
        XCTAssertTrue(head.contains("Content-Type: application/pdf\r\n\r\n%PDF-1.4 body\r\n--B\r\n"))
        XCTAssertTrue(String(decoding: data.suffix(8), as: UTF8.self).hasSuffix("--B--\r\n"))
        XCTAssertGreaterThan(data.count, 3 << 20)
    }

    func testExpandKeepsReadableFilesOnly() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir.appendingPathComponent("sub"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        for n in ["paper.pdf", "sub/table.csv", "notes.key", ".hidden.pdf"] {
            try Data().write(to: dir.appendingPathComponent(n))
        }
        let names = ScanQueue.expand([dir, dir.appendingPathComponent("paper.pdf")]).map(\.lastPathComponent)
        XCTAssertEqual(names, ["paper.pdf", "table.csv"])
    }

    func testPDFNeedleUsesLongestQuotedRun() {
        XCTAssertEqual(PDFPageView.needle("…as an AI language model, I cannot provide…"), "as an AI language model, I cannot provide")
        XCTAssertNil(PDFPageView.needle("…ab…"))
    }

    /// Every report the engine has written on this Mac must open.
    func testDecodesLocalReports() throws {
        let runs = EngineController.dataFolder.appendingPathComponent("runs")
        let dirs = (try? FileManager.default.contentsOfDirectory(at: runs, includingPropertiesForKeys: nil)) ?? []
        for d in dirs.prefix(50) {
            guard let data = try? Data(contentsOf: d.appendingPathComponent("report.json")) else { continue }
            XCTAssertNoThrow(try JSONDecoder().decode(Report.self, from: data), d.lastPathComponent)
        }
    }
}
