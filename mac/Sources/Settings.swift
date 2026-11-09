import AppKit
import Carbon.HIToolbox
import ServiceManagement
import SwiftUI

/// Voice, default speed and the Show Reader shortcut, stored in UserDefaults.
/// Each reader window starts at the default speed and can change it for that text only.
@MainActor
final class Settings: ObservableObject {
    struct Voice: Decodable, Hashable {
        let id: String
        let name: String
    }

    static let speedRange = 0.5...2.0

    @Published var voice: String {
        didSet {
            UserDefaults.standard.set(voice, forKey: "voice")
            onVoiceChange?(voice)
        }
    }
    @Published var defaultSpeed: Double {
        didSet { UserDefaults.standard.set(defaultSpeed, forKey: "defaultSpeed") }
    }
    /// Loaded from the server so the list matches what it can synthesize.
    @Published var voices: [Voice] = []
    /// The login item lives in the system, not UserDefaults; this mirrors it.
    @Published private(set) var startAtLogin = SMAppService.mainApp.status
    /// Brings up the reader from any app; nil when the user removed it.
    @Published var showReaderShortcut: Shortcut? {
        didSet {
            let data = showReaderShortcut.flatMap { try? JSONEncoder().encode($0) } ?? Data()
            UserDefaults.standard.set(data, forKey: "showReaderShortcut")
            applyShortcut()
        }
    }
    /// Set when another app already holds the shortcut.
    @Published private(set) var shortcutConflict = false
    /// Set when the shortcut is the Read in Aloud service's; it is then left unregistered.
    @Published private(set) var shortcutClashesWithService = false
    /// Read in Aloud's shortcut, owned by System Settings; see refreshServiceShortcut().
    private var serviceShortcut = ReadSelectionService.shortcut()

    let showReaderHotKey = GlobalHotKey()
    var onVoiceChange: ((String) -> Void)?

    init() {
        let defaults = UserDefaults.standard
        voice = defaults.string(forKey: "voice") ?? "bf_emma"
        let speed = defaults.double(forKey: "defaultSpeed")
        defaultSpeed = Settings.speedRange.contains(speed) ? speed : 1.3
        // Never set means the default; empty data means the user removed it.
        if let data = defaults.data(forKey: "showReaderShortcut") {
            showReaderShortcut = try? JSONDecoder().decode(Shortcut.self, from: data)
        } else {
            showReaderShortcut = .showReaderDefault
        }

        // The service's shortcut is changed in System Settings, and leaving it
        // activates another app, so this catches the change in either direction.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshServiceShortcut() }
        }
    }

    func applyShortcut() {
        // While recording, the hotkey stays off so pressing it reaches the recorder.
        guard !recordingShortcut else { return }
        // Registering it would stop the service from reading the selection, so the
        // service keeps the key until the user picks another one here.
        shortcutClashesWithService = clashesWithService(showReaderShortcut)
        shortcutConflict = !showReaderHotKey.register(shortcutClashesWithService ? nil : showReaderShortcut)
    }

    func refreshServiceShortcut() {
        let current = ReadSelectionService.shortcut()
        guard current != serviceShortcut else { return }
        serviceShortcut = current
        applyShortcut()
    }

    private func clashesWithService(_ shortcut: Shortcut?) -> Bool {
        guard let shortcut, let serviceShortcut else { return false }
        return shortcut.sameKeys(as: serviceShortcut)
    }

    @Published private(set) var recordingShortcut = false
    private var recordMonitor: Any?

    func startRecording() {
        guard recordMonitor == nil else { return }
        // Unregister so pressing the current shortcut reaches the recorder instead of firing.
        showReaderHotKey.register(nil)
        recordingShortcut = true
        recordMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            MainActor.assumeIsolated {
                guard let self else { return }
                if event.keyCode == UInt16(kVK_Escape) {
                    self.stopRecording()
                } else if let shortcut = Shortcut(event: event) {
                    if self.clashesWithService(shortcut) {
                        NSSound.beep()
                        self.shortcutClashesWithService = true
                    } else {
                        self.stopRecording()
                        self.showReaderShortcut = shortcut
                    }
                } else {
                    NSSound.beep()
                }
            }
            return nil
        }
    }

    func stopRecording() {
        guard let recordMonitor else { return }
        NSEvent.removeMonitor(recordMonitor)
        self.recordMonitor = nil
        recordingShortcut = false
        applyShortcut()
    }

    func setStartAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSAlert(error: error).runModal()
        }
        refreshStartAtLogin()
    }

    func refreshStartAtLogin() {
        startAtLogin = SMAppService.mainApp.status
    }

    func loadVoices(from server: ServerEndpoint) async {
        guard let (data, _) = try? await URLSession.shared.data(for: server.request("api/voices")),
              let list = try? JSONDecoder().decode([Voice].self, from: data)
        else { return }
        voices = list
    }
}

struct SettingsView: View {
    @ObservedObject var settings: Settings

    var body: some View {
        Form {
            Picker("Voice", selection: $settings.voice) {
                if settings.voices.isEmpty {
                    Text(settings.voice).tag(settings.voice)
                }
                ForEach(settings.voices, id: \.id) { voice in
                    Text(voice.name).tag(voice.id)
                }
            }
            LabeledContent("Default speed") {
                HStack {
                    Slider(value: $settings.defaultSpeed, in: Settings.speedRange, step: 0.1)
                    Text(String(format: "%.1f×", settings.defaultSpeed))
                        .monospacedDigit()
                        .frame(width: 36, alignment: .trailing)
                }
            }
            Section {
                LabeledContent("Show reader") {
                    ShortcutRecorder(settings: settings)
                }
            } footer: {
                if settings.shortcutClashesWithService {
                    Text("This shortcut runs Read in Aloud on selected text. Choose a different one.")
                        .font(.footnote)
                        .foregroundStyle(.red)
                } else if settings.shortcutConflict {
                    Text("Another app is using this shortcut. Choose a different one.")
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
            Section {
                // The Read in Aloud service's shortcut is owned by macOS, so this explains
                // where to set it rather than recording it here.
                VStack(alignment: .leading, spacing: 8) {
                    Text("Read selected text")
                    Text(
                        "Right-click selected text and choose Read in Aloud (under Services in some apps). "
                            + "To use a key instead, open Keyboard Shortcuts › Services › Text › Read in Aloud "
                            + "and assign one that includes ⌘ and differs from the shortcut above."
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    Button("Open Keyboard Settings…") {
                        NSWorkspace.shared.open(
                            URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension")!
                        )
                    }
                }
                .padding(.vertical, 2)
            }
            Section {
                Toggle("Start at login", isOn: Binding(
                    get: { settings.startAtLogin != .notRegistered && settings.startAtLogin != .notFound },
                    set: { settings.setStartAtLogin($0) }
                ))
            } footer: {
                if settings.startAtLogin == .requiresApproval {
                    Text("Allow Aloud in System Settings › General › Login Items.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400)
        .fixedSize()
    }
}

/// Click, then press the new combination. Esc cancels.
struct ShortcutRecorder: View {
    @ObservedObject var settings: Settings

    // One fixed-size field in every state, with the clear button inside it,
    // so nothing shifts and its edge lines up with the controls above.
    var body: some View {
        let recording = settings.recordingShortcut
        let shortcut = settings.showReaderShortcut
        Button {
            recording ? settings.stopRecording() : settings.startRecording()
        } label: {
            Text(recording ? "Type shortcut…" : shortcut?.display ?? "Click to record")
                .foregroundStyle(recording || shortcut == nil ? .secondary : .primary)
                .frame(width: 130, height: 22)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(
            RoundedRectangle(cornerRadius: 6).strokeBorder(
                recording ? Color.accentColor : Color(nsColor: .separatorColor),
                lineWidth: recording ? 2 : 1
            )
        )
        .overlay(alignment: .trailing) {
            if shortcut != nil, !recording {
                Button {
                    settings.showReaderShortcut = nil
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                }
                .buttonStyle(.borderless)
                .help("Remove shortcut")
                .padding(.trailing, 5)
            }
        }
        .onDisappear { settings.stopRecording() }
    }
}

@MainActor
final class SettingsWindow {
    let window: NSWindow

    init(settings: Settings) {
        let hosting = NSHostingController(rootView: SettingsView(settings: settings))
        window = NSWindow(contentViewController: hosting)
        window.title = "Aloud Settings"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        // SwiftUI sizes the window lazily; centering first would center a 1pt
        // window that then grows up and to the right.
        window.setContentSize(hosting.view.fittingSize)
        window.center()
    }

    func show() {
        window.bringToFront()
    }
}
