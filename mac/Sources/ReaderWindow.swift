import AppKit
import WebKit

/// One reader window, reused for every selection. Shows the server's web UI
/// in embed mode under a native toolbar, and pushes the selected text into it.
@MainActor
final class ReaderWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    let window: NSWindow
    private let webView: WKWebView
    private let settings: Settings
    private let toolbar = ReaderToolbar()
    private let progressLine = ProgressLine()
    private var pageLoaded = false
    private var loadingReader = false
    private var pending: (text: String, markdown: Bool)?

    init(settings: Settings) {
        self.settings = settings
        let config = WKWebViewConfiguration()
        // Start reading as soon as text arrives, without a click in the page.
        config.mediaTypesRequiringUserActionForPlayback = []
        webView = WKWebView(frame: .zero, configuration: config)
        // Match the page background while it loads, instead of flashing white in dark mode.
        webView.underPageBackgroundColor = .textBackgroundColor

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
            // Full-size content: the page runs under the toolbar and scrolls beneath its glass.
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Aloud"
        window.titleVisibility = .hidden
        window.toolbar = toolbar.toolbar
        window.toolbarStyle = .unified
        // Text scrolling under the glass marks the edge; a separator line would double it.
        window.titlebarSeparatorStyle = .none
        let container = NSView()
        for view in [webView, progressLine] as [NSView] {
            view.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(view)
        }
        window.contentView = container
        let contentTop = (window.contentLayoutGuide as! NSLayoutGuide).topAnchor
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            // Centered on the toolbar's bottom edge.
            progressLine.centerYAnchor.constraint(equalTo: contentTop),
            progressLine.heightAnchor.constraint(equalToConstant: 2),
            progressLine.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            progressLine.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ReaderWindow")
        if !window.setFrameUsingName("ReaderWindow") { window.center() }
        super.init()
        window.delegate = self
        webView.navigationDelegate = self

        webView.configuration.userContentController.add(self, name: "reader")

        toolbar.onTogglePlay = { [weak self] in self?.webView.evaluateJavaScript("togglePlay()") }
        toolbar.onSpeedChange = { [weak self] speed in
            self?.webView.callAsyncJavaScript(
                "readerSetSpeed(v)", arguments: ["v": speed], in: nil, in: .page
            ) { _ in }
        }
    }

    // The page reports playback state on every change; the toolbar mirrors it.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let state = message.body as? [String: Any] else { return }
        toolbar.update(
            playing: state["playing"] as? Bool ?? false,
            canPlay: state["canPlay"] as? Bool ?? false,
            speed: (state["speed"] as? NSNumber)?.doubleValue ?? settings.defaultSpeed,
            message: state["message"] as? String ?? ""
        )
        progressLine.setProgress((state["progress"] as? NSNumber)?.doubleValue ?? 0)
    }

    func show() {
        window.bringToFront()
    }

    /// Queues `text` to be read once the page is up; call `serverReady()` after.
    /// Markdown is cleaned up by the server before reading.
    func read(_ text: String, markdown: Bool = false) {
        pending = (text, markdown)
        show()
        if pageLoaded { deliverPending() }
    }

    func serverReady() {
        guard !pageLoaded, !loadingReader else { return }
        loadingReader = true
        var components = URLComponents(url: Server.url, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "embed", value: "1")]
        webView.load(URLRequest(url: components.url!))
    }

    func showMessage(_ message: String) {
        pageLoaded = false
        loadingReader = false
        toolbar.update(playing: false, canPlay: false, speed: settings.defaultSpeed, message: "")
        progressLine.setProgress(0)
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString(
            """
            <body style="font: 14px -apple-system; color: #6b7280; display: grid;
              place-items: center; height: 90vh; margin: 0">\(escaped)</body>
            """,
            baseURL: nil
        )
        show()
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        // A file dropped outside the page's drop handling would replace the reader.
        decisionHandler(action.request.url?.isFileURL == true ? .cancel : .allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView.url?.host == Server.url.host else { return }
        loadingReader = false
        pageLoaded = true
        webView.evaluateJavaScript("syncApp()")
        deliverPending()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        loadFailed(error)
    }

    func webView(
        _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error
    ) {
        loadFailed(error)
    }

    private func loadFailed(_ error: Error) {
        // A navigation we replaced ourselves (e.g. by showMessage) reports as cancelled.
        if (error as NSError).code == NSURLErrorCancelled { return }
        showMessage(error.localizedDescription)
    }

    private func deliverPending() {
        guard let (text, markdown) = pending else { return }
        pending = nil
        // Voice and default speed are read now, so a new text always starts
        // from the current settings rather than the last text's speed.
        webView.callAsyncJavaScript(
            "await readerLoad(text, settings, markdown)",
            arguments: [
                "text": text,
                "settings": ["voice": settings.voice, "speed": settings.defaultSpeed],
                "markdown": markdown,
            ],
            in: nil, in: .page
        ) { _ in }
    }

    func clear() {
        pending = nil
        guard pageLoaded else { return }
        webView.evaluateJavaScript("readerClear()")
    }

    func setVoice(_ voice: String) {
        guard pageLoaded else { return }
        webView.callAsyncJavaScript("readerSetVoice(v)", arguments: ["v": voice], in: nil, in: .page) { _ in }
    }

    func windowWillClose(_ notification: Notification) {
        // Stop reading when the window goes away; the page stays loaded for next time.
        webView.evaluateJavaScript("pause()")
    }
}

extension NSWindow {
    /// Shows the window in front with keyboard focus.
    func bringToFront() {
        AppActivation.activate()
        makeKeyAndOrderFront(nil)
        // Ordered front even if activation is slow or refused, so the window
        // never opens hidden behind the app the user came from.
        orderFrontRegardless()
    }
}

@MainActor
enum AppActivation {
    /// Until then, a reopen event comes from our own activation request
    /// rather than from the user, and must not open the reader.
    static var ownReopenUntil = Date.distantPast

    static func activate() {
        guard !NSApp.isActive else { return }
        // macOS refuses NSApp.activate() from a background menu bar app, even
        // right after a click on its menu bar icon. Asking Launch Services to
        // open the app, as `open -a` does, is honored.
        ownReopenUntil = Date().addingTimeInterval(1)
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: config)
    }
}
