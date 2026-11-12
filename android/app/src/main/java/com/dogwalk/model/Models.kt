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
