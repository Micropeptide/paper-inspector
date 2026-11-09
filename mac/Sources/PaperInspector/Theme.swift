import AppKit
import SwiftUI

extension NSColor {
    convenience init(hex: UInt32) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

extension Color {
    /// A colour that follows light/dark appearance and Increase Contrast.
    static func dynamic(light: NSColor, dark: NSColor, lightHC: NSColor? = nil, darkHC: NSColor? = nil) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            switch appearance.bestMatch(from: [.aqua, .darkAqua, .accessibilityHighContrastAqua,
                                               .accessibilityHighContrastDarkAqua, .vibrantDark]) {
            case .some(.accessibilityHighContrastDarkAqua): return darkHC ?? dark
            case .some(.accessibilityHighContrastAqua): return lightHC ?? light
            case .some(.darkAqua), .some(.vibrantDark): return dark
            default: return light
            }
        })
    }
}

enum Theme {
    static let critical = Color.dynamic(light: NSColor(hex: 0xB3261E), dark: NSColor(hex: 0xFF6B5E),
                                        lightHC: NSColor(hex: 0x8C1D18), darkHC: NSColor(hex: 0xFF9A90))
    static let high = Color.dynamic(light: NSColor(hex: 0xC2410C), dark: NSColor(hex: 0xFF8A4C),
                                    lightHC: NSColor(hex: 0x9A3412), darkHC: NSColor(hex: 0xFFB088))
    static let medium = Color.dynamic(light: NSColor(hex: 0xA16207), dark: NSColor(hex: 0xF5B544),
                                      lightHC: NSColor(hex: 0x7C4A03), darkHC: NSColor(hex: 0xFFD27A))
    static let low = Color(nsColor: .systemGray)
    static let info = Color(nsColor: .secondaryLabelColor)
    /// Text drawn on a severity-coloured fill.
    static let onBadge = Color.dynamic(light: .white, dark: .black)
    static let markA = Color(nsColor: NSColor(hex: 0x00B8D9))
    static let markB = Color(nsColor: NSColor(hex: 0xE0447C))
    static let calm = Color.dynamic(light: NSColor(hex: 0x15803D), dark: NSColor(hex: 0x4ADE80))
    /// The figure stage stays dark in both appearances so images are judged against a neutral surround.
    static let stage = Color.dynamic(light: NSColor(hex: 0x2A2B2E), dark: NSColor(hex: 0x1E1F22))
}

extension Severity {
    var color: Color {
        switch self {
        case .critical: return Theme.critical
        case .high: return Theme.high
        case .medium: return Theme.medium
        case .low, .unknown: return Theme.low
        case .info: return Theme.info
        }
    }

    /// Meaning never rides on colour alone: each level has its own shape.
    var symbol: String {
        switch self {
        case .critical: return "exclamationmark.octagon.fill"
        case .high: return "exclamationmark.triangle.fill"
        case .medium: return "exclamationmark.circle.fill"
        case .low, .unknown: return "circle.fill"
        case .info: return "info.circle"
        }
    }
}

enum ScoreBand {
    static func of(_ score: Int) -> (name: String, color: Color) {
        switch score {
        case 70...: return ("high risk", Theme.critical)
        case 40..<70: return ("elevated risk", Theme.high)
        case 15..<40: return ("some concerns", Theme.medium)
        default: return ("few concerns", Theme.low)
        }
    }
}

struct ScoreBadge: View {
    let score: Int
    var aiAdjusted = false

    var body: some View {
        let band = ScoreBand.of(score)
        HStack(spacing: 2) {
            Text("\(score)").monospacedDigit()
            if aiAdjusted { Image(systemName: "sparkle").imageScale(.small) }
        }
        .font(.callout.weight(.bold))
        .foregroundStyle(Theme.onBadge)
        .padding(.horizontal, 7)
        .frame(minWidth: 36, minHeight: 22)
        .background(band.color, in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Integrity score \(score) of 100, \(band.name)\(aiAdjusted ? ", adjusted by the AI investigator" : "")")
        .help("Integrity score \(score) of 100: \(band.name)")
    }
}

struct SeverityLabel: View {
    let severity: Severity
    var text: String? = nil

    var body: some View {
        Label {
            Text(text ?? severity.label)
        } icon: {
            Image(systemName: severity.symbol).foregroundStyle(severity.color)
        }
    }
}

/// An inline error with recovery buttons, used instead of loose red text.
struct ErrorBanner: View {
    let error: AppError
    var retry: (() -> Void)? = nil
    var dismiss: (() -> Void)? = nil
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.high)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(error.title).font(.headline)
                Text(error.message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                HStack {
                    if let retry { Button("Retry", action: retry) }
                    if error.suggestsLog { Button("Show Engine Log") { openWindow(id: "log") } }
                    if error.suggestsSettings {
                        SettingsLink { Text("Open Settings…") }
                    }
                    if let dismiss { Button("Dismiss", action: dismiss) }
                }
                .controlSize(.small)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .contain)
    }
}
