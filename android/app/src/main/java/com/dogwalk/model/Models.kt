package com.dogwalk.model

import kotlinx.serialization.Serializable

data class WalkerLocation(
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Float,
    val timestampMillis: Long,
)

/**
 * Another walker, described relative to us.
 *
 * There is no latitude or longitude here on purpose: the backend never sends
 * one. [bearingDegrees] and [distanceMeters] are enough to place a marker, and
 * both are quantised server-side, so the marker lands on the right street rather
 * than the right doorstep.
 */
data class NearbyWalker(
    val id: String,
    val name: String,
    val bearingDegrees: Double,
    val distanceMeters: Double,
)

/**
 * What just happened to the set of walkers inside the warning distance.
 *
 * One flow rather than an arrival flow plus a separate clear flow: the alert
 * occupies a single notification slot, so a withdrawal that overtook the arrival
 * replacing it would silently swallow a real alert.
 */
sealed interface ProximityEvent {
    /** Walkers that just came inside the warning distance. */
    data class Arrived(val walkers: List<NearbyWalker>) : ProximityEvent

    /** The last walker inside the warning distance left, so the alert is stale. */
    data object Cleared : ProximityEvent
}

enum class ConnectionState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
}

/**
 * How this dog feels about another one.
 *
 * Purely a local judgement: the server never learns it, and never sends one. It
 * only changes how a neighbour is drawn and which sound the phone makes, so
 * keeping it on the device costs nothing and keeps the social graph out of Redis.
 */
enum class DogRelationship {
    /** A dog yours likes. Green marker, friendly chime. */
    FRIEND,

    /** A dog yours does not like. Red marker, warning sound. */
    DISLIKED,

    /** Not marked either way. Amber marker, the default notification sound. */
    UNKNOWN,
}

/** A dog the user has marked, remembered by name so settings can list it later. */
@Serializable
data class MarkedDog(
    val id: String,
    val name: String,
    val relationship: DogRelationship,
)
