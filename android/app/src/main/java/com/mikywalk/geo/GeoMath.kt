package com.mikywalk.geo

import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

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

    /** Great-circle distance in meters between two points (haversine). */
    fun distanceMeters(
        latitude1: Double,
        longitude1: Double,
        latitude2: Double,
        longitude2: Double,
    ): Double {
        val lat1 = Math.toRadians(latitude1)
        val lat2 = Math.toRadians(latitude2)
        val dLat = lat2 - lat1
        val dLon = Math.toRadians(longitude2 - longitude1)

        val a = sin(dLat / 2).let { it * it } +
            cos(lat1) * cos(lat2) * sin(dLon / 2).let { it * it }

        return 2 * EARTH_RADIUS_METERS * asin(minOf(1.0, sqrt(a)))
    }
}
