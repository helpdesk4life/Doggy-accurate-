import Foundation

/// Client -> server: one GPS fix.
///
/// Field names are snake-free here because the backend reads `lat`, `lon`,
/// `accuracy` and `ts` verbatim. Do not "improve" them.
struct LocationFrame: Encodable {
    let type = "location"
    let lat: Double
    let lon: Double
    let accuracy: Double
    let ts: Int64
}

/// A walker the backend found inside the proximity radius.
///
/// Deliberately carries no position: the server sends a distance and a bearing
/// from us, both snapped to a coarse grid, and the map resolves them against our
/// own fix. That is what stops the app being a way to read strangers' addresses.
struct NearbyWalkerFrame: Decodable {
    let id: String
    let name: String?
    let distanceMeters: Double?
    let bearingDegrees: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case distanceMeters = "distance_m"
        case bearingDegrees = "bearing_deg"
    }
}

/// Server -> client. `type` is the discriminator: welcome / PROXIMITY_ALERT /
/// PROXIMITY_CLEAR / error.
struct ServerFrame: Decodable {
    let type: String
    let radiusMeters: Double?
    let walkers: [NearbyWalkerFrame]?
    let message: String?
    let walkerId: String?

    enum CodingKeys: String, CodingKey {
        case type
        case walkers
        case message
        case radiusMeters = "radius_m"
        case walkerId = "walker_id"
    }
}

enum FrameType {
    static let welcome = "welcome"
    static let proximityAlert = "PROXIMITY_ALERT"
    static let proximityClear = "PROXIMITY_CLEAR"
    static let error = "error"
}
