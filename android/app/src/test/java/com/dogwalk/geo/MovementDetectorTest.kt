package com.dogwalk.geo

import com.dogwalk.model.WalkerLocation
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The stationary/moving rules in [MovementDetector].
 *
 * The thresholds are the whole point of the class, so the tests are written in
 * metres rather than in fixes: a walking step at 5 s is about 7 m, and the
 * stationary radius is 12 m, which is what keeps a walker who is actually walking
 * out of the stationary branch.
 */
class MovementDetectorTest {

    private val origin = WalkerLocation(
        latitude = 52.0907,
        longitude = 5.1214,
        accuracyMeters = 8f,
        timestampMillis = 0,
    )

    /** A fix [meters] north of the origin, so the distance from it is exact. */
    private fun fix(meters: Double): WalkerLocation {
        val (latitude, longitude) = GeoMath.destination(
            origin.latitude,
            origin.longitude,
            0.0,
            meters,
        )
        return WalkerLocation(latitude, longitude, 8f, 0)
    }

    @Test
    fun `the first fix is moving`() {
        assertEquals(Cadence.MOVING, MovementDetector().onFix(origin))
    }

    @Test
    fun `three fixes inside the stationary radius are standing still`() {
        val detector = MovementDetector()

        assertEquals(Cadence.MOVING, detector.onFix(fix(0.0)))
        assertEquals(Cadence.MOVING, detector.onFix(fix(4.0)))
        assertEquals(Cadence.MOVING, detector.onFix(fix(7.0)))
        assertEquals(Cadence.STATIONARY, detector.onFix(fix(9.0)))
    }

    @Test
    fun `a walker who keeps walking never goes stationary`() {
        val detector = MovementDetector()

        // 7 m per fix is a walking step at 5 s. The anchor resets on every step
        // that leaves the radius, so the still count never reaches three.
        val verdicts = (0..20).map { detector.onFix(fix(it * 7.0)) }

        assertEquals(listOf(Cadence.MOVING), verdicts.distinct())
    }

    @Test
    fun `a stationary walker who walks away is moving again`() {
        val detector = MovementDetector()
        (0..3).forEach { detector.onFix(fix(it * 3.0)) }
        assertEquals(Cadence.STATIONARY, detector.cadence)

        // The moving radius is 30 m from where they stopped.
        assertEquals(Cadence.MOVING, detector.onFix(fix(31.0)))
    }

    @Test
    fun `noise inside the moving radius does not flip a stationary walker back`() {
        val detector = MovementDetector()
        (0..3).forEach { detector.onFix(fix(it * 3.0)) }
        assertEquals(Cadence.STATIONARY, detector.cadence)

        assertEquals(Cadence.STATIONARY, detector.onFix(fix(20.0)))
        assertEquals(Cadence.STATIONARY, detector.onFix(fix(5.0)))
    }

    @Test
    fun `reset returns the detector to moving`() {
        val detector = MovementDetector()
        (0..3).forEach { detector.onFix(fix(it * 3.0)) }
        assertEquals(Cadence.STATIONARY, detector.cadence)

        detector.reset()

        assertEquals(Cadence.MOVING, detector.cadence)
        assertEquals(Cadence.MOVING, detector.onFix(fix(0.0)))
    }
}
