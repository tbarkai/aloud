import AppKit

/// A 2pt accent line along the toolbar's bottom edge, filled to show how much
/// of the text has been read.
final class ProgressLine: NSView {
    private let fill = CALayer()
    private var progress: CGFloat = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.addSublayer(fill)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func setProgress(_ value: Double) {
        let value = CGFloat(min(max(value, 0), 1))
        guard value != progress else { return }
        // Small forward steps arrive several times a second while reading;
        // a short linear animation joins them into continuous motion.
        let animate = value > progress && value - progress < 0.2
        progress = value
        CATransaction.begin()
        CATransaction.setAnimationDuration(animate ? 0.3 : 0)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .linear))
        layoutFill()
        CATransaction.commit()
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        layoutFill()
        CATransaction.commit()
    }

    override func updateLayer() {
        fill.backgroundColor = NSColor.controlAccentColor.cgColor
    }

    override var wantsUpdateLayer: Bool { true }

    private func layoutFill() {
        fill.frame = CGRect(x: 0, y: 0, width: bounds.width * progress, height: bounds.height)
    }

    // Purely visual: clicks go through to the page.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
