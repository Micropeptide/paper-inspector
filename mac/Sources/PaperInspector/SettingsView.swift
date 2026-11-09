import AppKit
import SwiftUI

/// Settings > AI Investigator, Scholarly Data, Engine and General.  Model and data keys go to the engine's own
/// settings file (mode 600) through its API; the app itself never stores a key.  Changes apply immediately.
struct SettingsView: View {
    @Environment(EngineController.self) private var engine
    @State private var settings: EngineSettings?
    @State private var error: AppError?

    var body: some View {
        TabView {
            Group { content { AISettingsTab(settings: $0, save: save) } }
                .tabItem { Label("AI Investigator", systemImage: "sparkles") }
            Group { content { DataSettingsTab(settings: $0, save: save) } }
                .tabItem { Label("Scholarly Data", systemImage: "books.vertical") }
            EngineSettingsTab()
                .tabItem { Label("Engine", systemImage: "cpu") }
            GeneralSettingsTab()
                .tabItem { Label("General", systemImage: "gearshape") }
        }
        .frame(width: 540)
        .frame(minHeight: 420)
        .task(id: engine.isReady) { await load() }
    }

    @ViewBuilder private func content<V: View>(@ViewBuilder _ tab: @escaping (EngineSettings) -> V) -> some View {
        if let settings {
            VStack(spacing: 0) {
                if let error { ErrorBanner(error: error, dismiss: { self.error = nil }).padding([.horizontal, .top]) }
                tab(settings)
            }
        } else if let error {
            ErrorBanner(error: error, retry: { Task { await load() } }).padding()
                .frame(maxHeight: .infinity, alignment: .top)
        } else if engine.isReady {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ContentUnavailableView("The Engine Isn’t Running", systemImage: "cpu",
                                   description: Text("These settings live in the analysis engine. Start it from the Engine tab."))
        }
    }

    private func load() async {
        guard engine.isReady else { return }
        do {
            settings = try await engine.client.settings()
            error = nil
        } catch {
            self.error = AppError.from(error)
        }
    }

    private func save(_ patch: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: patch) else { return }
        Task {
            do {
                settings = try await engine.client.saveSettings(json: data)
                error = nil
            } catch {
                self.error = AppError(title: "Couldn’t save the setting", message: AppError.from(error).message)
            }
        }
    }
}

private struct AISettingsTab: View {
    let settings: EngineSettings
    let save: ([String: Any]) -> Void
    @State private var model = ""
    @State private var endpoint = ""
    @State private var key = ""

    private var presetIDs: [String] {
        settings.presets.keys.sorted { (settings.presets[$0]?.label ?? $0) < (settings.presets[$1]?.label ?? $1) }
    }

    var body: some View {
        Form {
            Section {
                Picker("Provider", selection: Binding(get: { settings.provider },
                                                      set: { save(["provider": $0, "model": "", "base_url": ""]) })) {
                    ForEach(presetIDs, id: \.self) { id in
                        let p = settings.presets[id]
                        let needsKey = !(p?.no_key ?? false) && !(settings.key_set[id] ?? false)
                        Text((p?.label ?? id) + (needsKey ? " (needs key)" : "")).tag(id)
                    }
                }
                HStack {
                    TextField("Model", text: $model, prompt: Text(settings.preset?.model ?? "model name"))
                        .onSubmit { save(["model": model.trimmingCharacters(in: .whitespaces)]) }
                    if let models = settings.preset?.models, !models.isEmpty {
                        Menu {
                            ForEach(models, id: \.self) { m in Button(m) { model = m; save(["model": m]) } }
                        } label: { Label("Choose a Model", systemImage: "chevron.up.chevron.down") }
                            .labelStyle(.iconOnly)
                            .menuStyle(.borderlessButton)
                            .fixedSize()
                    }
                }
                if settings.preset?.kind == "openai" {
                    TextField("Endpoint", text: $endpoint, prompt: Text(settings.preset?.base_url ?? "https://…/v1"))
                        .onSubmit { save(["base_url": endpoint.trimmingCharacters(in: .whitespaces)]) }
                }
                Toggle("Model can see images", isOn: Binding(get: { settings.vision }, set: { save(["vision": $0]) }))
                if settings.preset?.kind == "anthropic" {
                    Picker("Effort", selection: Binding(get: { settings.effort }, set: { save(["effort": $0]) })) {
                        Text("Low").tag("low"); Text("Medium").tag("medium"); Text("High").tag("high")
                    }
                }
            } footer: {
                Text("Press Return to apply a typed model or endpoint.").font(.caption).foregroundStyle(.secondary)
            }

            if !(settings.preset?.no_key ?? false) {
                Section("API Key") {
                    keyStatus
                    HStack {
                        SecureField("API key", text: $key, prompt: Text("Paste a new key"))
                            .onSubmit(saveKey)
                        Button("Save", action: saveKey).disabled(key.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    if settings.key_set[settings.provider] == true {
                        Button("Remove Saved Key", role: .destructive) { save(["keys": [settings.provider: ""]]) }
                    }
                }
            }

            Section {
                Text("The AI investigator runs only when you ask. It sends the paper’s figures, their captions and the findings to the provider above. A local model (Ollama, LM Studio) keeps everything on this Mac.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear(perform: sync)
        .onChange(of: settings) { _, _ in sync() }
    }

    @ViewBuilder private var keyStatus: some View {
        let env = settings.preset?.env ?? ""
        if settings.key_set[settings.provider] == true {
            Label(env.isEmpty ? "A key is saved." : "A key is saved, or set in $\(env).", systemImage: "checkmark.circle.fill")
                .foregroundStyle(Theme.calm)
        } else {
            Label(env.isEmpty ? "No key saved." : "No key saved. You can also set $\(env) in the engine’s environment.",
                  systemImage: "key")
                .foregroundStyle(.secondary)
        }
    }

    private func saveKey() {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty else { return }
        save(["keys": [settings.provider: k]])
        key = ""
    }

    private func sync() {
        model = settings.model == settings.preset?.model ? "" : settings.model
        endpoint = settings.base_url == (settings.preset?.base_url ?? "") ? "" : settings.base_url
    }
}

private struct DataSettingsTab: View {
    let settings: EngineSettings
    let save: ([String: Any]) -> Void
    @State private var key = ""
    @State private var email = ""
    @State private var emailEdited = false

    var body: some View {
        Form {
            Section {
                HStack {
                    TextField("Contact email", text: $email, prompt: Text("Optional"))
                        .textContentType(.emailAddress)
                        .onSubmit(saveEmail)
                        .onChange(of: email) { _, v in emailEdited = v != settings.email }
                    Button("Save", action: saveEmail).disabled(!emailEdited)
                }
                if !settings.email.isEmpty {
                    Button("Remove Email", role: .destructive) { email = ""; save(["email": ""]) }
                }
            } header: {
                Text("Contact Email")
            } footer: {
                Text("Crossref and OpenAlex answer faster when a request names a contact address. Nothing is sent unless you enter one here. An address that doesn’t look valid is ignored.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Toggle("Check cited DOIs with Crossref", isOn: Binding(get: { settings.online }, set: { save(["online": $0]) }))
            } footer: {
                Text("Looks up every cited DOI to find broken or retracted references. Sends the cited DOIs, not the paper, to Crossref.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                if settings.data_keys["openalex"] == true {
                    Label("An OpenAlex key is saved.", systemImage: "checkmark.circle.fill").foregroundStyle(Theme.calm)
                    Button("Remove OpenAlex Key", role: .destructive) { save(["keys": ["openalex": ""]]) }
                }
                HStack {
                    SecureField("OpenAlex key", text: $key, prompt: Text("Optional"))
                        .onSubmit(saveKey)
                    Button("Save", action: saveKey).disabled(key.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            } header: {
                Text("OpenAlex")
            } footer: {
                VStack(alignment: .leading, spacing: 4) {
                    Text("OpenAlex supplies reference, citation and retraction data. It works without a key; a free key raises the rate limit.")
                    if let u = URL(string: "https://openalex.org/settings/api") { Link("Get an OpenAlex key", destination: u) }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { email = settings.email }
        .onChange(of: settings.email) { _, v in email = v; emailEdited = false }
    }

    private func saveKey() {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty else { return }
        save(["keys": ["openalex": k]])
        key = ""
    }

    private func saveEmail() {
        save(["email": email.trimmingCharacters(in: .whitespacesAndNewlines)])
        emailEdited = false
    }
}

private struct EngineSettingsTab: View {
    @Environment(EngineController.self) private var engine
    @Environment(\.openWindow) private var openWindow
    @AppStorage("enginePath") private var enginePath = ""
    @AppStorage("enginePort") private var enginePort = 0

    var body: some View {
        Form {
            Section("Status") {
                LabeledContent("State", value: engine.statusText)
                if let p = engine.port { LabeledContent("Address", value: "127.0.0.1:\(p)") }
                if let pid = engine.pid { LabeledContent("Process", value: "\(pid)") }
                LabeledContent("Command") {
                    Text(engine.executable ?? "Not found").lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                }
            }
            Section {
                LabeledContent("Engine command") {
                    HStack {
                        Text(enginePath.isEmpty ? "Automatic" : enginePath).lineLimit(1).truncationMode(.middle)
                            .foregroundStyle(enginePath.isEmpty ? .secondary : .primary)
                        Button("Choose…", action: choose)
                        if !enginePath.isEmpty { Button("Use Automatic") { enginePath = "" } }
                    }
                }
                Picker("Port", selection: $enginePort) {
                    Text("Automatic").tag(0)
                    if enginePort != 0 && enginePort != 8765 { Text("\(enginePort)").tag(enginePort) }
                    Text("8765").tag(8765)
                }
            } footer: {
                Text("Automatic finds the `paperinspector` command next to this app or on your PATH and runs it on a free local port. Changes apply when the engine restarts.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                HStack {
                    Button("Restart Engine") { engine.restart() }
                    Button("Show Log") { openWindow(id: "log") }
                    Button("Show Data Folder") {
                        NSWorkspace.shared.activateFileViewerSelecting([EngineController.dataFolder])
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.treatsFilePackagesAsDirectories = true
        panel.message = "Choose the paperinspector command (usually in a .venv/bin folder)"
        panel.directoryURL = engine.executable.map { URL(fileURLWithPath: $0).deletingLastPathComponent() }
        guard panel.runModal() == .OK, let u = panel.url else { return }
        guard FileManager.default.isExecutableFile(atPath: u.path) else {
            ScanQueue.shared.present(AppError(title: "That file can’t be run", message: "Choose the `paperinspector` command itself."))
            return
        }
        enginePath = u.path
        engine.restart()
    }
}

private struct GeneralSettingsTab: View {
    @AppStorage("notifyOnCompletion") private var notify = true
    @AppStorage("keepRunningWithoutWindows") private var keepRunning = false
    @AppStorage("multiPDFDefault") private var multiPDF = "ask"
    @AppStorage("blinkAutoplay") private var blinkAutoplay = true
    @AppStorage("blinkInterval") private var blinkInterval = 0.65

    var body: some View {
        Form {
            Section {
                Toggle("Notify me when a scan finishes in the background", isOn: $notify)
                Toggle("Keep running after the last window closes", isOn: $keepRunning)
                Picker("When several PDFs are added", selection: $multiPDF) {
                    Text("Ask each time").tag("ask")
                    Text("Treat as one paper with supplements").tag("single")
                    Text("Compare them as separate papers").tag("compare")
                }
            }
            Section("Blink Comparator") {
                Toggle("Blink automatically", isOn: $blinkAutoplay)
                LabeledContent("Interval") {
                    Slider(value: $blinkInterval, in: 0.15...1.5) { Text("Interval") }
                        .labelsHidden()
                    Text(String(format: "%.2f s", blinkInterval)).monospacedDigit().foregroundStyle(.secondary)
                }
            }
            Section {
                Button("Clear Cached Images and Files") {
                    ImageStore.shared.purgeAll()
                    FileFetcher.purgeAll()
                }
            } footer: {
                Text("Removes the app’s copies of figures and supplementary files. Scans themselves are kept.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}
