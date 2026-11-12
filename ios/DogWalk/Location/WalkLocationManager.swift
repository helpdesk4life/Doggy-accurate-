import CoreLocation
import Foundation

/// Owns the GPS subscription for the duration of a walk.
///
/// This is the iOS equivalent of Android's `LocationTrackingService`, and it is
/// the file that decides whether the port works. The configuration below is not
/// arbitrary: each line is required for iOS to keep the app *running* — and
/// therefore the WebSocket open — while the phone is in a pocket.
///
/// Apple, "Handling location updates in the background":
///
/// > "If your app needs updates in a more timely manner, you can ask the system
/// > not suspend your app while location services are active."
///
/// Apple DTS, forum thread 35065, answering this exact question about holding a
/// WebSocket open in the background:
///
/// > "If so, your app will never get suspended … if you can prevent your app
/// > from suspending then networking in the background works just like
/// > networking in the foreground."
///
/// So the socket survives. What follows is the set of settings that make that
/// sentence true, and the ways it silently stops being true.
@MainActor
final class WalkLocationManager: NSObject {

    /// Called for every fix. The session forwards these to the socket.
    var onLocation: (@MainActor (WalkerLocation) -> Void)?

    /// Called when authorization changes, so the UI can explain what is missing.
    var onAuthorizationChange: (@MainActor (CLAuthorizationStatus) -> Void)?

    /// Called when the user has granted only approximate location. Proximity
    /// alerts are meaningless in that mode, so the UI has to say so.
    var onAccuracyChange: (@MainActor (Bool) -> Void)?

    /// Called for errors that are not the transient `kCLErrorLocationUnknown`.
    var onError: (@MainActor (Error) -> Void)?

    private let manager = CLLocationManager()
    private(set) var isRunning = false

    /// Set when `start()` was called before the user answered the permission
    /// prompt. `startUpdatingLocation()` is a no-op without authorization and
    /// does not begin on its own once it is granted, so the delegate has to
    /// finish the job.
    private var pendingStart = false

    override init() {
        super.init()
        manager.delegate = self

        // Best accuracy: a proximity alert at city-block resolution is useless.
        // This is the most expensive setting Core Location offers, so it is
        // worth measuring battery on a real device before shipping.
        manager.desiredAccuracy = kCLLocationAccuracyBest

        // No distance filter, for two reasons. DogWalk wants a steady stream of
        // fixes, and since iOS 16.4 combining `startUpdatingLocation()` with
        // significant-change monitoring at low accuracy *and* distance
        // filtering can get the app suspended.
        manager.distanceFilter = kCLDistanceFilterNone

        // Defaults to `true`. For a When In Use app a pause "ends access to
        // location changes until the app launches again" — the walk silently
        // dies and never comes back. Must be false.
        manager.pausesLocationUpdatesAutomatically = false

        // Tells Core Location how to interpret movement, which feeds its pause
        // heuristic. `.otherNavigation` is the honest description of a walk and
        // carries no documented pause note, unlike `.automotiveNavigation` and
        // `.fitness`.
        manager.activityType = .otherNavigation

        // The blue pill. It is visible for as long as background location is in
        // use, and hiding it is not an option — nor would it be honest.
        manager.showsBackgroundLocationIndicator = true
    }

    // MARK: - Authorization

    /// Asks for **When In Use only**.
    ///
    /// Always authorization is not what keeps updates flowing in the background.
    /// Apple: "If you enable background location updates, an app with When in
    /// Use authorization continues to run in the background when location
    /// services are active." Always only adds relaunch-after-termination, and
    /// only for significant-change, visits and region monitoring — not for
    /// standard updates. Asking for When In Use only removes the single biggest
    /// App Store review risk in this port.
    func requestAuthorization() {
        guard manager.authorizationStatus == .notDetermined else { return }
        manager.requestWhenInUseAuthorization()
    }

    var authorizationStatus: CLAuthorizationStatus { manager.authorizationStatus }

    /// True when the user granted approximate rather than precise location.
    var hasReducedAccuracy: Bool { manager.accuracyAuthorization == .reducedAccuracy }

    // MARK: - Lifecycle

    /// Starts updates. **Must be called while the app is in the foreground.**
    ///
    /// Apple: "attempts to start location updates while your app runs in the
    /// background will fail." Never call this from a push handler or a
    /// background task.
    func start() {
        guard !isRunning else { return }

        // Setting `allowsBackgroundLocationUpdates = true` without the
        // `UIBackgroundModes` / `location` entry is a fatal error that
        // terminates the app. Check rather than crash.
        guard hasLocationBackgroundMode else {
            assertionFailure(
                "Info.plist is missing UIBackgroundModes -> location. "
                    + "See ios/DogWalk/Resources/Info.plist."
            )
            return
        }

        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            pendingStart = false
            manager.allowsBackgroundLocationUpdates = true
            manager.startUpdatingLocation()
            isRunning = true

        case .notDetermined:
            // Ask, then let the delegate start updates once the user answers.
            pendingStart = true
            manager.requestWhenInUseAuthorization()

        default:
            // Denied or restricted. Nothing to start.
            pendingStart = false
        }
    }

    func stop() {
        guard isRunning else { return }
        manager.stopUpdatingLocation()
        manager.allowsBackgroundLocationUpdates = false
        isRunning = false
    }

    /// Restarts updates after the system terminated the app mid-walk.
    ///
    /// Apple: "The system can terminate apps at any time to free up memory or
    /// other system resources. If your app actively receives and processes
    /// location updates and terminates, it should restart those APIs upon launch
    /// in order to continue receiving updates."
    ///
    /// Whether this actually succeeds when the app was relaunched into the
    /// background is the one part of this file that could not be verified from
    /// documentation alone. Test it on hardware.
    func restartAfterTermination() {
        guard !isRunning else { return }
        guard manager.authorizationStatus == .authorizedWhenInUse
            || manager.authorizationStatus == .authorizedAlways
        else { return }
        start()
    }

    private var hasLocationBackgroundMode: Bool {
        let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String]
        return modes?.contains("location") ?? false
    }
}

// MARK: - CLLocationManagerDelegate

extension WalkLocationManager: CLLocationManagerDelegate {

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        // Map before hopping, so the CLLocation objects never cross an actor
        // boundary.
        let fixes = locations.map {
            WalkerLocation(
                latitude: $0.coordinate.latitude,
                longitude: $0.coordinate.longitude,
                accuracyMeters: $0.horizontalAccuracy,
                timestampMillis: Int64(($0.timestamp.timeIntervalSince1970 * 1000).rounded())
            )
        }
        Task { @MainActor in
            for fix in fixes { self.onLocation?(fix) }
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        let reduced = manager.accuracyAuthorization == .reducedAccuracy
        Task { @MainActor in
            self.onAuthorizationChange?(status)
            self.onAccuracyChange?(reduced)

            // Finish a start that was waiting on the permission prompt.
            if self.pendingStart,
               status == .authorizedWhenInUse || status == .authorizedAlways {
                self.pendingStart = false
                self.start()
            }
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didFailWithError error: Error
    ) {
        // `kCLErrorLocationUnknown` means "no fix yet", which is normal indoors
        // and self-healing. Anything else is worth surfacing.
        if let clError = error as? CLError, clError.code == .locationUnknown { return }
        Task { @MainActor in self.onError?(error) }
    }
}
