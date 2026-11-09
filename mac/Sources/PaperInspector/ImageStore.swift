import AppKit
import Foundation
import UniformTypeIdentifiers

/// Images from the engine, cached with a memory budget so browsing many figures and view modes cannot grow
/// without bound.  Concurrent requests for one URL share a single download.
@MainActor
final class ImageStore {
    static let shared = ImageStore()

    private let cache: NSCache<NSURL, NSImage> = {
        let c = NSCache<NSURL, NSImage>()
        c.totalCostLimit = 300 * 1024 * 1024
        return c
    }()
    private var inFlight: [URL: Task<Data?, Never>] = [:]
    private var byRun: [String: Set<URL>] = [:]

    func cached(_ url: URL) -> NSImage? { cache.object(forKey: url as NSURL) }

    func load(_ url: URL) async -> NSImage? {
        if let i = cache.object(forKey: url as NSURL) { return i }
        let task: Task<Data?, Never>
        if let t = inFlight[url] {
            task = t
        } else {
            task = Task.detached {
                guard let (data, resp) = try? await URLSession.shared.data(from: url),
                      (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
                return data
            }
            inFlight[url] = task
        }
        let data = await task.value
        inFlight[url] = nil
        if let i = cache.object(forKey: url as NSURL) { return i }
        guard let data, let img = NSImage(data: data) else { return nil }
        let px = img.representations.first.map { $0.pixelsWide * $0.pixelsHigh * 4 } ?? data.count
        cache.setObject(img, forKey: url as NSURL, cost: max(px, data.count))
        if let run = Self.run(of: url) { byRun[run, default: []].insert(url) }
        return img
    }

    func purge(run: String) {
        for u in byRun.removeValue(forKey: run) ?? [] { cache.removeObject(forKey: u as NSURL) }
    }

    func purgeAll() {
        cache.removeAllObjects()
        byRun.removeAll()
    }

    private static func run(of url: URL) -> String? {
        let c = url.pathComponents
        guard let i = c.firstIndex(of: "runs"), i + 1 < c.count else { return nil }
        return c[i + 1]
    }
}

/// Copies engine files (source PDF, supplements, evidence, the HTML report) into the app's cache folder so they can
/// be previewed with Quick Look, opened in another app, dragged out or shared.
@MainActor
enum FileFetcher {
    nonisolated static var cacheRoot: URL {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent(Bundle.main.bundleIdentifier ?? "app.paperinspector.mac")
    }

    nonisolated static func folder(run: String) -> URL { cacheRoot.appendingPathComponent(safe(run), isDirectory: true) }

    nonisolated static func safe(_ name: String) -> String {
        let s = name.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: ":", with: "_")
        return s.hasPrefix(".") ? "_" + s : (s.isEmpty ? "file" : s)
    }

    /// The local copy of `remote`, downloading it first unless `refresh` is false and a copy exists.
    static func local(_ remote: URL, run: String, name: String, refresh: Bool = false) async throws -> URL {
        let dest = folder(run: run).appendingPathComponent(safe(name))
        if !refresh, FileManager.default.fileExists(atPath: dest.path) { return dest }
        return try await EngineController.shared.client.download(remote, to: dest)
    }

    /// The paper itself; its extension comes from the engine's content type.
    static func source(run: String) async throws -> URL {
        let client = EngineController.shared.client
        let dir = folder(run: run).appendingPathComponent("paper", isDirectory: true)
        if let existing = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .first(where: { $0.deletingPathExtension().lastPathComponent == "source" }) {
            return existing
        }
        return try await client.download(client.sourceURL(run), into: dir, baseName: "source")
    }

    static func report(run: String) async throws -> URL {
        try await local(EngineController.shared.client.reportURL(run), run: run, name: "paperinspector-\(run).html",
                        refresh: true)
    }

    nonisolated static func purge(run: String) { try? FileManager.default.removeItem(at: folder(run: run)) }
    nonisolated static func purgeAll() { try? FileManager.default.removeItem(at: cacheRoot) }
}

