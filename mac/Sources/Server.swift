import Foundation

/// Where the app's own server listens, and the token it requires.
struct ServerEndpoint: Equatable {
    static let cookieName = "aloud_token"
    let url: URL
    let token: String

    /// Carries the token for the web view; set it in the page's cookie store.
    var cookie: HTTPCookie {
        HTTPCookie(properties: [
            .originURL: url, .path: "/", .name: Self.cookieName, .value: token,
            .sameSitePolicy: HTTPCookieStringPolicy.sameSiteStrict,
        ])!
    }

    func request(_ path: String) -> URLRequest {
        var request = URLRequest(url: url.appendingPathComponent(path))
        request.httpShouldHandleCookies = false
        request.setValue("\(Self.cookieName)=\(token)", forHTTPHeaderField: "Cookie")
        return request
    }
}

/// Runs the Python reader server (server.py) as a child process.
@MainActor
final class Server {
    private var process: Process?
    // Held open for the child's lifetime: it exits when this closes, even if the app crashes.
    private var stdin: Pipe?
    private var launch: Task<ServerEndpoint, Error>?
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

    /// Starts the server unless it's already running, and returns where it listens.
    func ensureRunning() async throws -> ServerEndpoint {
        if launch == nil || process?.isRunning != true {
            stop()
            launch = try start()
        }
        let launch = launch!
        do {
            return try await launch.value
        } catch {
            // Let the next call start afresh instead of repeating this failure.
            if self.launch == launch { stop() }
            throw error
        }
    }

    func stop() {
        process?.terminate()
        process = nil
        stdin = nil
        launch = nil
    }

    private func start() throws -> Task<ServerEndpoint, Error> {
        let python = projectDir.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw ServerError.missingVenv(python)
        }
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)
        let token = UUID().uuidString
        let input = Pipe()
        let output = Pipe()

        let p = Process()
        p.executableURL = python
        p.arguments = ["server.py", "--app"]
        p.currentDirectoryURL = projectDir
        // In the environment rather than the arguments, which other users can see in ps.
        p.environment = ProcessInfo.processInfo.environment.merging(["ALOUD_TOKEN": token]) { $1 }
        p.standardInput = input
        p.standardOutput = output
        p.standardError = log
        try p.run()
        process = p
        stdin = input

        let logURL = logURL
        return Task {
            let timeout = Task {
                try await Task.sleep(for: .seconds(60))
                p.terminate()
            }
            defer { timeout.cancel() }
            // The server prints its port once it's serving, then closes stdout;
            // anything an import printed comes before it. The read blocks, so
            // it runs on a dispatch thread rather than one of Swift's few task threads.
            let reader = output.fileHandleForReading
            let printed = await withCheckedContinuation { continuation in
                DispatchQueue.global().async { continuation.resume(returning: reader.readDataToEndOfFile()) }
            }
            guard let line = String(decoding: printed, as: UTF8.self).split(whereSeparator: \.isNewline).last,
                  let port = Int(line)
            else { throw ServerError.didNotStart(logURL) }
            return ServerEndpoint(url: URL(string: "http://127.0.0.1:\(port)/")!, token: token)
        }
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
