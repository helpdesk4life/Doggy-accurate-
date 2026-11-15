package com.dogwalk.data.history

import com.dogwalk.geo.GeoMath
import com.dogwalk.model.NearbyWalker
import com.dogwalk.model.WalkerLocation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The distance and dogs-met rules of [WalkStatsRecorder], on the JVM.
 *
 * Distances are generated with [GeoMath.destination], so a "100 m step" is
 * exactly 100 m by the same great-circle maths the recorder measures with.
 */
class WalkStatsRecorderTest {

    private val origin = WalkerLocation(
        latitude = 52.0907,
        longitude = 5.1214,
        accuracyMeters = 8f,
        timestampMillis = 1_000,
    )

    private fun fixAt(bearingDegrees: Double, meters: Double, accuracy: Float = 8f) =
        GeoMath.destination(origin.latitude, origin.longitude, bearingDegrees, meters)
            .let { (lat, lon) -> WalkerLocation(lat, lon, accuracy, 0) }

    private fun nearby(id: String) = NearbyWalker(
        id = id,
        name = id.replaceFirstChar { it.uppercase() },
        bearingDegrees = 90.0,
        distanceMeters = 80.0,
    )

    @Test
    fun `finish before start returns null`() {
        val recorder = WalkStatsRecorder()
        assertNull(recorder.finish(nowMillis = 5_000))
    }

    @Test
    fun `a walk with a single fix records zero distance`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onLocation(origin)
        val summary = recorder.finish(60_000)

        assertEquals(0.0, summary!!.distanceMeters, 0.0)
        assertEquals(1_000L, summary.startedAtMillis)
        assertEquals(60_000L, summary.endedAtMillis)
        assertEquals(59_000L, summary.durationMillis)
        assertTrue(summary.dogsMet.isEmpty())
        assertTrue(summary.id.isNotBlank())
    }

    @Test
    fun `a known hundred meter step accumulates about a hundred meters`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onLocation(origin)
        recorder.onLocation(fixAt(bearingDegrees = 0.0, meters = 100.0))

        val summary = recorder.finish(10_000)
        assertEquals(100.0, summary!!.distanceMeters, 0.5)
    }

    @Test
    fun `several steps add up along the track`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onLocation(origin)
        recorder.onLocation(fixAt(bearingDegrees = 0.0, meters = 100.0))
        // From the 100 m point, another 100 m east is not another 100 m from the
        // origin; the recorder must chain fixes, not measure from the first one.
        val second = fixAt(bearingDegrees = 0.0, meters = 100.0)
        val third = GeoMath.destination(second.latitude, second.longitude, 90.0, 100.0)
            .let { (lat, lon) -> WalkerLocation(lat, lon, 8f, 0) }
        recorder.onLocation(third)

        val summary = recorder.finish(20_000)
        assertEquals(200.0, summary!!.distanceMeters, 1.0)
    }

    @Test
    fun `jitter below the accuracy radius is not counted and the reference point stays put`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onLocation(origin)
        // 2 m away, but the fix claims 10 m accuracy: indistinguishable from noise.
        recorder.onLocation(fixAt(bearingDegrees = 0.0, meters = 2.0, accuracy = 10f))
        assertEquals(0.0, recorder.finish(10_000)!!.distanceMeters, 0.0)

        // A later fix 12 m from the origin crosses the deadband and is measured
        // from the origin, not from the jittered fix, so it counts once.
        val recorder2 = WalkStatsRecorder()
        recorder2.start(1_000)
        recorder2.onLocation(origin)
        recorder2.onLocation(fixAt(bearingDegrees = 0.0, meters = 2.0, accuracy = 10f))
        recorder2.onLocation(fixAt(bearingDegrees = 0.0, meters = 12.0, accuracy = 10f))
        assertEquals(12.0, recorder2.finish(20_000)!!.distanceMeters, 0.5)
    }

    @Test
    fun `an oversized glitch is dropped and does not poison the next step`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onLocation(origin)
        // A fix 1 km away in one 5 s interval is a glitch, not a walk.
        recorder.onLocation(fixAt(bearingDegrees = 0.0, meters = 1_000.0))
        // The next honest fix is measured from the origin, so the jump back is
        // not counted either.
        recorder.onLocation(fixAt(bearingDegrees = 0.0, meters = 100.0))

        val summary = recorder.finish(20_000)
        assertEquals(100.0, summary!!.distanceMeters, 0.5)
    }

    @Test
    fun `dogs met are distinct by id, keep the first name and stay in arrival order`() {
        val recorder = WalkStatsRecorder()
        recorder.start(1_000)
        recorder.onDogsMet(listOf(nearby("buddy"), nearby("rex")))
        recorder.onDogsMet(listOf(nearby("buddy"), nearby("mia")))

        val dogs = recorder.finish(30_000)!!.dogsMet
        assertEquals(listOf("buddy", "rex", "mia"), dogs.map { it.id })
        assertEquals("Buddy", dogs.first().name)
    }

    @Test
    fun `each finished walk gets a fresh id`() {
        val a = WalkStatsRecorder().run { start(1_000); finish(2_000) }!!
        val b = WalkStatsRecorder().run { start(1_000); finish(2_000) }!!
        assertNotEquals(a.id, b.id)
    }
}
