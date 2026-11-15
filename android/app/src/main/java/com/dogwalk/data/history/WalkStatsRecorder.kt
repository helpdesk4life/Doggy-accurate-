package com.dogwalk.data.history

import com.dogwalk.geo.GeoMath
import com.dogwalk.model.MetDog
import com.dogwalk.model.NearbyWalker
import com.dogwalk.model.WalkSummary
import com.dogwalk.model.WalkerLocation
import java.util.UUID

/**
 * Accumulates the stats of one walk from the two things the app already sees:
 * its own GPS fixes and the walkers that arrive inside the warning distance.
 *
 * Pure bookkeeping with no Android or network dependency, so the JVM tests pin
 * the distance and dogs-met rules directly. [WalkRepository] owns one instance
 * per walk and persists the [WalkSummary] [finish] returns.
 *
 * Distance is the sum of haversine steps between consecutive fixes, with two
 * guards against GPS noise:
 *
 * - a step shorter than the better fix's accuracy radius is jitter, not movement,
 *   so it is not counted — but the reference point is not moved either, so slow
 *   real movement still accumulates until it crosses the deadband;
 * - a step longer than [MAX_STEP_METERS] is a glitch (a fix hundreds of meters
 *   off in one interval), so it is skipped without moving the reference point,
 *   which also stops the jump back from being counted.
 *
 * No coordinates are kept: only the running total and the last fix.
 */
class WalkStatsRecorder {

    private var startedAtMillis: Long? = null
    private var lastFix: WalkerLocation? = null
    private var distanceMeters = 0.0
    private val dogsMet = LinkedHashMap<String, MetDog>()

    fun start(nowMillis: Long) {
        startedAtMillis = nowMillis
        lastFix = null
        distanceMeters = 0.0
        dogsMet.clear()
    }

    fun onLocation(location: WalkerLocation) {
        val previous = lastFix
        if (previous != null) {
            val step = GeoMath.distanceMeters(
                previous.latitude,
                previous.longitude,
                location.latitude,
                location.longitude,
            )
            val noiseFloor = maxOf(
                MIN_STEP_METERS,
                minOf(previous.accuracyMeters, location.accuracyMeters).toDouble(),
            )
            if (step in noiseFloor..MAX_STEP_METERS) {
                distanceMeters += step
                lastFix = location
            } else if (step <= MAX_STEP_METERS) {
                // Jitter: not counted, and the reference point stays put so the
                // next small real move is measured from the last good fix.
            }
            // else: glitch, skip without moving the reference point.
        } else {
            lastFix = location
        }
    }

    /** Walkers that just arrived inside the warning distance; distinct by id. */
    fun onDogsMet(walkers: List<NearbyWalker>) {
        walkers.forEach { dogsMet.putIfAbsent(it.id, MetDog(id = it.id, name = it.name)) }
    }

    /** The finished walk, or null when [start] never ran. */
    fun finish(nowMillis: Long): WalkSummary? {
        val startedAt = startedAtMillis ?: return null
        return WalkSummary(
            id = UUID.randomUUID().toString(),
            startedAtMillis = startedAt,
            endedAtMillis = nowMillis,
            distanceMeters = distanceMeters,
            dogsMet = dogsMet.values.toList(),
        )
    }

    companion object {

        /** Steps shorter than this are never movement, whatever the accuracy says. */
        const val MIN_STEP_METERS = 3.0

        /**
         * A single 5 s fix cannot legitimately jump further than this for a dog
         * walker; anything beyond is a GPS glitch and is dropped.
         */
        const val MAX_STEP_METERS = 250.0
    }
}
