import CoreGraphics
import Foundation

// Every model decodes leniently: a missing or mistyped field falls back to a default, so a report written by an
// older or newer engine still opens instead of failing as a whole.

extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T { (try? decodeIfPresent(T.self, forKey: key)) ?? fallback }
    func optional<T: Decodable>(_ key: Key) -> T? { (try? decodeIfPresent(T.self, forKey: key)) ?? nil }

    /// An array that skips elements it cannot decode rather than dropping the whole array.
    func lossy<T: Decodable>(_ key: Key) -> [T] {
        guard var c = try? nestedUnkeyedContainer(forKey: key) else { return [] }
        var out: [T] = []
        while !c.isAtEnd {
            if let v = try? c.decode(T.self) { out.append(v) } else if (try? c.decode(Skip.self)) == nil { break }
        }
        return out
    }
}

private struct Skip: Decodable { init(from decoder: Decoder) throws {} }

extension Array where Element == Double {
    /// `[x, y, w, h]` as a rectangle; nil for short, negative or non-finite boxes.
    var rect: CGRect? {
        guard count >= 4, prefix(4).allSatisfy(\.isFinite), self[2] >= 0, self[3] >= 0 else { return nil }
        return CGRect(x: self[0], y: self[1], width: self[2], height: self[3])
    }
}

enum Severity: String, Decodable, CaseIterable, Comparable, Hashable {
    case critical, high, medium, low, info, unknown

    init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = Severity(rawValue: raw.lowercased()) ?? .unknown
    }

    /// Most severe first.
    static func < (a: Severity, b: Severity) -> Bool { a.rank < b.rank }
    private var rank: Int { Severity.allCases.firstIndex(of: self) ?? 99 }
    var label: String { self == .unknown ? "Unrated" : rawValue.capitalized }
    var isNote: Bool { self == .info }
}

struct Loc: Decodable, Hashable {
    var figure: String?
    var panel: String?
    var page: Int?
    var box: [Double]?
    var quote: String?
    var file: String?
    var sheet: String?
    var cells: String?

    enum CodingKeys: String, CodingKey { case figure, panel, page, box, quote, file, sheet, cells }

    init(figure: String? = nil, panel: String? = nil, page: Int? = nil, box: [Double]? = nil, quote: String? = nil) {
        self.figure = figure; self.panel = panel; self.page = page; self.box = box; self.quote = quote
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        figure = c.optional(.figure); panel = c.optional(.panel); page = c.optional(.page); box = c.optional(.box)
        quote = c.optional(.quote); file = c.optional(.file); sheet = c.optional(.sheet); cells = c.optional(.cells)
    }
}

struct AIVerdict: Decodable, Hashable {
    var verdict: String
    var reason: String

    enum CodingKeys: String, CodingKey { case verdict, reason }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        verdict = c.value(.verdict, ""); reason = c.value(.reason, "")
    }

    var isFalsePositive: Bool { verdict == "false_positive" }
    var label: String { verdict.replacingOccurrences(of: "_", with: " ").capitalized }
}

struct Finding: Decodable, Identifiable, Hashable {
    var id: String
    var check: String
    var severity: Severity
    var category: String?
    var kind: String
    var title: String
    var detail: String
    var confidence: Double
    var locations: [Loc]
    var evidence: [String]
    var ai: AIVerdict?
    var data: [String: JSONValue]
    /// Stable across rescans of the same paper; rulings are stored against it.  Nil from older engines.
    var fingerprint: String?
    /// The reviewer's ruling as the engine stored it when the report was fetched.
    var ruling: RulingInfo

    enum CodingKeys: String, CodingKey {
        case id, check, severity, category, kind, title, detail, confidence, locations, evidence, ai, data
        case fingerprint, ruling
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        check = c.value(.check, "")
        severity = c.value(.severity, .unknown)
        category = c.optional(.category)
        kind = c.value(.kind, "integrity")
        title = c.value(.title, "Untitled finding")
        detail = c.value(.detail, "")
        confidence = c.value(.confidence, 0)
        locations = c.lossy(.locations)
        evidence = c.value(.evidence, [])
        ai = c.optional(.ai)
        data = c.value(.data, [:])
        fingerprint = c.optional(.fingerprint)
        ruling = c.value(.ruling, RulingInfo())
    }

    var panels: [String] { locations.compactMap(\.panel) }
    var figures: [String] {
        var seen: [String] = []
        for f in locations.compactMap(\.figure) where !seen.contains(f) { seen.append(f) }
        return seen
    }
    var kindName: String { checkNames[check] ?? check }
    var isPair: Bool { check == "image.duplicate" && panels.count == 2 }
    var textLocation: Loc? { locations.first { $0.quote != nil || ($0.page != nil && $0.figure == nil) } }
    var plainText: String {
        var s = "\(severity.label) – \(kindName) – \(title)\n\(detail)"
        let where_ = (figures + panels).joined(separator: ", ")
        if !where_.isEmpty { s += "\n(\(where_))" }
        return s
    }
}

struct PanelInfo: Decodable, Identifiable, Hashable {
    var id: String
    var figure: String
    var box: [Double]
    var kind: String

    enum CodingKeys: String, CodingKey { case id, figure, box, kind }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        figure = c.value(.figure, ""); box = c.value(.box, []); kind = c.value(.kind, "panel")
    }
}

struct FigureInfo: Decodable, Identifiable, Hashable {
    var id: String
    var label: String
    var page: Int
    var caption: String
    var width: Double
    var height: Double
    var panels: [PanelInfo]

    enum CodingKeys: String, CodingKey { case id, label, page, caption, width, height, panels }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = c.value(.label, id); page = c.value(.page, 0); caption = c.value(.caption, "")
        width = c.value(.width, 0); height = c.value(.height, 0); panels = c.lossy(.panels)
    }

    var hasSize: Bool { width.isFinite && height.isFinite && width > 0 && height > 0 }
}

struct PaperInfo: Decodable, Hashable {
    var source: String = ""
    var title: String = ""
    var doi: String = ""
    var pages: Int = 0

    enum CodingKeys: String, CodingKey { case source, title, doi, pages }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        source = c.value(.source, ""); title = c.value(.title, ""); doi = c.value(.doi, ""); pages = c.value(.pages, 0)
    }
}

struct AIReview: Decodable, Hashable {
    var model: String
    var summary: String

    enum CodingKeys: String, CodingKey { case model, summary }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = c.value(.model, "AI model"); summary = c.value(.summary, "")
    }
}

struct Author: Decodable, Hashable {
    var given: String?
    var family: String?
    var name: String { [given, family].compactMap { $0 }.joined(separator: " ") }
}

struct Notice: Decodable, Hashable {
    var type: String
    var date: String?

    enum CodingKeys: String, CodingKey { case type, date }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = c.value(.type, "notice"); date = c.optional(.date)
    }
}

struct PaperMeta: Decodable, Hashable {
    var doi: String?
    var title: String?
    var journal: String?
    var issued: String?
    var authors: [Author]
    var notices: [Notice]
    var isRetracted: Bool
    var citedBy: Int?

    enum CodingKeys: String, CodingKey {
        case doi, title, journal, issued, authors, notices, is_retracted, cited_by_count
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        doi = c.optional(.doi); title = c.optional(.title); journal = c.optional(.journal); issued = c.optional(.issued)
        authors = c.lossy(.authors); notices = c.lossy(.notices)
        isRetracted = c.value(.is_retracted, false); citedBy = c.optional(.cited_by_count)
    }
}

struct RefStats: Decodable, Hashable {
    var count: Int?
    var resolved: Int?
    var self_citations: Int?
    var self_citation_rate: Double?
    var retracted: Int?
    var unmatched: Int?
}

struct SourceInfo: Decodable, Hashable, Identifiable {
    var name: String
    var label: String
    var kind: String
    var status: String
    var origin: String
    var figures: Int
    var tables: Int
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, label, kind, status, origin, figures, tables }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        label = c.value(.label, name); kind = c.value(.kind, ""); status = c.value(.status, "")
        origin = c.value(.origin, ""); figures = c.value(.figures, 0); tables = c.value(.tables, 0)
    }

    var notExamined: Bool { status.hasPrefix("not") }
}

struct CaseMember: Decodable, Hashable {
    var index: Int?
    var source: String?
    var title: String?
    var doi: String?
}

struct Report: Decodable, Hashable {
    var id: String
    var paper: PaperInfo
    var figures: [FigureInfo]
    var findings: [Finding]
    var score: Int
    var scoreAI: Int?
    var verdict: String
    var panels: Int
    var aiReview: AIReview?
    var meta: PaperMeta?
    var references: RefStats?
    var sources: [SourceInfo]
    var tables: Int
    var caseMembers: [CaseMember]?
    var created: String
    /// How far the reviewer has got, from the engine's rulings.  Nil from older engines.
    var review: ReviewSummary?
    var tags: [String]

    enum CodingKeys: String, CodingKey {
        case id, paper, figures, findings, score, score_ai, verdict, panels, ai_review, meta, references, sources, tables
        case caseMembers = "case", created, review, tags
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        paper = c.value(.paper, PaperInfo())
        figures = c.lossy(.figures); findings = c.lossy(.findings)
        score = c.value(.score, 0); scoreAI = c.optional(.score_ai); verdict = c.value(.verdict, "")
        panels = c.value(.panels, 0); aiReview = c.optional(.ai_review); meta = c.optional(.meta)
        references = c.optional(.references); sources = c.lossy(.sources); tables = c.value(.tables, 0)
        caseMembers = c.optional(.caseMembers); created = c.value(.created, "")
        review = c.optional(.review); tags = c.value(.tags, [])
    }

    var displayTitle: String {
        let t = paper.title.isEmpty ? (meta?.title ?? "") : paper.title
        return t.isEmpty ? (paper.source.isEmpty ? "Untitled scan" : paper.source) : t
    }
    var displayScore: Int { scoreAI ?? score }
    var isCase: Bool { !(caseMembers ?? []).isEmpty }
    var verdictHeadline: String { verdict.components(separatedBy: ":").first ?? "" }

    var subtitle: String {
        var parts: [String] = []
        if let j = meta?.journal, !j.isEmpty { parts.append(j) }
        if let y = meta?.issued?.prefix(4), !y.isEmpty { parts.append(String(y)) }
        parts.append(paper.doi.isEmpty ? paper.source : "doi:\(paper.doi)")
        return parts.joined(separator: " · ")
    }

    func figure(_ id: String?) -> FigureInfo? { figures.first { $0.id == id } }
    func finding(_ id: String?) -> Finding? { findings.first { $0.id == id } }
    func panel(_ id: String) -> PanelInfo? {
        for f in figures { if let p = f.panels.first(where: { $0.id == id }) { return p } }
        return nil
    }
}

struct RunSummary: Decodable, Identifiable, Hashable {
    var id: String
    var title: String
    var source: String
    var score: Int
    var scoreAI: Int?
    var created: String
    var counts: [String: Int]
    var isCase: Bool
    var tags: [String]

    enum CodingKeys: String, CodingKey { case id, title, source, score, score_ai, created, counts, isCase = "case", tags }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        source = c.value(.source, ""); title = c.value(.title, source); score = c.value(.score, 0)
        scoreAI = c.optional(.score_ai); created = c.value(.created, ""); counts = c.value(.counts, [:])
        isCase = c.value(.isCase, false); tags = c.value(.tags, [])
    }

    var displayScore: Int { scoreAI ?? score }
    var date: Date? { parseEngineDate(created) }
}

/// The engine writes local times without a zone ("2025-01-01T22:12:30"); newer engines may add one.
func parseEngineDate(_ s: String) -> Date? {
    if let d = ISO8601DateFormatter().date(from: s) { return d }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return f.date(from: String(s.prefix(19)))
}

enum JobStatus: String, Decodable {
    case running, done, error, cancelled, unknown
    init(from decoder: Decoder) throws {
        self = JobStatus(rawValue: (try? decoder.singleValueContainer().decode(String.self)) ?? "") ?? .unknown
    }
}

struct EngineJob: Decodable, Hashable {
    var id: String
    var stage: String
    var progress: Double
    var status: JobStatus
    var run: String?
    var error: String?

    enum CodingKeys: String, CodingKey { case id, stage, progress, status, run, error }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        stage = c.value(.stage, "Working…"); progress = c.value(.progress, 0); status = c.value(.status, .unknown)
        run = c.optional(.run); error = c.optional(.error)
    }

    /// The engine keeps a cancelled job "running" until the scan reaches its next step.
    var isCancelling: Bool { status == .running && stage.lowercased().hasPrefix("cancelling") }
}

struct CompareResult: Decodable, Hashable {
    var aligned: Bool
    var inliers: Int?
    var ncc_texture: Double?
    var mirrored: Bool?
    var rotation: Double?
    var scale: Double?
}

/// What GET /api/settings returns.  Keys themselves never come back, only whether one is set.
struct EngineSettings: Decodable, Equatable {
    struct Preset: Decodable, Equatable {
        var kind: String
        var label: String
        var model: String
        var base_url: String?
        var vision: Bool?
        var env: String?
        var models: [String]?
        var no_key: Bool?
    }

    var provider: String
    var model: String
    var base_url: String
    var vision: Bool
    var effort: String
    var online: Bool
    var data_keys: [String: Bool]
    var key_set: [String: Bool]
    var presets: [String: Preset]
    /// The contact address sent to scholarly databases; empty unless the user set one.
    var email: String

    enum CodingKeys: String, CodingKey {
        case provider, model, base_url, vision, effort, online, data_keys, key_set, presets, email
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = c.value(.provider, "anthropic"); model = c.value(.model, ""); base_url = c.value(.base_url, "")
        vision = c.value(.vision, true); effort = c.value(.effort, "medium"); online = c.value(.online, false)
        data_keys = c.value(.data_keys, [:]); key_set = c.value(.key_set, [:]); presets = c.value(.presets, [:])
        email = c.value(.email, "")
    }

    var preset: Preset? { presets[provider] }
    var needsKey: Bool { !(preset?.no_key ?? false) && !(key_set[provider] ?? false) }
}

/// Loose JSON for the free-form `data` field of a finding.
enum JSONValue: Decodable, Hashable {
    case number(Double), string(String), bool(Bool), array([JSONValue]), null, other

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let d = try? c.decode(Double.self) { self = .number(d) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .other }
    }

    var double: Double? { if case .number(let d) = self { return d }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }

    var display: String {
        switch self {
        case .number(let d): return d == d.rounded() && abs(d) < 1e9 ? String(Int(d)) : String(format: "%.3g", d)
        case .string(let s): return s
        case .bool(let b): return b ? "yes" : "no"
        case .array(let a): return a.map(\.display).joined(separator: ", ")
        case .null, .other: return "–"
        }
    }
}

let checkNames: [String: String] = [
    "image.duplicate": "Reused image", "image.duplicate_chart": "Identical chart", "image.clone": "Cloned region",
    "image.splice": "Splice seam", "image.flat": "Painted patch", "text.ai_boilerplate": "Chatbot text",
    "text.ai_statement": "AI statement", "text.tortured": "Tortured phrases",
    "text.prompt_injection": "Hidden AI instructions", "text.hidden_text": "Hidden text",
    "text.hidden_tags": "Invisible tags", "text.invisible_chars": "Invisible characters",
    "stats.recompute": "p-value mismatch", "stats.grim": "Impossible mean", "stats.percent": "Wrong percentage",
    "stats.repeated_se": "Repeated spread", "stats.impossible": "Impossible p-value",
    "refs.uncited": "Padded bibliography", "refs.missing": "Missing reference", "refs.duplicate": "Duplicate reference",
    "refs.retracted": "Retracted citation", "image.metadata": "Image metadata",
    "data.duplicate_block": "Copied data", "data.derived_column": "Derived column",
    "data.random_formula": "Random-number formula", "data.terminal_digits": "Odd last digits",
    "data.digit_preference": "Rounded values", "data.repeated_values": "Repeated values",
    "data.impossible": "Impossible values", "data.sort_anomaly": "Rows out of order", "data.too_similar": "Identical spread",
    "data.gene_dates": "Gene names as dates", "data.hidden": "Hidden data", "data.mixed_fonts": "Mixed fonts",
    "data.generator_digits": "Machine precision", "refs.bad_doi": "Broken DOI", "refs.unverified": "Unfindable references",
    "refs.self_citation": "Self-citation", "refs.citation_pushing": "Citation concentration",
    "refs.journal_self": "Journal self-citation", "refs.off_topic": "Off-topic references", "refs.sneaked": "Sneaked references",
    "record.retracted": "Retracted", "record.concern": "Expression of concern", "record.correction": "Correction",
    "record.fast_acceptance": "Fast acceptance", "record.duplicate_title": "Duplicate title",
    "record.author_retractions": "Authors' retractions", "report.timeline": "Timeline",
    "report.missing_data": "No data statement", "report.missing_coi": "No conflict statement",
    "report.missing_funding": "No funding statement", "report.data_on_request": "Data on request",
    "report.no_ethics": "No ethics approval", "report.no_registration": "Unregistered trial",
    "report.data_not_shown": "Data not shown", "report.sem_small_n": "SEM with tiny n",
    "report.misidentified_cells": "Misidentified cells", "report.figure_count": "Figure count",
    "report.abstract_numbers": "Abstract numbers", "report.accession_missing": "Missing accession",
    "image.unchecked": "Extra image checks not run",
    "data.offset_copy": "Copied and shifted data",
    "data.typed_over_formula": "Typed over formula",
    "data.calc_order": "Rows moved (Excel record)",
    "data.external_links": "Linked workbooks",
    "data.summary_mismatch": "Mean/SD mismatch",
    "data.precision_mix": "Mixed precision",
    "data.trailing_repeats": "Repeated decimals",
    "data.benford": "Benford first digits",
    "data.file_dates": "File dated after publication",
    "data.file_metadata": "Supplement file dates",
    "stats.grimmer": "Impossible SD",
    "stats.ci_p": "CI and p-value disagree",
    "stats.ci_estimate": "Estimate outside its interval",
    "stats.df_n": "Degrees of freedom too large",
    "stats.sd_sem": "SD or SEM mix-up",
    "stats.out_of_range": "Impossible value",
    "stats.carlisle": "Baseline too similar or too different",
    "stats.unchecked": "Baseline table not checked",
    "refs.doi_mismatch": "Cited DOI points elsewhere",
    "refs.cited_after_retraction": "Cites a work retracted before writing",
    "refs.concern": "Cites a work under concern",
    "record.special_issue": "Special-issue paper",
    "report.trial_id_format": "Trial number not valid",
    "report.trial_not_found": "Trial not found in registry",
    "report.trial_registered_late": "Trial registered late",
    "report.trial_retrospective": "Trial registered after it started",
    "report.trial_unchecked": "Trial registry not checked",
]

let categoryNames: [(id: String, name: String)] = [
    ("image", "Images"), ("data", "Data"), ("stats", "Statistics"), ("text", "Text"), ("refs", "References"),
    ("report", "Reporting"), ("record", "Publication record"),
]

enum ViewMode: String, CaseIterable, Identifiable {
    case original, levels, reveal, noise, falseColour = "false"
    var id: String { rawValue }
    var label: String {
        switch self {
        case .original: return "Original"
        case .levels: return "Levels"
        case .reveal: return "Reveal"
        case .noise: return "Noise"
        case .falseColour: return "False Colour"
        }
    }
    var help: String {
        switch self {
        case .original: return "The figure as published."
        case .levels: return "Stretched contrast: brings out faint edges and erased backgrounds."
        case .reveal: return "Local contrast boost: shows pasted patches and smoothing."
        case .noise: return "Camera noise only: cloned or painted regions look flat or repeat."
        case .falseColour: return "Brightness as colour: small steps between similar greys become visible."
        }
    }
}

enum FindingSort: String, CaseIterable, Identifiable {
    case severity, confidence, figure
    var id: String { rawValue }
    var label: String {
        switch self {
        case .severity: return "Severity"
        case .confidence: return "Confidence"
        case .figure: return "Figure Order"
        }
    }
}
