import AppKit

/// The reader window's toolbar: play/pause, a speed slider for this text, and
/// the progress status. Native items, so macOS draws them as Liquid Glass. The
/// page owns playback; this mirrors its state and forwards clicks to it.
@MainActor
final class ReaderToolbar: NSObject, NSToolbarDelegate {
    let toolbar = NSToolbar(identifier: "ReaderToolbar")
    var onTogglePlay: () -> Void = {}
    var onSpeedChange: (Double) -> Void = { _ in }

    private let playItem = NSToolbarItem(itemIdentifier: .play)
    private let slider = NSSlider(
        value: 1.3, minValue: Settings.speedRange.lowerBound, maxValue: Settings.speedRange.upperBound,
        target: nil, action: nil
    )
    private let speedLabel = NSTextField(labelWithString: "1.3×")
    private let statusLabel = NSTextField(labelWithString: "")

    override init() {
        super.init()
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false

        playItem.label = "Play"
        playItem.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: "Play")
        playItem.target = self
        playItem.action = #selector(playClicked)
        playItem.isBordered = true
        playItem.autovalidates = false
        playItem.isEnabled = false

        slider.target = self
        slider.action = #selector(speedChanged)
        slider.controlSize = .small
        slider.widthAnchor.constraint(equalToConstant: 110).isActive = true
        speedLabel.font = .monospacedDigitSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        speedLabel.widthAnchor.constraint(equalToConstant: 32).isActive = true

        statusLabel.font = .monospacedDigitSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.alignment = .right
    }

    func update(playing: Bool, canPlay: Bool, speed: Double, status: String) {
        playItem.isEnabled = canPlay
        playItem.label = playing ? "Pause" : "Play"
        playItem.image = NSImage(
            systemSymbolName: playing ? "pause.fill" : "play.fill", accessibilityDescription: playItem.label
        )
        slider.doubleValue = speed
        speedLabel.stringValue = String(format: "%.1f×", speed)
        statusLabel.stringValue = status
    }

    @objc private func playClicked() {
        onTogglePlay()
    }

    @objc private func speedChanged() {
        // Snap to the page's 0.1 steps without drawing tick marks in the toolbar.
        let speed = (slider.doubleValue * 10).rounded() / 10
        speedLabel.stringValue = String(format: "%.1f×", speed)
        onSpeedChange(speed)
    }

    // MARK: NSToolbarDelegate

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.play, .speed, .flexibleSpace, .status]
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }

    func toolbar(
        _ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier, willBeInsertedIntoToolbar: Bool
    ) -> NSToolbarItem? {
        switch id {
        case .play:
            return playItem
        case .speed:
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = "Speed"
            item.toolTip = "Speed for this text. The default is in Settings."
            let stack = NSStackView(views: [slider, speedLabel])
            stack.spacing = 6
            stack.edgeInsets = NSEdgeInsets(top: 0, left: 8, bottom: 0, right: 4)
            item.view = stack
            return item
        case .status:
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = "Progress"
            item.view = statusLabel
            return item
        default:
            return nil
        }
    }
}

private extension NSToolbarItem.Identifier {
    static let play = Self("play")
    static let speed = Self("speed")
    static let status = Self("status")
}
