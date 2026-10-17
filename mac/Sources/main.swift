import AppKit
import ServiceManagement

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let server = Server()
    private lazy var reader = ReaderWindow()
    private var statusItem: NSStatusItem!
    private var loginItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The "TTS" entry in the right-click menu is declared under NSServices in Info.plist.
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
        setUpStatusItem()
        // Start the server (and its model warm-up) now, so the first selection reads quickly.
        Task { try? await server.ensureRunning() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        reader.show()
        return false
    }

    // MARK: Services

    @objc func readSelection(
        _ pboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>
    ) {
        guard let text = pboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty
        else {
            error.pointee = "No text was selected." as NSString
            return
        }
        read(text)
    }

    private func read(_ text: String) {
        reader.read(text)
        Task {
            do {
                try await server.ensureRunning()
                reader.serverReady()
            } catch {
                reader.showMessage(error.localizedDescription)
            }
        }
    }

    // MARK: Menu bar

    private func setUpStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(
            systemSymbolName: "speaker.wave.2", accessibilityDescription: "Kokoro Reader"
        )

        let menu = NSMenu()
        menu.addItem(withTitle: "Read Clipboard", action: #selector(readClipboard), keyEquivalent: "")
        menu.addItem(withTitle: "Show Reader", action: #selector(showReader), keyEquivalent: "")
        menu.addItem(.separator())
        loginItem = menu.addItem(withTitle: "Start at Login", action: #selector(toggleLogin), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Kokoro Reader", action: #selector(NSApplication.terminate), keyEquivalent: "q")
        statusItem.menu = menu
        updateLoginItem()
    }

    @objc private func readClipboard() {
        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else { return }
        read(text)
    }

    @objc private func showReader() {
        reader.show()
        Task {
            if (try? await server.ensureRunning()) != nil { reader.serverReady() }
        }
    }

    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
        } catch {
            let alert = NSAlert(error: error)
            alert.runModal()
        }
        updateLoginItem()
    }

    private func updateLoginItem() {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
