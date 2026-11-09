import Foundation
import SwiftUI

// Models for the reviewer's rulings, the check catalogue, letters and the batch queue.  All decode leniently, like
// the report models, so a newer or older engine never stops the app from opening a scan.

/// A person's ruling on one finding.  The engine stores it with the paper, so a rescan keeps it.
enum Ruling: String, Codable, CaseIterable, Identifiable, Hashable {
    case unreviewed, confirmed, falsePositive = "false_positive", needsRawData = "needs_raw_data"

    var id: String { rawValue }

    init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = Ruling(rawValue: raw) ?? .unreviewed
    }

    var label: String {
        switch self {
        case .unreviewed: return "Not Reviewed"
        case .confirmed: return "Confirmed"
        case .falsePositive: return "False Positive"
        case .needsRawData: return "Needs Raw Data"
        }
    }

    /// For sentences: "You marked this false positive."
    var phrase: String {
        switch self {
        case .unreviewed: return "not reviewed"
        case .confirmed: return "confirmed"
        case .falsePositive: return "a false positive"
        case .needsRawData: return "needing the raw data"
        }
    }

    var help: String {
        switch self {
        case .unreviewed: return "You haven't ruled on this finding yet."
        case .confirmed: return "You looked and agree there is a problem here."
        case .falsePositive: return "You looked and the check was wrong. It no longer counts toward the score."
        case .needsRawData: return "You can't decide without the original images or data."
        }
    }

    var symbol: String {
        switch self {
        case .unreviewed: return "circle.dashed"
        case .confirmed: return "exclamationmark.circle.fill"
        case .falsePositive: return "xmark.circle"
        case .needsRawData: return "questionmark.circle.fill"
        }
    }

    var color: Color {
        switch self {
        case .unreviewed: return .secondary
        case .confirmed: return Theme.critical
        case .falsePositive: return Theme.calm
        case .needsRawData: return Theme.medium
        }
    }

    /// The rulings a reviewer sets, in menu order (⌘1, ⌘2, ⌘3).
    static let decisions: [Ruling] = [.confirmed, .falsePositive, .needsRawData]

    /// Earlier versions of the app kept a local review state; this maps it onto a ruling.
    static func fromLegacy(_ state: String) -> Ruling? {
        switch state {
        case "confirmed": return .confirmed
        case "dismissed": return .falsePositive
        default: return nil
        }
    }
}

struct RulingInfo: Codable, Hashable {
    var ruling: Ruling = .unreviewed
    var note: String = ""
    var updated: String?

    enum CodingKeys: String, CodingKey { case ruling, note, updated }
    init(ruling: Ruling = .unreviewed, note: String = "", updated: String? = nil) {
        self.ruling = ruling; self.note = note; self.updated = updated
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ruling = c.value(.ruling, .unreviewed); note = c.value(.note, ""); updated = c.optional(.updated)
    }
}

/// The report's `review` block: how many findings have a ruling, and the score once false positives are dropped.
struct ReviewSummary: Decodable, Hashable {
    var counts: [String: Int]
    var total: Int
    var reviewed: Int
    var scoreReviewed: Int?
    var unmatched: Int

    enum CodingKeys: String, CodingKey { case counts, total, reviewed, score_reviewed, unmatched }
    init(counts: [String: Int] = [:], total: Int = 0, reviewed: Int = 0, scoreReviewed: Int? = nil, unmatched: Int = 0) {
        self.counts = counts; self.total = total; self.reviewed = reviewed; self.scoreReviewed = scoreReviewed
        self.unmatched = unmatched
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        counts = c.value(.counts, [:]); total = c.value(.total, 0); reviewed = c.value(.reviewed, 0)
        scoreReviewed = c.optional(.score_reviewed); unmatched = c.value(.unmatched, 0)
    }

    func count(_ r: Ruling) -> Int { counts[r.rawValue] ?? 0 }
}

/// What POST/DELETE …/findings/{fid}/ruling returns.
struct RulingResponse: Decodable {
    var finding: String
    var fingerprint: String?
    var ruling: RulingInfo
    var review: ReviewSummary?

    enum CodingKeys: String, CodingKey { case finding, fingerprint, ruling, review }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        finding = c.value(.finding, ""); fingerprint = c.optional(.fingerprint)
        ruling = c.value(.ruling, RulingInfo()); review = c.optional(.review)
    }
}

/// One check, explained in plain words (GET /api/catalog/{check}).
struct CheckInfo: Decodable, Hashable, Identifiable {
    var id: String
    var label: String
    var category: String
    var categoryLabel: String
    var kind: String
    var scored: Bool
    var what: String
    var falseAlarms: String
    var lookAt: String
    var ask: String
    var known: Bool

    enum CodingKeys: String, CodingKey {
        case id, label, category, category_label, kind, scored, what, false_alarms, look_at, ask, known
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = c.value(.label, id); category = c.value(.category, ""); categoryLabel = c.value(.category_label, category)
        kind = c.value(.kind, "integrity"); scored = c.value(.scored, false); what = c.value(.what, "")
        falseAlarms = c.value(.false_alarms, ""); lookAt = c.value(.look_at, ""); ask = c.value(.ask, "")
        known = c.value(.known, false)
    }
}

struct CatalogResponse: Decodable {
    var checks: [CheckInfo]
    enum CodingKeys: String, CodingKey { case checks }
    init(from decoder: Decoder) throws {
        checks = try decoder.container(keyedBy: CodingKeys.self).lossy(.checks)
    }
}

/// A drafted letter (GET /api/runs/{rid}/letters/{kind}).
struct Letter: Decodable, Hashable {
    var kind: String
    var label: String
    var subject: String
    var body: String
    var included: [String]
    var empty: Bool

    enum CodingKeys: String, CodingKey { case kind, label, subject, body, included, empty }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, ""); label = c.value(.label, "Letter"); subject = c.value(.subject, "")
        body = c.value(.body, ""); included = c.value(.included, []); empty = c.value(.empty, false)
    }

    /// Subject line and body as one plain-text document.
    var text: String { subject.isEmpty ? body : "Subject: \(subject)\n\n\(body)" }
}

enum LetterKind: String, CaseIterable, Identifiable {
    case editor, authors
    var id: String { rawValue }
    var menuLabel: String { self == .editor ? "Letter to the Editor…" : "Letter to the Authors…" }
    var title: String { self == .editor ? "Letter to the Editor" : "Letter to the Authors" }
}

// MARK: batch queue

enum QueueStatus: String, Decodable, CaseIterable {
    case queued, running, done, error, cancelled, unknown
    init(from decoder: Decoder) throws {
        self = QueueStatus(rawValue: (try? decoder.singleValueContainer().decode(String.self)) ?? "") ?? .unknown
    }
    var isActive: Bool { self == .queued || self == .running }
    var isFinished: Bool { self == .done || self == .error || self == .cancelled }

    var label: String {
        switch self {
        case .queued: return "Waiting"
        case .running: return "Scanning"
        case .done: return "Done"
        case .error: return "Failed"
        case .cancelled: return "Cancelled"
        case .unknown: return "Unknown"
        }
    }

    var symbol: String {
        switch self {
        case .queued: return "clock"
        case .running: return "arrow.triangle.2.circlepath"
        case .done: return "checkmark.circle.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .cancelled: return "slash.circle"
        case .unknown: return "questionmark.circle"
        }
    }

    /// Sidebar order: what is happening now first, then what needs attention, then what is finished.
    var rank: Int {
        switch self {
        case .running: return 0
        case .queued: return 1
        case .error: return 2
        case .cancelled: return 3
        case .done: return 4
        case .unknown: return 5
        }
    }
}

struct QueueItem: Decodable, Identifiable, Hashable {
    var id: String
    var batch: String
    var input: String
    var doi: String?
    var online: Bool
    var status: QueueStatus
    var stage: String
    var progress: Double
    var run: String?
    var error: String?
    var added: String
    var started: String?
    var finished: String?
    var title: String?
    var score: Int?

    enum CodingKeys: String, CodingKey {
        case id, batch, input, doi, online, status, stage, progress, run, error, added, started, finished, title, score
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        batch = c.value(.batch, ""); input = c.value(.input, ""); doi = c.optional(.doi); online = c.value(.online, true)
        status = c.value(.status, .unknown); stage = c.value(.stage, ""); progress = c.value(.progress, 0)
        run = c.optional(.run); error = c.optional(.error); added = c.value(.added, "")
        started = c.optional(.started); finished = c.optional(.finished); title = c.optional(.title)
        score = c.optional(.score)
    }

    var displayTitle: String {
        if let t = title, !t.isEmpty { return t }
        if let d = doi, !d.isEmpty { return d }
        return input
    }

    /// The line under the title: the stage while working, the reason after a failure.
    var caption: String {
        switch status {
        case .running: return stage.isEmpty ? "Scanning" : stage
        case .queued: return stage.isEmpty || stage.lowercased() == "queued" ? "Waiting" : stage
        case .error: return error.map { "Failed: \($0)" } ?? "Failed"
        case .cancelled: return "Cancelled"
        case .done: return score.map { "Done · integrity score \($0)" } ?? "Done"
        case .unknown: return stage
        }
    }
}

struct QueueBatch: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var created: String
    var count: Int

    enum CodingKeys: String, CodingKey { case id, name, created, count }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = c.value(.name, ""); created = c.value(.created, ""); count = c.value(.count, 0)
    }

    var displayName: String { name.isEmpty ? "Batch of \(count)" : name }
}

struct QueueList: Decodable {
    var items: [QueueItem]
    var counts: [String: Int]
    var running: String?
    var batches: [QueueBatch]

    enum CodingKeys: String, CodingKey { case items, counts, running, batches }
    init(items: [QueueItem] = [], counts: [String: Int] = [:], running: String? = nil, batches: [QueueBatch] = []) {
        self.items = items; self.counts = counts; self.running = running; self.batches = batches
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = c.lossy(.items); counts = c.value(.counts, [:]); running = c.optional(.running); batches = c.lossy(.batches)
    }
}

struct QueueSkip: Decodable, Hashable {
    var input: String
    var reason: String
    enum CodingKeys: String, CodingKey { case input, reason }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        input = c.value(.input, ""); reason = c.value(.reason, "")
    }
}

struct QueueAddResult: Decodable {
    var batch: QueueBatch?
    var added: [QueueItem]
    var skipped: [QueueSkip]

    enum CodingKeys: String, CodingKey { case batch, added, skipped }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        batch = c.optional(.batch); added = c.lossy(.added); skipped = c.lossy(.skipped)
    }

    /// One plain sentence for the home view.
    var summary: String {
        var s = added.isEmpty ? "Nothing new was added." : "Added \(added.count) paper\(added.count == 1 ? "" : "s") to the queue."
        if !skipped.isEmpty {
            let shown = skipped.prefix(3).map { "\($0.input) (\($0.reason))" }.joined(separator: "; ")
            s += " Skipped \(skipped.count): \(shown)\(skipped.count > 3 ? "; …" : "")."
        }
        return s
    }
}

/// Rough, local count of the identifiers in pasted text, for the button label.  The engine does the real parsing.
enum BatchInput {
    private static let doi = try! NSRegularExpression(pattern: #"10\.\d{4,9}/[^\s"'<>{},;]+"#, options: [.caseInsensitive])
    private static let other = try! NSRegularExpression(
        pattern: #"^(?:\d{5,9}|PMC\d{4,10}|arxiv:\s*\S+|\d{4}\.\d{4,5}(?:v\d+)?|https?://\S+)$"#, options: [.caseInsensitive])

    /// DOIs anywhere in the text, plus whole lines that look like a PubMed, PMC or arXiv id or a link.
    static func identifiers(in text: String) -> [String] {
        var out: [String] = []
        var seen = Set<String>()
        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            let ns = line as NSString
            let dois = doi.matches(in: line, range: NSRange(location: 0, length: ns.length)).map {
                ns.substring(with: $0.range).trimmingCharacters(in: CharacterSet(charactersIn: ".)]"))
            }
            let found = dois.isEmpty && other.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) != nil
                ? [line] : dois
            for f in found where seen.insert(f.lowercased()).inserted { out.append(f) }
        }
        return out
    }
}
