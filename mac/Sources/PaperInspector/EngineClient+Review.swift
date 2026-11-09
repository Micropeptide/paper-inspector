import Foundation

/// The engine routes for rulings, the check catalogue, exports, letters and the batch queue.
extension EngineClient {
    // MARK: transport (a small copy of the private helpers, so this file stays separate)

    func send(_ path: String, method: String = "GET", json body: Any? = nil, timeout: TimeInterval = 30) async throws -> Data {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        req.timeoutInterval = timeout
        if method != "GET" { req.setValue(Self.guardHeader.value, forHTTPHeaderField: Self.guardHeader.name) }
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp, data)
        return data
    }

    private func get<T: Decodable>(_ path: String, timeout: TimeInterval = 30) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await send(path, timeout: timeout))
    }

    private func post<T: Decodable>(_ path: String, json: Any? = nil, method: String = "POST") async throws -> T {
        try JSONDecoder().decode(T.self, from: try await send(path, method: method, json: json))
    }

    // MARK: rulings

    /// Sets a ruling.  A nil note keeps the note already stored; an empty one clears it.
    func setRuling(run: String, finding: String, ruling: Ruling, note: String?) async throws -> RulingResponse {
        var body: [String: Any] = ["ruling": ruling.rawValue]
        if let note { body["note"] = String(note.prefix(4000)) }
        return try await post("/api/runs/\(Self.q(run))/findings/\(Self.q(finding))/ruling", json: body)
    }

    func clearRuling(run: String, finding: String) async throws -> RulingResponse {
        try await post("/api/runs/\(Self.q(run))/findings/\(Self.q(finding))/ruling", method: "DELETE")
    }

    // MARK: catalogue and letters

    func catalog() async throws -> [CheckInfo] { (try await get("/api/catalog") as CatalogResponse).checks }
    func explain(_ check: String) async throws -> CheckInfo { try await get("/api/catalog/\(Self.q(check))") }

    func letter(run: String, kind: LetterKind, includeNeedsData: Bool) async throws -> Letter {
        try await get("/api/runs/\(Self.q(run))/letters/\(kind.rawValue)?needs_data=\(includeNeedsData)")
    }

    // MARK: exports

    func exportURL(_ run: String, _ kind: ExportKind, options: ExportOptions) -> URL? {
        let r = Self.q(run)
        switch kind {
        case .csv: return url("/api/runs/\(r)/export/findings.csv?info=\(options.includeNotes)&notes=\(options.includeRulingNotes)")
        case .xlsx: return url("/api/runs/\(r)/export/findings.xlsx?info=\(options.includeNotes)")
        case .pdf: return url("/api/runs/\(r)/export/report.pdf?info=\(options.includeNotes)&notes=\(options.includeRulingNotes)")
        case .evidence: return url("/api/runs/\(r)/export/evidence.zip?notes=\(options.includeRulingNotes)")
        case .html: return reportURL(run)
        case .json: return url("/api/runs/\(r)")
        }
    }

    /// Downloads with a long timeout: an evidence pack is built before its first byte is sent.
    func downloadExport(_ remote: URL, to dest: URL) async throws -> URL {
        var req = URLRequest(url: remote)
        req.timeoutInterval = 600
        let (tmp, resp) = try await session.download(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let body = (try? Data(contentsOf: tmp)) ?? Data()
            try? FileManager.default.removeItem(at: tmp)
            try Self.check(resp, body)
        }
        let fm = FileManager.default
        try fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.removeItem(at: dest)
        try fm.moveItem(at: tmp, to: dest)
        return dest
    }

    // MARK: batch queue

    func queueAdd(text: String, name: String, online: Bool) async throws -> QueueAddResult {
        try await post("/api/queue", json: ["text": text, "name": name, "online": online])
    }

    func queue(batch: String? = nil) async throws -> QueueList {
        try await get(batch.map { "/api/queue?batch=\(Self.q($0))" } ?? "/api/queue", timeout: 10)
    }

    @discardableResult
    func queueCancel(_ id: String) async throws -> QueueItem { try await post("/api/queue/\(Self.q(id))/cancel") }
    @discardableResult
    func queueRetry(_ id: String) async throws -> QueueItem { try await post("/api/queue/\(Self.q(id))/retry") }
    func queueRemove(_ id: String) async throws { _ = try await send("/api/queue/\(Self.q(id))", method: "DELETE") }

    @discardableResult
    func queueClear() async throws -> Int {
        let d = try await send("/api/queue/clear", method: "POST")
        return ((try? JSONSerialization.jsonObject(with: d)) as? [String: Any])?["removed"] as? Int ?? 0
    }

    func batchCSVURL(_ batch: String) -> URL { url("/api/queue/batches/\(Self.q(batch)).csv") }
}
