import AppKit
import SwiftUI

/// The blink comparator: two aligned copies in one place, alternating.  Identical pictures stand still.
/// Keys work only while the stage has focus (click it): Space flips, P plays or pauses, ← / → show A or B,
/// [ and ] change the speed, 1–4 switch modes.
struct BlinkView: View {
    let a: URL
    let b: URL
    let diff: URL?
    let labelA: String
    let labelB: String
    var tall = false

    enum Mode: String, CaseIterable { case blink = "Blink", swipe = "Swipe", diff = "Difference", side = "Side by Side" }

    @State private var mode: Mode = .blink
    @State private var imgA: NSImage?
    @State private var imgB: NSImage?
    @State private var imgD: NSImage?
    @State private var failed = false
    @State private var showB = false
    @State private var split = 0.5
    @AppStorage("blinkAutoplay") private var autoplay = true
    @AppStorage("blinkInterval") private var interval = 0.65
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var stageFocused: Bool

    private var playing: Bool { autoplay && !reduceMotion }
    private var modes: [Mode] { Mode.allCases.filter { $0 != .diff || diff != nil } }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("Comparison mode", selection: $mode) {
                ForEach(modes, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            stage
                .frame(maxWidth: .infinity, minHeight: 200, maxHeight: tall ? .infinity : 360)
                .background(Theme.stage)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(stageFocused ? Color.accentColor : .clear, lineWidth: 2)
                }
                .focusable()
                .focused($stageFocused)
                .focusEffectDisabled()
                .onTapGesture { stageFocused = true }
                .onKeyPress(keys: [.space, .leftArrow, .rightArrow]) { press in
                    switch press.key {
                    case .space: autoplay = false; showB.toggle()
                    case .leftArrow: autoplay = false; showB = false; split = max(0, split - 0.05)
                    default: autoplay = false; showB = true; split = min(1, split + 0.05)
                    }
                    return .handled
                }
                .onKeyPress(characters: .init(charactersIn: "pP[]1234")) { press in
                    switch press.characters {
                    case "p", "P": autoplay.toggle()
                    case "[": interval = min(1.5, interval + 0.1)
                    case "]": interval = max(0.15, interval - 0.1)
                    default:
                        if let n = Int(press.characters), n >= 1, n <= modes.count { mode = modes[n - 1] }
                    }
                    return .handled
                }
                .environment(\.colorScheme, .dark)

            controls
        }
        .task(id: [a, b, diff].compactMap { $0?.absoluteString }.joined(separator: "|")) { await load() }
        .task(id: "\(playing)-\(interval)-\(mode)") {
            guard playing, mode == .blink else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(max(0.15, interval) * 1_000_000_000))
                if Task.isCancelled { break }
                showB.toggle()
            }
        }
    }

    @ViewBuilder private var controls: some View {
        switch mode {
        case .blink:
            HStack {
                Button(playing ? "Pause" : "Play") { autoplay.toggle() }
                    .disabled(reduceMotion)
                    .help(reduceMotion ? "Automatic blinking is off because Reduce Motion is on. Use Flip." : "P")
                Button("Flip") { autoplay = false; showB.toggle() }
                    .help("Space, with the comparator focused")
                Slider(value: $interval, in: 0.15...1.5) { Text("Interval") }
                    .frame(maxWidth: 160)
                    .help("Seconds between flips")
            }
            .controlSize(.small)
        case .swipe:
            Text("Drag across the picture, or use ← and →, to wipe between the two copies.")
                .font(.caption).foregroundStyle(.secondary)
        case .diff:
            Text("Black means the two copies agree pixel for pixel.").font(.caption).foregroundStyle(.secondary)
        case .side:
            EmptyView()
        }
    }

    @ViewBuilder private var stage: some View {
        switch mode {
        case .side:
            HStack(spacing: 8) {
                labelled(imgA, labelA)
                labelled(imgB, labelB)
            }
            .padding(8)
        case .diff:
            image(imgD, label: "Difference between \(labelA) and \(labelB)").padding(8)
        case .blink:
            ZStack(alignment: .topLeading) {
                image(imgA, label: labelA).opacity(showB ? 0 : 1)
                image(imgB, label: labelB).opacity(showB ? 1 : 0)
                tag(showB ? "B  \(labelB)" : "A  \(labelA)", color: showB ? Theme.markB : Theme.markA)
            }
            .padding(8)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Blink comparator showing \(showB ? labelB : labelA)")
            .accessibilityAction(named: "Flip") { autoplay = false; showB.toggle() }
        case .swipe:
            GeometryReader { geo in
                ZStack(alignment: .topLeading) {
                    image(imgA, label: labelA)
                    image(imgB, label: labelB).mask(alignment: .trailing) {
                        Rectangle().frame(width: geo.size.width * (1 - split))
                    }
                    Rectangle().fill(.white).frame(width: 2).offset(x: geo.size.width * split - 1)
                    tag("A  \(labelA)  |  B  \(labelB)", color: Theme.markA)
                }
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged {
                    guard geo.size.width > 0 else { return }
                    split = min(1, max(0, $0.location.x / geo.size.width))
                })
            }
            .padding(8)
            .accessibilityRepresentation {
                Slider(value: $split, in: 0...1) { Text("Wipe between \(labelA) and \(labelB)") }
            }
        }
    }

    private func image(_ i: NSImage?, label: String) -> some View {
        Group {
            if let i {
                Image(nsImage: i).resizable().interpolation(.none).aspectRatio(contentMode: .fit)
                    .accessibilityLabel(label)
            } else if failed {
                Text("Couldn’t load this comparison").font(.caption).foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func labelled(_ i: NSImage?, _ label: String) -> some View {
        VStack { image(i, label: label); Text(label).font(.caption).foregroundStyle(.secondary) }
    }

    private func tag(_ text: String, color: Color) -> some View {
        Text(text).font(.caption.weight(.semibold)).padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.9), in: RoundedRectangle(cornerRadius: 4)).foregroundStyle(.black).padding(6)
    }

    private func load() async {
        failed = false
        async let ia = ImageStore.shared.load(a)
        async let ib = ImageStore.shared.load(b)
        let (la, lb) = await (ia, ib)
        imgA = la
        imgB = lb
        if let diff { imgD = await ImageStore.shared.load(diff) } else { imgD = nil }
        failed = la == nil || lb == nil
    }
}

/// The comparator in its own window, sized for close inspection.
struct BlinkWindow: View {
    let spec: BlinkSpec

    var body: some View {
        let client = EngineController.shared.client
        BlinkView(a: client.frameURL(spec.run, "a", spec.a, spec.b), b: client.frameURL(spec.run, "b", spec.a, spec.b),
                  diff: client.frameURL(spec.run, "diff", spec.a, spec.b), labelA: spec.a, labelB: "\(spec.b), aligned",
                  tall: true)
            .padding(16)
            .navigationTitle("\(spec.a) and \(spec.b)")
            .navigationSubtitle(spec.title)
    }
}
