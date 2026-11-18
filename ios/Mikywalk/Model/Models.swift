import Foundation

/// One GPS fix, as the app understands it.
struct WalkerLocation: Equatable {
    let latitude: Double
    let longitude: Double
    let accuracyMeters: Double
    let timestampMillis: Int64
}

/// Another walker, described relative to us.
///
/// There is no latitude or longitude here on purpose: the backend never sends
/// one. `bearingDegrees` and `distanceMeters` are enough to place a marker, and
/// both are quantised server-side, so the marker lands on the right street
/// rather than the right doorstep.
struct NearbyWalker: Identifiable, Equatable {
    let id: String
    let name: String
    let bearingDegrees: Double
    let distanceMeters: Double
}

enum ConnectionState: Equatable {
    case disconnected
    case connecting
    case connected
}

/// How this dog feels about another one.
///
/// Purely a local judgement: the server never learns it, and never sends one. It
/// only changes how a neighbour is drawn and which sound the phone makes, so
/// keeping it on the device costs nothing and keeps the social graph out of
/// Redis.
enum DogRelationship: String, Codable, CaseIterable {
    /// A dog yours likes. Green marker, friendly chime.
    case friend

    /// A dog yours does not like. Red marker, warning sound.
    case disliked

    /// Not marked either way. Amber marker, the default notification sound.
    case unknown
}

/// A dog the user has marked, remembered by name so settings can list it later.
struct MarkedDog: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let relationship: DogRelationship
}
