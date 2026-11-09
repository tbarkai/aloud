import AppKit
import Carbon.HIToolbox

/// A key combination, stored in UserDefaults as JSON.
struct Shortcut: Codable, Equatable {
    var keyCode: UInt32
    /// Device-independent NSEvent.ModifierFlags raw value.
    var modifiers: UInt
    /// The key as shown to the user, e.g. "S" or "Space".
    var key: String

    static let showReaderDefault = Shortcut(
        keyCode: UInt32(kVK_ANSI_S), modifiers: NSEvent.ModifierFlags([.control, .command]).rawValue, key: "S"
    )

    /// A shortcut from a key press, or nil when it has no ⌃, ⌥ or ⌘
    /// (a bare key or ⇧-key would fire while typing in other apps).
    init?(event: NSEvent) {
        let flags = event.modifierFlags.intersection([.control, .option, .shift, .command])
        guard !flags.isDisjoint(with: [.control, .option, .command]) else { return nil }
        keyCode = UInt32(event.keyCode)
        modifiers = flags.rawValue
        key = Shortcut.keyNames[Int(event.keyCode)]
            ?? event.charactersIgnoringModifiers?.uppercased()
            ?? "?"
    }

    init(keyCode: UInt32, modifiers: UInt, key: String) {
        self.keyCode = keyCode
        self.modifiers = modifiers
        self.key = key
    }

    var display: String {
        let flags = NSEvent.ModifierFlags(rawValue: modifiers)
        var s = ""
        if flags.contains(.control) { s += "⌃" }
        if flags.contains(.option) { s += "⌥" }
        if flags.contains(.shift) { s += "⇧" }
        if flags.contains(.command) { s += "⌘" }
        return s + key
    }

    fileprivate var carbonModifiers: UInt32 {
        let flags = NSEvent.ModifierFlags(rawValue: modifiers)
        var m: UInt32 = 0
        if flags.contains(.control) { m |= UInt32(controlKey) }
        if flags.contains(.option) { m |= UInt32(optionKey) }
        if flags.contains(.shift) { m |= UInt32(shiftKey) }
        if flags.contains(.command) { m |= UInt32(cmdKey) }
        return m
    }

    /// A shortcut from a services key equivalent such as "@~s": modifier symbols, then the
    /// key, uppercase when ⇧ is part of it. Nil when the key isn't on the keyboard layout.
    init?(keyEquivalent: String) {
        guard let char = keyEquivalent.last else { return nil }
        var flags: NSEvent.ModifierFlags = []
        for symbol in keyEquivalent.dropLast() {
            switch symbol {
            case "@": flags.insert(.command)
            case "~": flags.insert(.option)
            case "^": flags.insert(.control)
            case "$": flags.insert(.shift)
            default: return nil
            }
        }
        if char.isUppercase { flags.insert(.shift) }
        guard let code = Shortcut.keyCode(producing: Character(char.lowercased())) else { return nil }
        self.init(keyCode: code, modifiers: flags.rawValue, key: char.uppercased())
    }

    /// Whether both press the same keys. `key` is left out: it is the character the
    /// layout produced when recorded, so the same key reads "S" in one layout and "ד" in another.
    func sameKeys(as other: Shortcut) -> Bool {
        keyCode == other.keyCode && modifiers == other.modifiers
    }

    /// The key that types `char` on the ASCII-capable layout, which AppKit matches
    /// ⌘ key equivalents against when the current layout isn't Latin.
    private static func keyCode(producing char: Character) -> UInt32? {
        guard let source = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(),
              let layoutData = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData)
        else { return nil }
        let data = Unmanaged<CFData>.fromOpaque(layoutData).takeUnretainedValue() as Data
        let target = String(char)
        return data.withUnsafeBytes { raw -> UInt32? in
            guard let layout = raw.bindMemory(to: UCKeyboardLayout.self).baseAddress else { return nil }
            // Main keys come before the keypad, so digits resolve to the top row.
            for code in UInt16(0)..<128 {
                var deadKeys: UInt32 = 0
                var length = 0
                var chars = [UniChar](repeating: 0, count: 4)
                let status = UCKeyTranslate(
                    layout, code, UInt16(kUCKeyActionDisplay), 0, UInt32(LMGetKbdType()),
                    OptionBits(kUCKeyTranslateNoDeadKeysBit), &deadKeys, chars.count, &length, &chars
                )
                if status == noErr, String(utf16CodeUnits: chars, count: length) == target {
                    return UInt32(code)
                }
            }
            return nil
        }
    }

    // Keys whose characters don't print usefully.
    private static let keyNames: [Int: String] = [
        kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_Delete: "⌫", kVK_ForwardDelete: "⌦",
        kVK_LeftArrow: "←", kVK_RightArrow: "→", kVK_UpArrow: "↑", kVK_DownArrow: "↓",
        kVK_Home: "↖", kVK_End: "↘", kVK_PageUp: "⇞", kVK_PageDown: "⇟",
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5", kVK_F6: "F6",
        kVK_F7: "F7", kVK_F8: "F8", kVK_F9: "F9", kVK_F10: "F10", kVK_F11: "F11", kVK_F12: "F12",
    ]
}

/// A system-wide shortcut. Carbon's RegisterEventHotKey works in every app
/// without the Accessibility permission an event tap would need.
@MainActor
final class GlobalHotKey {
    var action: () -> Void = {}
    private var ref: EventHotKeyRef?

    init() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData in
                let hotKey = Unmanaged<GlobalHotKey>.fromOpaque(userData!).takeUnretainedValue()
                MainActor.assumeIsolated { hotKey.action() }
                return noErr
            },
            1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil
        )
    }

    /// Replaces the current shortcut; nil removes it. Returns false when
    /// another app already holds the combination.
    @discardableResult
    func register(_ shortcut: Shortcut?) -> Bool {
        if let ref {
            UnregisterEventHotKey(ref)
            self.ref = nil
        }
        guard let shortcut else { return true }
        let id = EventHotKeyID(signature: OSType(0x4B52_4452), id: 1)  // "KRDR"
        // Exclusive: fails if another app holds the combination, instead of silently sharing it.
        return RegisterEventHotKey(
            shortcut.keyCode, shortcut.carbonModifiers, id, GetApplicationEventTarget(),
            OptionBits(kEventHotKeyExclusive), &ref
        ) == noErr
    }
}

/// The Read in Aloud service declared under NSServices in Info.plist. macOS runs its
/// shortcut only when no app claims the key first, so a Show Reader hotkey on the same
/// key would swallow it and open the reader empty.
enum ReadSelectionService {
    /// Its shortcut as System Settings has it, or nil when it has none or is turned off.
    static func shortcut() -> Shortcut? {
        guard let bundleID = Bundle.main.bundleIdentifier,
              let service = (Bundle.main.infoDictionary?["NSServices"] as? [[String: Any]])?.first,
              let title = (service["NSMenuItem"] as? [String: String])?["default"],
              let message = service["NSMessage"] as? String
        else { return nil }
        let domain = "pbs" as CFString
        CFPreferencesAppSynchronize(domain)
        let status = CFPreferencesCopyAppValue("NSServicesStatus" as CFString, domain) as? [String: Any]
        let entry = status?["\(bundleID) - \(title) - \(message)"] as? [String: Any]
        // Unchecking the service in Keyboard Shortcuts keeps its key but stops it running.
        if (entry?["enabled_services_menu"] as? Bool) == false { return nil }
        // The user's key wins (empty when they removed it); without one, the Info.plist
        // default applies, a single character with ⌘ implied.
        let equivalent = entry?["key_equivalent"] as? String
            ?? ((service["NSKeyEquivalent"] as? [String: String])?["default"]).map { "@" + $0 }
        guard let equivalent, !equivalent.isEmpty else { return nil }
        return Shortcut(keyEquivalent: equivalent)
    }
}
