package com.mikywalk.notifications

import com.mikywalk.model.DogRelationship

/**
 * When a proximity alert is allowed to make a sound.
 *
 * The alert has two jobs and they are not the same job: it says who is nearby, and
 * it tells you that something changed. The text always names the most urgent walker
 * *present*, so it is rebuilt whenever the set changes. The sound is only worth
 * making when the news is actually worse than what is already on screen.
 *
 * Without that split, a friend arriving while a disliked dog is inside replaces the
 * warning with a chime — the wrong way round — and re-posting the warning instead
 * replays the alarm for a dog that was already there.
 *
 * The rule is one line: sound when the most urgent walker present is more urgent
 * than the one already showing, or when nothing is showing. That is airtight in
 * both directions. If the most urgent walker present is now more urgent than it was
 * at the last post, then a walker of that relationship must have arrived, because
 * otherwise the last post would already have been about them.
 *
 * Pure, so the rule is pinned on the JVM rather than on a device.
 */
internal object ProximityAlertPolicy {

    /** Lower is more urgent. */
    fun urgency(relationship: DogRelationship): Int = when (relationship) {
        DogRelationship.DISLIKED -> 0
        DogRelationship.UNKNOWN -> 1
        DogRelationship.FRIEND -> 2
    }

    /**
     * Whether an alert about [relationship] should sound, given that [showing] is
     * the most urgent relationship in the alert currently on screen — null when
     * there is nothing on screen.
     */
    fun alertsOn(relationship: DogRelationship, showing: DogRelationship?): Boolean =
        showing == null || urgency(relationship) < urgency(showing)
}
