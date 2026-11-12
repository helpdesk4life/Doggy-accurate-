import Foundation
import OSLog

/// WebSocket transport for the walk session.
///
/// Owns reconnection with exponential backoff (1 s -> 30 s cap) so callers only
/// have to observe `state` and `onFrame`. The access token travels in the
/// `Authorization` header rather than the query string, so it never appears in
/// the server's access log.
///
/// When the server closes with 4401 the socket does **not** retry on its own: it
/// reports `onAuthExpired` and waits for the session to refresh the token and
/// call `reconnect()`. Retrying with the same dead token would just loop.
///
/// This is the piece that makes the port viable. With the `location` background
/// mode active the app is not suspended, so this socket stays open with the
/// phone in a pocket — exactly like the Android foreground service. See
/// `WalkLocationManager` for the settings that guarantee that.
@MainActor
final class WalkSocketClient: NSObject {

    private enum CloseCode {
        static let normal = 1000
        static let unauthorized = 4401
    }

    private static let baseBackoff: TimeInterval = 1
    private static let maxBackoff: TimeInterval = 30
    private static let pingInterval: TimeInterval = 20
    private static let logger = Logger(subsystem: "com.dogwalk", category: "WalkSocket")

    private let accessToken: @MainActor () -> String?

    /// `URLSession.delegate` is read-only, so the session has to be built with
    /// `self` already available. A lazy var is the only way to do that.
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.default
        // A background reconnect can fire while the radio is asleep. Without
        // this the attempt fails immediately instead of waiting for a network.
        configuration.waitsForConnectivity = true
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    private var task: URLSessionWebSocketTask?
    private var endpoint: URL?
    private var displayName = ""
    private var reconnectAttempt = 0
    private var reconnectTask: Task<Void, Never>?
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var stopped = true

    private(set) var state: ConnectionState = .disconnected {
        didSet {
            guard state != oldValue else { return }
            onStateChange?(state)
        }
    }

    var onStateChange: (@MainActor (ConnectionState) -> Void)?
    var onFrame: (@MainActor (ServerFrame) -> Void)?
    var onAuthExpired: (@MainActor () -> Void)?

    init(accessToken: @escaping @MainActor () -> String?) {
        self.accessToken = accessToken
        super.init()
    }

    // MARK: - Public surface

    func connect(endpoint: URL, displayName: String) {
        self.endpoint = endpoint
        self.displayName = displayName
        stopped = false
        reconnectAttempt = 0
        reconnectTask?.cancel()
        reconnectTask = nil
        open()
    }

    func disconnect() {
        stopped = true
        reconnectTask?.cancel()
        reconnectTask = nil
        receiveTask?.cancel()
        receiveTask = nil
        pingTask?.cancel()
        pingTask = nil
        task?.cancel(with: .normalClosure, reason: Data("walk finished".utf8))
        task = nil
        state = .disconnected
    }

    /// Re-opens after the caller has refreshed the access token.
    func reconnect() {
        guard !stopped else { return }
        reconnectAttempt = 0
        open()
    }

    /// Returns false when the socket is not open, so the caller can drop the fix.
    @discardableResult
    func sendLocation(_ location: WalkerLocation) -> Bool {
        guard let task, state == .connected else { return false }

        let frame = LocationFrame(
            lat: location.latitude,
            lon: location.longitude,
            accuracy: location.accuracyMeters,
            ts: location.timestampMillis
        )
        guard let data = try? JSONEncoder().encode(frame),
              let text = String(data: data, encoding: .utf8)
        else { return false }

        // URLSessionWebSocketTask delivers sends in the order they were
        // enqueued, so fixes cannot overtake each other.
        Task { [weak self] in
            do {
                try await task.send(.string(text))
            } catch {
                self?.handleFailure(error)
            }
        }
        return true
    }

    // MARK: - Connection

    private func open() {
        guard let endpoint else { return }

        guard let token = accessToken() else {
            state = .disconnected
            onAuthExpired?()
            return
        }

        task?.cancel(with: .goingAway, reason: nil)
        state = .connecting

        var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)
        var items = components?.queryItems ?? []
        items.append(URLQueryItem(name: "name", value: displayName))
        components?.queryItems = items

        guard let url = components?.url else {
            state = .disconnected
            return
        }

        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let newTask = session.webSocketTask(with: request)
        task = newTask
        newTask.resume()
        startReceiveLoop()
        startPing()
    }

    private func startReceiveLoop() {
        receiveTask?.cancel()
        receiveTask = Task { [weak self] in
            while true {
                guard let self, let task = self.task else { return }
                do {
                    let message = try await task.receive()
                    self.handle(message)
                } catch {
                    self.handleFailure(error)
                    return
                }
            }
        }
    }

    /// URLSessionWebSocketTask does not ping on its own. Without this an idle
    /// socket is dropped by NAT and by the backend's own idle timeout, and the
    /// app looks connected while receiving nothing.
    private func startPing() {
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while true {
                try? await Task.sleep(nanoseconds: UInt64(Self.pingInterval * 1_000_000_000))
                guard let self, let task = self.task else { return }
                task.sendPing { _ in }
            }
        }
    }

    private func scheduleReconnect() {
        guard !stopped else { return }
        guard reconnectTask == nil else { return }

        let exponent = Double(min(reconnectAttempt, 5))
        let delay = min(Self.maxBackoff, Self.baseBackoff * pow(2, exponent))
        reconnectAttempt += 1

        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            self.reconnectTask = nil
            guard !self.stopped else { return }
            self.open()
        }
    }

    // MARK: - Events

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let data: Data?
        switch message {
        case .string(let text): data = text.data(using: .utf8)
        case .data(let raw): data = raw
        @unknown default: data = nil
        }
        guard let data,
              let frame = try? JSONDecoder().decode(ServerFrame.self, from: data)
        else { return }
        onFrame?(frame)
    }

    private func handleOpen() {
        reconnectAttempt = 0
        state = .connected
    }

    private func handleClose(code: Int) {
        Self.logger.info("socket closed code=\(code)")
        state = .disconnected
        if code == CloseCode.unauthorized {
            onAuthExpired?()
        } else {
            scheduleReconnect()
        }
    }

    private func handleFailure(_ error: Error?) {
        // Without this the reason a connection failed is invisible: the UI only
        // shows "Offline", which looks identical for a TLS error, a refused
        // connection and a rejected token.
        if let error {
            Self.logger.warning("socket failure: \(error.localizedDescription, privacy: .public)")
        }
        state = .disconnected
        scheduleReconnect()
    }
}

// MARK: - URLSessionWebSocketDelegate

extension WalkSocketClient: URLSessionWebSocketDelegate {

    nonisolated func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didOpenWithProtocol _: String?
    ) {
        Task { @MainActor in self.handleOpen() }
    }

    nonisolated func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        // `CloseCode` is a Swift enum, so a non-standard code such as 4401 may
        // not round-trip through it. This is the documented path and it is what
        // the Android client relies on, but confirm on a real device that a
        // revoked token actually lands here as 4401 — see ios/README.md.
        let code = closeCode.rawValue
        Task { @MainActor in self.handleClose(code: code) }
    }
}
