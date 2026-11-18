package com.mikywalk.model

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
 * One flow rather than a change flow plus a separate clear flow: the alert
 * occupies a single notification slot, so a withdrawal that overtook the change
 * replacing it would silently swallow a real alert.
 */
sealed interface ProximityEvent {
    /**
     * The set inside the warning distance changed and is no longer empty.
     *
     * Carries everyone inside, not just the ones that just arrived: the alert
     * names the most urgent walker *present*, so it has to be rebuilt from the
     * whole set. Whether that rebuild is worth a sound is decided from what the
     * user was last told about, not from the event type.
     */
    data class Changed(val walkers: List<NearbyWalker>) : ProximityEvent

    /** Nobody is inside the warning distance any more, so the alert is stale. */
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

/**
 * A walker this user has blocked.
 *
 * Unlike [MarkedDog] this is server state, not device state: the backend keeps
 * the list so the block survives a reinstall, and so it can stop sending either
 * walker's bearing to the other. The name is captured when the block is made,
 * because a blocked walker is usually not online when settings is opened.
 */
@Serializable
data class BlockedWalker(
    val id: String,
    val name: String,
)

/** A dog met during a walk, remembered by name so the history can say who it was. */
@Serializable
data class MetDog(
    val id: String,
    val name: String,
)

/**
 * One finished walk: when it started and ended, how far it went and which dogs
 * came within the warning distance.
 *
 * Aggregates only, on purpose: no route and no coordinates are kept, so the
 * history file is not a copy of the track the privacy design keeps off the wire
 * in the first place. It lives in local storage and does not survive a reinstall,
 * the same trade the marked-dogs list already makes.
 */
@Serializable
data class WalkSummary(
    val id: String,
    val startedAtMillis: Long,
    val endedAtMillis: Long,
    val distanceMeters: Double,
    val dogsMet: List<MetDog>,
) {

    val durationMillis: Long get() = endedAtMillis - startedAtMillis
}
