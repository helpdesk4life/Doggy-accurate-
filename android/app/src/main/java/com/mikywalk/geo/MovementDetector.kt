package com.mikywalk.geo

import com.mikywalk.model.WalkerLocation

/** How often the walk needs a fix, derived from whether the walker is moving. */
enum class Cadence {
    /** On the move: a high-accuracy fix every few seconds. */
    MOVING,

    /** Standing still: the position is not changing, so a probe every few tens of seconds. */
    STATIONARY,
}

/**
 * Decides whether the walker is moving, from the fixes the service already gets.
 *
 * Standing still is the common case on a dog walk — the dog stops to sniff, the
 * walker stops to talk — and a high-accuracy fix every 5 s through those minutes
 * keeps the GPS radio on for a position that is not changing. Dropping to a
 * periodic one-shot probe lets the radio sleep between fixes.
 *
 * Two thresholds rather than one, because a single threshold flaps: GPS noise of
 * a few metres around a stationary point would cross it every other fix, and each
 * crossing costs a request swap. [stationaryRadiusMeters] is the noise band that
 * counts as standing still; [movingRadiusMeters] is how far the walker has to get
 * from where they stopped before the walk counts as moving again.
 *
 * The anchor is deliberately not moved while the still count is running. Measuring
 * each fix against the previous one instead would call a slow walker stationary:
 * three 4 m steps are 12 m of real movement, but no single step looks like it.
 *
 * Pure, so the rules are pinned on the JVM rather than on a device.
 */
class MovementDetector(
    private val stationaryRadiusMeters: Double = DEFAULT_STATIONARY_RADIUS_METERS,
    private val movingRadiusMeters: Double = DEFAULT_MOVING_RADIUS_METERS,
    private val stationaryFixes: Int = DEFAULT_STATIONARY_FIXES,
) {
    /** The point the current verdict is measured from. */
    private var anchor: WalkerLocation? = null

    /** Consecutive fixes that stayed inside [stationaryRadiusMeters] of [anchor]. */
    private var stillFixes = 0

    var cadence: Cadence = Cadence.MOVING
        private set

    /** Feed one fix and get the cadence the service should be running at. */
    fun onFix(location: WalkerLocation): Cadence {
        val from = anchor
        if (from == null) {
            anchor = location
            return cadence
        }

        val moved = GeoMath.distanceMeters(
            from.latitude,
            from.longitude,
            location.latitude,
            location.longitude,
        )

        when (cadence) {
            Cadence.MOVING -> {
                if (moved <= stationaryRadiusMeters) {
                    stillFixes++
                    if (stillFixes >= stationaryFixes) cadence = Cadence.STATIONARY
                } else {
                    // Real movement: restart the count from here, so the window
                    // that has to stay still is always the most recent one.
                    stillFixes = 0
                    anchor = location
                }
            }

            Cadence.STATIONARY -> {
                if (moved > movingRadiusMeters) {
                    cadence = Cadence.MOVING
                    stillFixes = 0
                    anchor = location
                }
            }
        }

        return cadence
    }

    /** Forget the walk so far, for the start of a new one. */
    fun reset() {
        anchor = null
        stillFixes = 0
        cadence = Cadence.MOVING
    }

    companion object {
        /**
         * A stationary fix wanders by a few metres; this is the band that still
         * counts as standing still. Small enough that a walking step (about 7 m
         * per 5 s) leaves it within three fixes.
         */
        const val DEFAULT_STATIONARY_RADIUS_METERS = 12.0

        /** Far enough that GPS noise cannot cross it, close enough to notice a walk. */
        const val DEFAULT_MOVING_RADIUS_METERS = 30.0

        /** Three fixes at 5 s is 15 s of stillness before the radio is allowed to sleep. */
        const val DEFAULT_STATIONARY_FIXES = 3
    }
}
