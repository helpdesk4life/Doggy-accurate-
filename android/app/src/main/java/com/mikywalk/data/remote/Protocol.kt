package com.mikywalk.data.remote

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Client -> server: one GPS fix. */
@Serializable
data class LocationFrame(
    val type: String = "location",
    val lat: Double,
    val lon: Double,
    val accuracy: Float,
    val ts: Long,
)

/**
 * A walker the backend found inside the proximity radius.
 *
 * Deliberately carries no position: the server sends a distance and a bearing
 * from us, both snapped to a coarse grid, and the map resolves them against our
 * own fix. That is what stops the app being a way to read strangers' addresses.
 */
@Serializable
data class NearbyWalkerFrame(
    val id: String,
    val name: String = "",
    @SerialName("distance_m") val distanceMeters: Double = 0.0,
    @SerialName("bearing_deg") val bearingDegrees: Double = 0.0,
)

/** Server -> client. `type` is the discriminator: welcome / PROXIMITY_ALERT / PROXIMITY_CLEAR / error. */
@Serializable
data class ServerFrame(
    val type: String,
    @SerialName("radius_m") val radiusMeters: Double? = null,
    val walkers: List<NearbyWalkerFrame> = emptyList(),
    val message: String? = null,
    @SerialName("walker_id") val walkerId: String? = null,
)

object FrameTypes {
    const val WELCOME = "welcome"
    const val PROXIMITY_ALERT = "PROXIMITY_ALERT"
    const val PROXIMITY_CLEAR = "PROXIMITY_CLEAR"
    const val ERROR = "error"
}
