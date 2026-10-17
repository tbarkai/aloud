import Foundation

/// Runs the Python reader server (server.py) as a child process.
@MainActor
final class Server {
    // Not server.py's default 7860, so a dev server run by hand (possibly
    // running older code) never stands in for the app's own.
    static let port = 7861
    static let url = URL(string: "http://127.0.0.1:\(port)/")!

    private var process: Process?
    private let projectDir: URL
    let logURL: URL

    init() {
        // Written into Info.plist by install.sh: this app runs the repo's server in place.
        let dir = Bundle.main.object(forInfoDictionaryKey: "ReaderProjectDir") as? String ?? ""
        projectDir = URL(fileURLWithPath: dir)
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Aloud")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("server.log")
    }

    /// Starts the server unless one is already answering on the port, then
    /// waits until it serves requests.
    func ensureRunning() async throws {
        if await isUp() { return }
        if process?.isRunning != true { try start() }
        for _ in 0..<240 {
            try await Task.sleep(for: .milliseconds(250))
            if await isUp() { return }
            if process?.isRunning != true { break }
        }
        throw ServerError.didNotStart(logURL)
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    private func start() throws {
        let python = projectDir.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw ServerError.missingVenv(python)
        }
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)

        let p = Process()
        p.executableURL = python
        p.arguments = ["server.py", "--port", String(Server.port)]
        p.currentDirectoryURL = projectDir
        p.standardOutput = log
        p.standardError = log
        try p.run()
        process = p
    }

    private func isUp() async -> Bool {
        var request = URLRequest(url: Server.url)
        request.timeoutInterval = 1
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }
}

enum ServerError: LocalizedError {
    case missingVenv(URL)
    case didNotStart(URL)

    var errorDescription: String? {
        switch self {
        case .missingVenv(let python):
            return "Python environment not found at \(python.path). Run mac/install.sh again."
        case .didNotStart(let log):
            return "The reader server did not start. See \(log.path)."
        }
    }
}
