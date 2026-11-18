package com.mikywalk.notifications

import com.mikywalk.model.DogRelationship
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The sound rule in [ProximityAlertPolicy].
 *
 * The cases are written as "what is on screen, what just became the most urgent
 * walker present, should it sound", because that is the whole rule.
 */
class ProximityAlertPolicyTest {

    @Test
    fun `nothing on screen always sounds`() {
        DogRelationship.entries.forEach { relationship ->
            assertTrue(
                "$relationship should sound when nothing is showing",
                ProximityAlertPolicy.alertsOn(relationship, showing = null),
            )
        }
    }

    @Test
    fun `a friend arriving while a disliked dog is inside does not sound`() {
        assertFalse(
            ProximityAlertPolicy.alertsOn(DogRelationship.FRIEND, DogRelationship.DISLIKED),
        )
    }

    @Test
    fun `a disliked dog arriving while a friend is inside sounds`() {
        assertTrue(
            ProximityAlertPolicy.alertsOn(DogRelationship.DISLIKED, DogRelationship.FRIEND),
        )
    }

    @Test
    fun `a stranger arriving while a friend is inside sounds`() {
        assertTrue(
            ProximityAlertPolicy.alertsOn(DogRelationship.UNKNOWN, DogRelationship.FRIEND),
        )
    }

    @Test
    fun `a friend arriving while a stranger is inside does not sound`() {
        assertFalse(
            ProximityAlertPolicy.alertsOn(DogRelationship.FRIEND, DogRelationship.UNKNOWN),
        )
    }

    @Test
    fun `a second disliked dog does not replay the alarm`() {
        assertFalse(
            ProximityAlertPolicy.alertsOn(DogRelationship.DISLIKED, DogRelationship.DISLIKED),
        )
    }

    @Test
    fun `a refresh after the most urgent walker leaves does not sound`() {
        // The disliked dog walked away and a friend is still inside: the text has to
        // change, but the news is better, not worse.
        assertFalse(
            ProximityAlertPolicy.alertsOn(DogRelationship.FRIEND, DogRelationship.DISLIKED),
        )
    }
}
