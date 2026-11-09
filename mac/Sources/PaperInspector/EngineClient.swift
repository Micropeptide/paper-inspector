import Foundation
import UniformTypeIdentifiers

/// One error type for everything the user can see, with enough context to offer the right way out.
struct AppError: LocalizedError, Equatable {
    var title: String
    var message: String
    var status: Int? = nil
    var suggestsLog = false
    var suggestsSettings = false

    var errorDescription: String? { message }

    static func from(_ error: Error) -> AppError {
        if let e = error as? AppError { return e }
        if let u = error as? URLError {
            switch u.code {
            case .cannotConnectToHost, .networkConnectionLost, .cannotFindHost, .notConnectedToInternet:
                return AppError(title: "The analysis engine isn't answering",
                                message: "It may have stopped. Restart it from the sidebar, or check the engine log.",
                                suggestsLog: true)
            case .timedOut:
                return AppError(title: "The engine took too long", message: u.localizedDescription, suggestsLog: true)
            default:
                return AppError(title: "Couldn't reach the engine", message: u.localizedDescription, suggestsLog: true)
            }
        }
        if error is DecodingError {
            return AppError(title: "This scan can't be read",
                            message: "It was probably saved by a different version of the engine. Re-run the scan to update it.")
        }
        return AppError(title: "Something went wrong", message: error.localizedDescription)
    }
}

/// Typed access to the engine's local HTTP API.  Calls run off the main actor, so decoding and multipart building
/// never block the UI.  Every POST and DELETE carries the header the engine requires to block cross-site requests.
struct EngineClient: Sendable {
    let base: URL
    /// Tests pass a session whose protocol classes answer without a network.
    var session: URLSession = EngineClient.sharedSession

    static let guardHeader = (name: "X-Paper-Inspector", value: "1")

    static let sharedSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 60
        c.timeoutIntervalForResource = 3600
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: c)
    }()

    func url(_ path: String) -> URL { URL(string: path, relativeTo: base)?.absoluteURL ?? base }

    static func q(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(.init(charactersIn: "._-"))) ?? s
    }

    // MARK: transport

    private func request(_ path: String, method: String = "GET", body: Data? = nil, contentType: String? = nil,
                         timeout: TimeInterval = 60) -> URLRequest {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        req.httpBody = body
        req.timeoutInterval = timeout
        if method != "GET" { req.setValue(Self.guardHeader.value, forHTTPHeaderField: Self.guardHeader.name) }
        if let contentType { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        return req
    }

    private func data(_ req: URLRequest) async throws -> Data {
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp, data)
        return data
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T { try JSONDecoder().decode(T.self, from: data) }

    static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) else { return }
        var message = HTTPURLResponse.localizedString(forStatusCode: http.statusCode).capitalized
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let d = obj["detail"] as? String { message = d }
            else if let d = obj["detail"] as? [[String: Any]], let m = d.first?["msg"] as? String { message = m }
        }
        let server = http.statusCode >= 500
        throw AppError(title: server ? "The engine hit an error" : "The engine refused the request",
                       message: message, status: http.statusCode, suggestsLog: server)
    }

    private func json(_ object: Any) throws -> Data { try JSONSerialization.data(withJSONObject: object) }

    // MARK: API

    func health() async -> String? {
        var req = request("/api/health", timeout: 1.5)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (data, resp) = try? await session.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return obj["version"] as? String ?? "?"
    }

    func runs() async throws -> [RunSummary] {
        let d = try await data(request("/api/runs"))
        return (try? JSONDecoder().decode(LossyList<RunSummary>.self, from: d).items) ?? []
    }

    func report(_ id: String) async throws -> Report { try decode(try await data(request("/api/runs/\(Self.q(id))"))) }
    func job(_ id: String) async throws -> EngineJob { try decode(try await data(request("/api/jobs/\(Self.q(id))"))) }
    func demo() async throws -> EngineJob { try decode(try await data(request("/api/demo", method: "POST"))) }

    func scanDOI(_ doi: String) async throws -> EngineJob {
        try decode(try await data(request("/api/doi", method: "POST", body: try json(["doi": doi]),
                                          contentType: "application/json")))
    }

    func investigate(_ id: String) async throws -> EngineJob {
        try decode(try await data(request("/api/runs/\(Self.q(id))/investigate", method: "POST")))
    }

    /// Asks the engine to stop a job.  A running scan keeps the status "running" (stage "Cancelling...") until it
    /// reaches its next step; a finished job comes back unchanged.
    @discardableResult
    func cancelJob(_ id: String) async throws -> EngineJob {
        try decode(try await data(request("/api/jobs/\(Self.q(id))/cancel", method: "POST", timeout: 10)))
    }

    func deleteRun(_ id: String) async throws {
        _ = try await data(request("/api/runs/\(Self.q(id))", method: "DELETE"))
    }

    func compare(_ run: String, _ a: String, _ b: String) async throws -> CompareResult {
        try decode(try await data(request("/api/runs/\(Self.q(run))/compare?a=\(Self.q(a))&b=\(Self.q(b))", timeout: 120)))
    }

    func pubpeer(_ run: String) async throws -> String {
        String(decoding: try await data(request("/api/runs/\(Self.q(run))/pubpeer")), as: UTF8.self)
    }

    func reportHTML(_ run: String) async throws -> String {
        String(decoding: try await data(request("/api/runs/\(Self.q(run))/report.html")), as: UTF8.self)
    }

    func settings() async throws -> EngineSettings { try decode(try await data(request("/api/settings"))) }

    /// Patch the engine's settings.  Keys go only to the engine (settings.json, mode 600); an empty key removes it.
    func saveSettings(json patch: Data) async throws -> EngineSettings {
        try decode(try await data(request("/api/settings", method: "POST", body: patch, contentType: "application/json")))
    }

    /// Download any engine URL to `dest`, replacing what is there.
    func download(_ remote: URL, to dest: URL) async throws -> URL {
        let (tmp, _) = try await fetchFile(remote)
        return try Self.move(tmp, to: dest)
    }

    /// Download into `folder` as `baseName` plus the extension the engine's response names (the source can be a
    /// PDF or an image).
    func download(_ remote: URL, into folder: URL, baseName: String) async throws -> URL {
        let (tmp, resp) = try await fetchFile(remote)
        let suggested = resp.suggestedFilename.map { ($0 as NSString).pathExtension } ?? ""
        let ext = !suggested.isEmpty ? suggested
            : resp.mimeType.flatMap { UTType(mimeType: $0)?.preferredFilenameExtension } ?? "bin"
        return try Self.move(tmp, to: folder.appendingPathComponent(baseName).appendingPathExtension(ext))
    }

    private func fetchFile(_ remote: URL) async throws -> (URL, URLResponse) {
        let (tmp, resp) = try await session.download(from: remote)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let body = (try? Data(contentsOf: tmp)) ?? Data()
            try? FileManager.default.removeItem(at: tmp)
            try Self.check(resp, body)
        }
        return (tmp, resp)
    }

    private static func move(_ tmp: URL, to dest: URL) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.removeItem(at: dest)
        try fm.moveItem(at: tmp, to: dest)
        return dest
    }

    // MARK: upload

    /// One paper (main) with its supplementary files, or several papers compared as a case.  The body is streamed
    /// into a temporary file and uploaded from disk, so large supplements never sit in memory.
    func upload(main: URL?, others: [URL], asCase: Bool,
                progress: @escaping @Sendable (Double) -> Void) async throws -> EngineJob {
        let boundary = "paperinspector-\(UUID().uuidString)"
        var parts: [(String, URL)] = []
        if asCase {
            parts = others.map { ("files", $0) }
        } else if let main {
            parts = [("file", main)] + others.map { ("supplements", $0) }
        }
        let body = try Self.writeMultipart(parts, boundary: boundary)
        defer { try? FileManager.default.removeItem(at: body) }
        var req = request(asCase ? "/api/case" : "/api/scan", method: "POST",
                          contentType: "multipart/form-data; boundary=\(boundary)", timeout: 600)
        req.httpBody = nil
        let delegate = UploadProgress(progress)
        let (data, resp) = try await session.upload(for: req, fromFile: body, delegate: delegate)
        try Self.check(resp, data)
        return try decode(data)
    }

    static func writeMultipart(_ parts: [(field: String, file: URL)], boundary: String) throws -> URL {
        let out = FileManager.default.temporaryDirectory.appendingPathComponent("pi-upload-\(UUID().uuidString)")
        guard FileManager.default.createFile(atPath: out.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw AppError(title: "Couldn't prepare the upload", message: "No space for a temporary file.")
        }
        let w = try FileHandle(forWritingTo: out)
        defer { try? w.close() }
        do {
            for (field, file) in parts {
                let name = file.lastPathComponent
                let head = "--\(boundary)\r\nContent-Disposition: form-data; name=\"\(field)\"; "
                    + "filename=\"\(dispositionFilename(name))\"; filename*=UTF-8''\(rfc5987(name))\r\n"
                    + "Content-Type: \(contentType(for: file))\r\n\r\n"
                try w.write(contentsOf: Data(head.utf8))
                let r = try FileHandle(forReadingFrom: file)
                defer { try? r.close() }
                while let chunk = try r.read(upToCount: 1 << 20), !chunk.isEmpty { try w.write(contentsOf: chunk) }
                try w.write(contentsOf: Data("\r\n".utf8))
            }
            try w.write(contentsOf: Data("--\(boundary)--\r\n".utf8))
        } catch {
            try? FileManager.default.removeItem(at: out)
            throw AppError(title: "Couldn't read a file", message: error.localizedDescription)
        }
        return out
    }

    /// A filename safe inside a quoted header value: no quotes, backslashes or line breaks (UTF-8 is kept, which is
    /// what the engine's multipart parser decodes).
    static func dispositionFilename(_ name: String) -> String {
        var s = ""
        for ch in name {
            switch ch {
            case "\"": s.append("'")
            case "\\", "\r", "\n", "\r\n", "\0": s.append("_")
            default: s.append(ch)
            }
        }
        return s.isEmpty ? "upload" : s
    }

    static func rfc5987(_ name: String) -> String {
        name.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(.init(charactersIn: ".-_~"))) ?? "upload"
    }

    static func contentType(for file: URL) -> String {
        UTType(filenameExtension: file.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
    }
}

private struct LossyList<T: Decodable>: Decodable {
    var items: [T] = []
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        while !c.isAtEnd {
            if let v = try? c.decode(T.self) { items.append(v) } else if (try? c.decode(SkipOne.self)) == nil { break }
        }
    }
    private struct SkipOne: Decodable { init(from decoder: Decoder) throws {} }
}

private final class UploadProgress: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let report: @Sendable (Double) -> Void
    init(_ report: @escaping @Sendable (Double) -> Void) { self.report = report }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        report(Double(totalBytesSent) / Double(totalBytesExpectedToSend))
    }
}

extension EngineClient {
    func figureURL(_ run: String, _ fig: String, mode: ViewMode = .original) -> URL {
        mode == .original ? url("/api/runs/\(Self.q(run))/figures/\(Self.q(fig)).png")
            : url("/api/runs/\(Self.q(run))/figures/\(Self.q(fig))/view.png?mode=\(mode.rawValue)")
    }
    func frameURL(_ run: String, _ which: String, _ a: String, _ b: String) -> URL {
        url("/api/runs/\(Self.q(run))/compare/\(which).png?a=\(Self.q(a))&b=\(Self.q(b))")
    }
    func evidenceURL(_ run: String, _ name: String) -> URL { url("/api/runs/\(Self.q(run))/evidence/\(Self.q(name))") }
    func reportURL(_ run: String) -> URL { url("/api/runs/\(Self.q(run))/report.html") }
    func supplementURL(_ run: String, _ name: String) -> URL { url("/api/runs/\(Self.q(run))/supplements/\(Self.q(name))") }
    func sourceURL(_ run: String) -> URL { url("/api/runs/\(Self.q(run))/source") }
}
