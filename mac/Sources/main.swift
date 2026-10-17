import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let server = Server()
    private let settings = Settings()
    private lazy var reader = ReaderWindow(settings: settings)
    private lazy var settingsWindow = SettingsWindow(settings: settings)
    private var statusItem: NSStatusItem!
    private var statusMenu: NSMenu!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The "Read in Aloud" entry in the right-click menu is declared under NSServices in Info.plist.
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
        NSApp.mainMenu = makeMainMenu()
        setUpStatusItem()
        settings.onVoiceChange = { [weak self] voice in self?.reader.setVoice(voice) }
        settings.showReaderHotKey.action = { [weak self] in self?.showReader() }
        settings.applyShortcut()

        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: nil, queue: .main
        ) { [weak self] note in
            let closing = note.object as? NSWindow
            MainActor.assumeIsolated { self?.updateActivationPolicy(closing: closing) }
        }

        // Start the server (and its model warm-up) now, so the first selection reads quickly.
        Task {
            try? await server.ensureRunning()
            await settings.loadVoices()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if Date() < AppActivation.ownReopenUntil { return false }
        showReader()
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

    // Files dropped on the Dock icon, or opened with Open With.
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.first else { return }
        var encoding = String.Encoding.utf8
        guard let text = (try? String(contentsOf: url, encoding: .utf8))
                ?? (try? String(contentsOf: url, usedEncoding: &encoding))
        else {
            NSAlert(error: CocoaError(.fileReadInapplicableStringEncoding, userInfo: [NSURLErrorKey: url]))
                .runModal()
            return
        }
        read(text, markdown: ["md", "markdown"].contains(url.pathExtension.lowercased()))
    }

    private func read(_ text: String, markdown: Bool = false) {
        becomeRegularApp()
        reader.read(text, markdown: markdown)
        Task {
            do {
                try await server.ensureRunning()
                reader.serverReady()
            } catch {
                reader.showMessage(error.localizedDescription)
            }
        }
    }

    // MARK: Actions

    @objc func readClipboard() {
        guard let text = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
        else {
            NSSound.beep()
            return
        }
        read(text)
    }

    @objc func clearReader() {
        reader.clear()
    }

    @objc func showReader() {
        becomeRegularApp()
        reader.show()
        Task {
            if (try? await server.ensureRunning()) != nil { reader.serverReady() }
        }
    }

    @objc func showSettings() {
        becomeRegularApp()
        // It may have been changed in System Settings since.
        settings.refreshStartAtLogin()
        settingsWindow.show()
        Task {
            if (try? await server.ensureRunning()) != nil { await settings.loadVoices() }
        }
    }

    // MARK: Dock and app switcher

    // A menu bar app is hidden from ⌘-Tab and the Dock. While one of its
    // windows is open it becomes a regular app, so it can be switched to.
    private func becomeRegularApp() {
        if NSApp.activationPolicy() != .regular { NSApp.setActivationPolicy(.regular) }
    }

    private func updateActivationPolicy(closing: NSWindow?) {
        let open = [reader.window, settingsWindow.window].contains { $0 !== closing && $0.isVisible }
        if !open { NSApp.setActivationPolicy(.accessory) }
    }

    // MARK: Menus

    private func makeMainMenu() -> NSMenu {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Aloud", action: #selector(NSApplication.hide), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Aloud", action: #selector(NSApplication.terminate), keyEquivalent: "q")
        main.addItem(submenu: appMenu, title: "Aloud")

        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        // ⌘V reads the clipboard: the reader has no text field to paste into.
        edit.addItem(withTitle: "Read Clipboard", action: #selector(readClipboard), keyEquivalent: "v").target = self
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Clear", action: #selector(clearReader), keyEquivalent: "k").target = self
        main.addItem(submenu: edit, title: "Edit")

        let window = NSMenu(title: "Window")
        window.addItem(withTitle: "Close", action: #selector(NSWindow.performClose), keyEquivalent: "w")
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize), keyEquivalent: "m")
        main.addItem(submenu: window, title: "Window")
        NSApp.windowsMenu = window

        return main
    }

    private func setUpStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        // The default status-item size draws the circle small; 16pt fills the menu bar height.
        statusItem.button?.image = NSImage(
            systemSymbolName: "waveform.circle", accessibilityDescription: "Aloud"
        )?.withSymbolConfiguration(.init(pointSize: 16, weight: .regular))

        let menu = NSMenu()
        menu.addItem(withTitle: "Read Clipboard", action: #selector(readClipboard), keyEquivalent: "")
        menu.addItem(withTitle: "Show Reader", action: #selector(showReader), keyEquivalent: "")
        menu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Aloud", action: #selector(NSApplication.terminate), keyEquivalent: "")
        for item in menu.items where item.action != #selector(NSApplication.terminate) { item.target = self }
        statusMenu = menu

        // Left click opens the reader; right click (or ⌃-click) opens the menu.
        statusItem.button?.target = self
        statusItem.button?.action = #selector(statusItemClicked)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    @objc private func statusItemClicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            // Attaching the menu only for this click keeps left clicks going to the action.
            statusItem.menu = statusMenu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            showReader()
        }
    }
}

private extension NSMenu {
    func addItem(submenu: NSMenu, title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        addItem(item)
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
