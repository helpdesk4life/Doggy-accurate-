import CoreLocation
import Foundation
import Observation

/// Everything the walk session needs from the rest of the app.
///
/// Kept as a protocol so this file does not depend on the auth and settings
/// layers, which are ordinary work and do not affect the background strategy.
/// `AuthStore` and `SettingsStore` conform to it once they exist.
@MainActor
protocol WalkSessionEnvironment: AnyObject {
    var backendURL: URL { get }
    var displayName: String { get }
    var warningDistanceMeters: Double { get }
    var markedDogs: [String: MarkedDog] { get }
    var accessToken: String? { get }

    /// Exchanges the refresh token for a new pair. Returns false when the
    /// refresh token is dead too, which means the user has to sign in again.
    func refreshAccessToken() async -> Bool

    func signOut()
}

/// Single source of truth for the walk session.
///
/// The location manager pushes GPS fixes in, the socket pushes proximity frames
/// back, and the UI observes the resulting state. This is the iOS counterpart of
/// `WalkRepository` plus `LocationTrackingService` on Android, collapsed into one
/// object because iOS has no foreground service to host the second half.
///
/// Also owns session renewal: when the socket reports an expired access token it
/// exchanges the refresh token for a new pair and reconnects. If the refresh
/// token is dead too, the session is cleared and the UI falls back to login.
@MainActor
@Observable
final class WalkSession {

    private(set) var isTracking = false
    private(set) var selfLocation: WalkerLocation?
    private(set) var nearby: [NearbyWalker] = []
    private(set) var radiusMeters: Double = 500
    private(set) var connection: ConnectionState = .disconnected
    private(set) var authorizationStatus: CLAuthorizationStatus = .notDetermined
    private(set) var hasPreciseAccuracy = true

    private let environment: WalkSessionEnvironment
    private let location = WalkLocationManager()
    private let notifier = ProximityNotifier()
    private let socket: WalkSocketClient
    private let defaults: UserDefaults

    /// Walkers already alerted on, so a walker standing still does not buzz on
    /// every fix.
    private var alertedIds: Set<String> = []

    private static let walkActiveKey = "dogwalk.walkActive"

    init(environment: WalkSessionEnvironment, defaults: UserDefaults = .standard) {
        self.environment = environment
        self.defaults = defaults
        self.socket = WalkSocketClient { [weak environment] in environment?.accessToken }

        socket.onStateChange = { [weak self] state in self?.connection = state }
        socket.onFrame = { [weak self] frame in self?.handle(frame) }
        socket.onAuthExpired = { [weak self] in
            Task { await self?.renewSession() }
        }

        location.onLocation = { [weak self] fix in self?.publish(fix) }
        location.onAuthorizationChange = { [weak self] status in
            self?.authorizationStatus = status
        }
        location.onAccuracyChange = { [weak self] reduced in
            self?.hasPreciseAccuracy = !reduced
        }

        // Seed from the manager rather than waiting for the delegate to fire.
        // `resumeIfNeeded()` runs at launch, before any delegate callback, and
        // would otherwise see `.notDetermined` and refuse to resume a walk.
        authorizationStatus = location.authorizationStatus
        hasPreciseAccuracy = !location.hasReducedAccuracy
    }

    // MARK: - Lifecycle

    /// Starts a walk. Call from a foreground moment — a button tap or a resume
    /// handler — never from a push handler.
    func startWalk() async {
        guard !isTracking else { return }

        await notifier.requestAuthorization()
        location.requestAuthorization()

        isTracking = true
        defaults.set(true, forKey: Self.walkActiveKey)

        location.start()
        socket.connect(endpoint: environment.backendURL, displayName: environment.displayName)
    }

    func stopWalk() {
        guard isTracking else { return }
        isTracking = false
        defaults.set(false, forKey: Self.walkActiveKey)

        location.stop()
        socket.disconnect()
        nearby = []
        selfLocation = nil
        alertedIds = []
    }

    /// Restarts a walk that was running when the system terminated the app.
    ///
    /// Apple: "If your app actively receives and processes location updates and
    /// terminates, it should restart those APIs upon launch in order to continue
    /// receiving updates." Call this from `application(_:didFinishLaunchingWithOptions:)`.
    ///
    /// This cannot tell a system termination from a user force-quit, so it will
    /// also resume after a force-quit. That is the intended trade-off: a walk the
    /// user did not end is a walk they still expect to be running.
    func resumeIfNeeded() {
        guard !isTracking, defaults.bool(forKey: Self.walkActiveKey) else { return }
        guard authorizationStatus == .authorizedWhenInUse
            || authorizationStatus == .authorizedAlways
        else { return }

        isTracking = true
        location.restartAfterTermination()
        socket.connect(endpoint: environment.backendURL, displayName: environment.displayName)
    }

    // MARK: - Outbound

    private func publish(_ fix: WalkerLocation) {
        selfLocation = fix
        socket.sendLocation(fix)
    }

    // MARK: - Inbound

    private func handle(_ frame: ServerFrame) {
        switch frame.type {
        case FrameType.proximityAlert:
            if let radius = frame.radiusMeters { radiusMeters = radius }

            let walkers = (frame.walkers ?? []).map { dto in
                NearbyWalker(
                    id: dto.id,
                    name: dto.name ?? "",
                    bearingDegrees: dto.bearingDegrees ?? 0,
                    distanceMeters: dto.distanceMeters ?? 0
                )
            }
            nearby = walkers

            // Everyone inside the server radius stays on the map; only walkers
            // inside the user's warning distance raise an alert, and only on
            // entry.
            let warningDistance = environment.warningDistanceMeters
            let withinWarning = walkers.filter { $0.distanceMeters <= warningDistance }
            let ids = Set(withinWarning.map(\.id))

            guard ids != alertedIds else { return }

            // Only the ones that just arrived. Re-alerting about a dog that was
            // already inside would replay its sound every time somebody else
            // appeared, which is especially wrong now that the sound depends on
            // who it is.
            let arrived = withinWarning.filter { !alertedIds.contains($0.id) }
            alertedIds = ids

            if !arrived.isEmpty {
                let marked = environment.markedDogs
                Task { await notifier.notifyProximity(arrived, marked: marked) }
            }

        case FrameType.proximityClear:
            nearby = []
            alertedIds = []

        case FrameType.welcome:
            if let radius = frame.radiusMeters { radiusMeters = radius }

        default:
            break
        }
    }

    // MARK: - Session renewal

    private func renewSession() async {
        let renewed = await environment.refreshAccessToken()
        guard renewed else {
            signOut()
            return
        }
        socket.reconnect()
    }

    /// Tears the walk down and drops the session.
    private func signOut() {
        isTracking = false
        defaults.set(false, forKey: Self.walkActiveKey)
        location.stop()
        socket.disconnect()
        nearby = []
        selfLocation = nil
        alertedIds = []
        environment.signOut()
    }
}
