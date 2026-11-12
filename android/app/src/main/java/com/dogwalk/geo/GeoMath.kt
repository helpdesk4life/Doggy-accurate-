package com.dogwalk.geo

import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin

object GeoMath {

    private const val EARTH_RADIUS_METERS = 6_371_008.8

    /**
     * Great-circle destination from [latitude]/[longitude] travelling [distanceMeters]
     * along [bearingDegrees]. Returns (latitude, longitude).
     */
    fun destination(
        latitude: Double,
        longitude: Double,
        bearingDegrees: Double,
        distanceMeters: Double,
    ): Pair<Double, Double> {
        val angular = distanceMeters / EARTH_RADIUS_METERS
        val bearing = Math.toRadians(bearingDegrees)
        val lat1 = Math.toRadians(latitude)
        val lon1 = Math.toRadians(longitude)

        val lat2 = asin(sin(lat1) * cos(angular) + cos(lat1) * sin(angular) * cos(bearing))
        val lon2 = lon1 + atan2(
            sin(bearing) * sin(angular) * cos(lat1),
            cos(angular) - sin(lat1) * sin(lat2),
        )

        return Math.toDegrees(lat2) to Math.toDegrees(lon2)
    }

    /** Closed ring approximating a circle of [radiusMeters] around the given point. */
    fun circle(
        latitude: Double,
        longitude: Double,
        radiusMeters: Double,
        segments: Int = 64,
    ): List<Pair<Double, Double>> = (0 until segments).map { index ->
        destination(latitude, longitude, index * 360.0 / segments, radiusMeters)
    }
}
