import AppKit
import WebKit

/// One reader window, reused for every selection. Shows the server's web UI
/// in embed mode and pushes the selected text into it.
@MainActor
final class ReaderWindow: NSObject, NSWindowDelegate, WKNavigationDelegate {
    private let window: NSWindow
    private let webView: WKWebView
    private var pageLoaded = false
    private var loadingReader = false
    private var pendingText: String?

    override init() {
        let config = WKWebViewConfiguration()
        // Start reading as soon as text arrives, without a click in the page.
        config.mediaTypesRequiringUserActionForPlayback = []
        webView = WKWebView(frame: .zero, configuration: config)

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Kokoro Reader"
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ReaderWindow")
        if !window.setFrameUsingName("ReaderWindow") { window.center() }
        super.init()
        window.delegate = self
        webView.navigationDelegate = self
    }

    func show() {
        // macOS may decline to activate a background app; bring the window
        // to the front regardless so it never opens hidden behind the source app.
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    /// Queues `text` to be read once the page is up; call `serverReady()` after.
    func read(_ text: String) {
        pendingText = text
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

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView.url?.host == Server.url.host else { return }
        loadingReader = false
        pageLoaded = true
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
        guard let text = pendingText else { return }
        pendingText = nil
        webView.callAsyncJavaScript(
            "await readerLoad(text)", arguments: ["text": text], in: nil, in: .page
        ) { _ in }
    }

    func windowWillClose(_ notification: Notification) {
        // Stop reading when the window goes away; the page stays loaded for next time.
        webView.evaluateJavaScript("pause()")
    }
}
