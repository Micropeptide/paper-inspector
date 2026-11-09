import XCTest
@testable import PaperInspector

/// Answers requests from a handler instead of the network, and records what was sent.
final class MockEngine: URLProtocol {
    struct Sent { var method: String; var path: String; var query: String?; var headers: [String: String]; var body: Data }
    nonisolated(unsafe) static var sent: [Sent] = []
    nonisolated(unsafe) static var handler: (Sent) -> (Int, String) = { _ in (200, "{}") }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if body.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                body.append(buf, count: n)
            }
            stream.close()
        }
        let u = request.url!
        let s = Sent(method: request.httpMethod ?? "GET", path: u.path, query: u.query,
                     headers: request.allHTTPHeaderFields ?? [:], body: body)
        Self.sent.append(s)
        let (status, text) = Self.handler(s)
        let resp = HTTPURLResponse(url: u, statusCode: status, httpVersion: "HTTP/1.1",
                                   headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(text.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func client() -> EngineClient {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [MockEngine.self]
        return EngineClient(base: URL(string: "http://127.0.0.1:9")!, session: URLSession(configuration: c))
    }

    static func reset(_ h: @escaping (Sent) -> (Int, String) = { _ in (200, "{}") }) {
        sent = []
        handler = h
    }
}

final class ReviewTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    private static let reportJSON = """
    {"id": "r1", "paper": {"title": "A paper", "doi": "10.1/x"}, "score": 50,
     "findings": [
       {"id": "001", "check": "image.duplicate", "severity": "high", "title": "Reused panel", "fingerprint": "abc",
        "ruling": {"ruling": "confirmed", "note": "Same blot", "updated": "2025-01-08T10:00:00"}},
       {"id": "002", "check": "stats.grim", "severity": "medium", "title": "Impossible mean",
        "ruling": {"ruling": "false_positive", "note": ""}},
       {"id": "003", "check": "refs.uncited", "severity": "low", "kind": "quality", "title": "Padded"},
       {"id": "004", "check": "report.timeline", "severity": "info", "title": "Timeline",
        "ruling": {"ruling": "something_new"}}],
     "review": {"paper_key": "doi-1", "counts": {"unreviewed": 1, "confirmed": 1, "false_positive": 1, "needs_raw_data": 0},
                "total": 3, "reviewed": 2, "score_reviewed": 35, "unmatched": 1, "labels": {}},
     "tags": ["batch-a"]}
    """

    // MARK: decoding

    func testFindingsCarryRulingsAndReportCarriesReview() throws {
        let r = try decode(Report.self, Self.reportJSON)
        XCTAssertEqual(r.findings[0].fingerprint, "abc")
        XCTAssertEqual(r.findings[0].ruling.ruling, .confirmed)
        XCTAssertEqual(r.findings[0].ruling.note, "Same blot")
        XCTAssertEqual(r.findings[1].ruling.ruling, .falsePositive)
        XCTAssertEqual(r.findings[2].ruling, RulingInfo(), "no ruling block means not reviewed")
        XCTAssertEqual(r.findings[3].ruling.ruling, .unreviewed, "an unknown ruling falls back instead of failing")
        let rv = try XCTUnwrap(r.review)
        XCTAssertEqual(rv.reviewed, 2)
        XCTAssertEqual(rv.total, 3)
        XCTAssertEqual(rv.scoreReviewed, 35)
        XCTAssertEqual(rv.count(.confirmed), 1)
        XCTAssertEqual(rv.unmatched, 1)
        XCTAssertEqual(r.tags, ["batch-a"])
    }

    func testOlderReportHasNoReview() throws {
        let r = try decode(Report.self, #"{"id": "old", "paper": {}, "findings": [{"id": "1", "severity": "high"}]}"#)
        XCTAssertNil(r.review)
        XCTAssertTrue(r.tags.isEmpty)
        XCTAssertNil(r.findings[0].fingerprint)
    }

    func testRulingWireNamesMatchTheEngine() throws {
        XCTAssertEqual(Ruling.allCases.map(\.rawValue), ["unreviewed", "confirmed", "false_positive", "needs_raw_data"])
        XCTAssertEqual(Ruling.decisions, [.confirmed, .falsePositive, .needsRawData])
        XCTAssertEqual(Ruling.fromLegacy("dismissed"), .falsePositive)
        XCTAssertEqual(Ruling.fromLegacy("confirmed"), .confirmed)
        XCTAssertNil(Ruling.fromLegacy("unreviewed"))
    }

    func testCheckInfoDecodesWithGaps() throws {
        let c = try decode(CheckInfo.self, """
        {"id": "stats.grim", "label": "Impossible mean", "category": "stats", "category_label": "Statistics",
         "kind": "integrity", "scored": true, "what": "W", "false_alarms": "F", "look_at": "L", "ask": "A", "known": true}
        """)
        XCTAssertEqual(c.falseAlarms, "F")
        XCTAssertEqual(c.lookAt, "L")
        XCTAssertTrue(c.scored)
        let sparse = try decode(CheckInfo.self, #"{"id": "x.new"}"#)
        XCTAssertEqual(sparse.label, "x.new")
        XCTAssertFalse(sparse.scored)
        let all = try decode(CatalogResponse.self, #"{"checks": [{"id": "a"}, {"label": "no id"}, {"id": "b"}], "categories": {}}"#)
        XCTAssertEqual(all.checks.map(\.id), ["a", "b"])
    }

    func testLetterText() throws {
        let l = try decode(Letter.self, #"{"kind": "editor", "label": "Editor", "subject": "Concern", "body": "Dear [name]", "included": ["001"], "empty": false}"#)
        XCTAssertEqual(l.text, "Subject: Concern\n\nDear [name]")
        let e = try decode(Letter.self, #"{"body": "Nothing yet", "empty": true}"#)
        XCTAssertTrue(e.empty)
        XCTAssertEqual(e.text, "Nothing yet")
    }

    // MARK: queue

    func testQueueDecodingAndCaptions() throws {
        let q = try decode(QueueList.self, """
        {"items": [
          {"id": "q1", "batch": "b", "input": "10.1/a", "doi": "10.1/a", "status": "done", "stage": "Done", "progress": 1,
           "run": "run1", "added": "2025-01-08T10:00:00", "finished": "2025-01-08T10:05:00", "title": "Paper A", "score": 80},
          {"id": "q2", "batch": "b", "input": "12345678", "doi": null, "status": "error", "error": "not found",
           "added": "2025-01-08T10:00:01"},
          {"id": "q3", "batch": "b", "input": "10.1/c", "doi": "10.1/c", "status": "running", "stage": "Reading figures",
           "progress": 0.4, "added": "2025-01-08T10:00:02"},
          {"id": "q4", "batch": "b", "input": "10.1/d", "status": "queued", "stage": "queued", "added": "x"},
          {"id": "q5", "status": "paused?"},
          {"batch": "no id"}],
         "counts": {"queued": 1, "running": 1, "done": 1, "error": 1, "cancelled": 0},
         "running": "q3", "batches": [{"id": "b", "name": "", "created": "x", "count": 4}]}
        """)
        XCTAssertEqual(q.items.map(\.id), ["q1", "q2", "q3", "q4", "q5"], "an item without an id is skipped")
        XCTAssertEqual(q.items[0].caption, "Done · integrity score 80")
        XCTAssertEqual(q.items[0].displayTitle, "Paper A")
        XCTAssertEqual(q.items[1].caption, "Failed: not found")
        XCTAssertEqual(q.items[1].displayTitle, "12345678")
        XCTAssertEqual(q.items[2].caption, "Reading figures")
        XCTAssertEqual(q.items[3].caption, "Waiting")
        XCTAssertEqual(q.items[4].status, .unknown)
        XCTAssertEqual(q.batches.first?.displayName, "Batch of 4")
        XCTAssertEqual(q.running, "q3")
    }

    @MainActor func testQueueSortsActiveFirst() throws {
        let q = try decode(QueueList.self, """
        {"items": [{"id": "d1", "status": "done", "finished": "2025-01-08T10:01:00"},
                   {"id": "w1", "status": "queued"}, {"id": "e1", "status": "error"},
                   {"id": "d2", "status": "done", "finished": "2025-01-08T10:09:00"},
                   {"id": "r1", "status": "running"}, {"id": "w2", "status": "queued"}],
         "counts": {"queued": 2, "running": 1, "done": 2, "error": 1}}
        """)
        let m = BatchQueueModel()
        m.apply(q)
        XCTAssertEqual(m.sorted.map(\.id), ["r1", "w1", "w2", "e1", "d2", "d1"])
        XCTAssertEqual(m.active, 3)
        XCTAssertTrue(m.hasActive)
        XCTAssertEqual(m.finishedCount, 3)
        m.apply(QueueList())
        XCTAssertFalse(m.hasActive)
    }

    func testQueueAddSummary() throws {
        let r = try decode(QueueAddResult.self, """
        {"batch": {"id": "b", "name": "Mine", "created": "x", "count": 2},
         "added": [{"id": "a", "input": "10.1/a"}, {"id": "b", "input": "10.1/b"}],
         "skipped": [{"input": "10.1/a", "reason": "already in the queue"}]}
        """)
        XCTAssertEqual(r.summary, "Added 2 papers to the queue. Skipped 1: 10.1/a (already in the queue).")
        let none = try decode(QueueAddResult.self, #"{"batch": null, "added": [], "skipped": []}"#)
        XCTAssertEqual(none.summary, "Nothing new was added.")
    }

    func testBatchInputFindsIdentifiers() {
        let text = """
        10.1371/journal.pbio.1002106
        https://doi.org/10.1038/nature12373.
        see doi:10.1126/science.169.3946.635 and 10.1016/j.cell.2020.01.001;
        PMC1234567
        31452104
        2101.00001v2
        10.1371/JOURNAL.PBIO.1002106
        """
        XCTAssertEqual(BatchInput.identifiers(in: text), [
            "10.1371/journal.pbio.1002106", "10.1038/nature12373", "10.1126/science.169.3946.635",
            "10.1016/j.cell.2020.01.001", "PMC1234567", "31452104", "2101.00001v2",
        ], "a repeated DOI in another case is counted once")
    }

    func testBatchInputIgnoresProseAndBibTeX() {
        let text = """
        @article{smith2020,
          title = {A study of things},
          year = {2020},
        }
        Please screen these papers when you can.
        1234
        """
        XCTAssertEqual(BatchInput.identifiers(in: text), [])
        XCTAssertEqual(BatchInput.identifiers(in: ""), [])
    }

    // MARK: rulings in the run model

    @MainActor func testFilterAndCountByRuling() throws {
        let m = RunModel(runID: "r1", report: try decode(Report.self, Self.reportJSON))
        XCTAssertTrue(m.supportsRulings)
        var f = FindingFilter()
        f.ruling = "confirmed"
        XCTAssertEqual(m.ordered(f).map(\.id), ["001"])
        f.ruling = "unreviewed"
        XCTAssertEqual(m.ordered(f).map(\.id), ["003"], "notes stay hidden unless asked for")
        f.ruling = "all"
        let counts = m.rulingCounts(f)
        XCTAssertEqual(counts[.confirmed], 1)
        XCTAssertEqual(counts[.falsePositive], 1)
        XCTAssertEqual(counts[.unreviewed], 1)
        XCTAssertNil(counts[.needsRawData])
    }

    @MainActor func testSetRulingSavesToEngineAndAppliesReply() async throws {
        let m = RunModel(runID: "r1", report: try decode(Report.self, Self.reportJSON))
        m.client = { MockEngine.client() }
        MockEngine.reset { _ in
            (200, """
            {"finding": "003", "fingerprint": "fp3", "ruling": {"ruling": "needs_raw_data", "note": "ask", "updated": "2025-01-09T09:00:00"},
             "review": {"counts": {"needs_raw_data": 1}, "total": 3, "reviewed": 3, "score_reviewed": 30, "unmatched": 0}}
            """)
        }
        await m.setRuling(.needsRawData, for: "003", note: "ask")
        let sent = try XCTUnwrap(MockEngine.sent.first)
        XCTAssertEqual(sent.method, "POST")
        XCTAssertEqual(sent.path, "/api/runs/r1/findings/003/ruling")
        XCTAssertEqual(sent.headers[EngineClient.guardHeader.name], EngineClient.guardHeader.value)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: sent.body) as? [String: String])
        XCTAssertEqual(body, ["ruling": "needs_raw_data", "note": "ask"])
        XCTAssertEqual(m.ruling(for: "003").ruling, .needsRawData)
        XCTAssertEqual(m.ruling(for: "003").updated, "2025-01-09T09:00:00")
        XCTAssertEqual(m.review?.reviewed, 3)
        XCTAssertNil(m.rulingError)
    }

    @MainActor func testFailedRulingIsUndone() async throws {
        let m = RunModel(runID: "r1", report: try decode(Report.self, Self.reportJSON))
        m.client = { MockEngine.client() }
        MockEngine.reset { _ in (404, #"{"detail": "no such finding"}"#) }
        await m.setRuling(.falsePositive, for: "001")
        XCTAssertEqual(m.ruling(for: "001").ruling, .confirmed, "the old ruling comes back")
        XCTAssertEqual(m.ruling(for: "001").note, "Same blot")
        XCTAssertEqual(m.rulingError?.message, "no such finding")
    }

    @MainActor func testClearingWithoutNoteUsesDelete() async throws {
        let m = RunModel(runID: "r1", report: try decode(Report.self, Self.reportJSON))
        m.client = { MockEngine.client() }
        MockEngine.reset { _ in (200, #"{"finding": "002", "ruling": {"ruling": "unreviewed", "note": ""}}"#) }
        await m.setRuling(.unreviewed, for: "002")
        XCTAssertEqual(MockEngine.sent.map(\.method), ["DELETE"])
        XCTAssertEqual(m.ruling(for: "002").ruling, .unreviewed)
        // A note on an unreviewed finding is kept, so that goes as a POST.
        MockEngine.reset { _ in (200, #"{"finding": "002", "ruling": {"ruling": "unreviewed", "note": "later"}}"#) }
        await m.setNote("  later ", for: "002")
        XCTAssertEqual(MockEngine.sent.map(\.method), ["POST"])
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: MockEngine.sent[0].body) as? [String: String])
        XCTAssertEqual(body["note"], "later", "the note is trimmed")
        // The same note again sends nothing.
        MockEngine.reset()
        await m.setNote("later", for: "002")
        XCTAssertTrue(MockEngine.sent.isEmpty)
    }

    @MainActor func testUnknownFindingSendsNothing() async throws {
        let m = RunModel(runID: "r1", report: try decode(Report.self, Self.reportJSON))
        m.client = { MockEngine.client() }
        MockEngine.reset()
        await m.setRuling(.confirmed, for: "nope")
        XCTAssertTrue(MockEngine.sent.isEmpty)
    }

    func testLegacyReviewMigratesOnlyWhereEngineHasNothing() {
        let legacy: [String: TriageStore.Entry] = [
            "001": .init(state: "dismissed", note: "old"),     // engine already has a ruling: keep the engine's
            "002": .init(state: "confirmed"),
            "003": .init(state: "dismissed", note: " looks fine "),
            "005": .init(state: "confirmed"),                   // no longer in the scan
            "006": .init(state: "unreviewed", note: ""),
        ]
        let current: [String: RulingInfo] = [
            "001": RulingInfo(ruling: .confirmed), "002": RulingInfo(), "003": RulingInfo(), "006": RulingInfo(),
        ]
        let m = TriageStore.migration(legacy, current: current)
        XCTAssertEqual(m.map(\.id), ["002", "003"])
        XCTAssertEqual(m.map(\.ruling), [.confirmed, .falsePositive])
        XCTAssertEqual(m.map(\.note), ["", "looks fine"])
    }

    // MARK: client routes

    func testClientRoutesAndHeaders() async throws {
        let c = MockEngine.client()
        MockEngine.reset { s in
            switch s.path {
            case "/api/queue": return (200, #"{"batch": null, "added": [{"id": "a", "input": "10.1/a", "status": "queued"}], "skipped": []}"#)
            case "/api/jobs/j1/cancel": return (200, #"{"id": "j1", "status": "running", "stage": "Cancelling..."}"#)
            case "/api/queue/q1/cancel": return (200, #"{"id": "q1", "status": "cancelled"}"#)
            default: return (200, "{}")
            }
        }
        let added = try await c.queueAdd(text: "10.1/a", name: "N", online: false)
        XCTAssertEqual(added.added.count, 1)
        let job = try await c.cancelJob("j1")
        XCTAssertTrue(job.isCancelling)
        let item = try await c.queueCancel("q1")
        XCTAssertEqual(item.status, .cancelled)
        XCTAssertEqual(MockEngine.sent.map(\.method), ["POST", "POST", "POST"])
        XCTAssertTrue(MockEngine.sent.allSatisfy { $0.headers[EngineClient.guardHeader.name] == "1" })
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: MockEngine.sent[0].body) as? [String: Any])
        XCTAssertEqual(body["text"] as? String, "10.1/a")
        XCTAssertEqual(body["name"] as? String, "N")
        XCTAssertEqual(body["online"] as? Bool, false)
    }

    func testGetsCarryNoGuardHeaderAndErrorsKeepDetail() async throws {
        let c = MockEngine.client()
        MockEngine.reset { _ in (400, #"{"detail": "no DOIs found in what was sent"}"#) }
        do {
            _ = try await c.queueAdd(text: "hello", name: "", online: true)
            XCTFail("expected an error")
        } catch let e as AppError {
            XCTAssertEqual(e.status, 400)
            XCTAssertEqual(e.message, "no DOIs found in what was sent")
        }
        MockEngine.reset { _ in (200, #"{"id": "stats.grim", "label": "Impossible mean"}"#) }
        let info = try await c.explain("stats.grim")
        XCTAssertEqual(info.label, "Impossible mean")
        XCTAssertEqual(MockEngine.sent[0].method, "GET")
        XCTAssertNil(MockEngine.sent[0].headers[EngineClient.guardHeader.name])
    }

    func testExportURLs() {
        let c = EngineClient(base: URL(string: "http://127.0.0.1:5000")!)
        let on = ExportOptions(includeNotes: true, includeRulingNotes: true)
        let off = ExportOptions()
        XCTAssertEqual(c.exportURL("r/1", .csv, options: on)?.absoluteString,
                       "http://127.0.0.1:5000/api/runs/r%2F1/export/findings.csv?info=true&notes=true")
        XCTAssertEqual(c.exportURL("r1", .xlsx, options: off)?.absoluteString,
                       "http://127.0.0.1:5000/api/runs/r1/export/findings.xlsx?info=false")
        XCTAssertEqual(c.exportURL("r1", .pdf, options: off)?.absoluteString,
                       "http://127.0.0.1:5000/api/runs/r1/export/report.pdf?info=false&notes=false")
        XCTAssertEqual(c.exportURL("r1", .evidence, options: on)?.absoluteString,
                       "http://127.0.0.1:5000/api/runs/r1/export/evidence.zip?notes=true")
        XCTAssertEqual(c.batchCSVURL("b1").absoluteString, "http://127.0.0.1:5000/api/queue/batches/b1.csv")
    }

    @MainActor func testExportFileNamesAndOptions() {
        XCTAssertEqual(Exporter.fileName(.evidence, run: "abc"), "paperinspector-abc-evidence.zip")
        XCTAssertEqual(Exporter.fileName(.xlsx, run: "abc"), "paperinspector-abc-findings.xlsx")
        XCTAssertEqual(Exporter.fileName(.pdf, run: "abc"), "paperinspector-abc-report.pdf")
        XCTAssertTrue(ExportKind.csv.takesInfo && ExportKind.csv.takesRulingNotes)
        XCTAssertFalse(ExportKind.evidence.takesInfo)
        XCTAssertFalse(ExportKind.xlsx.takesRulingNotes)
        XCTAssertFalse(ExportKind.json.takesInfo || ExportKind.json.takesRulingNotes)
    }
}
