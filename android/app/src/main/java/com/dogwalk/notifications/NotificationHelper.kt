package com.dogwalk.notifications

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.dogwalk.MainActivity
import com.dogwalk.R
import com.dogwalk.model.DogRelationship
import com.dogwalk.model.MarkedDog
import com.dogwalk.model.NearbyWalker
import com.dogwalk.service.LocationTrackingService
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.math.roundToInt

/**
 * Proximity alerts, one channel per relationship.
 *
 * Android fixes a channel's sound at creation and ignores later changes, so
 * "a different sound for dogs yours does not like" has to be three channels
 * rather than one channel with a variable sound. The sounds are the system
 * notification and alarm tones: no audio assets to ship, and they are already
 * distinct to the user's ear.
 */
@Singleton
class NotificationHelper @Inject constructor(
    @param:ApplicationContext private val context: Context,
) {
    private val manager = NotificationManagerCompat.from(context)

    fun createChannels() {
        val tracking = NotificationChannel(
            CHANNEL_TRACKING,
            context.getString(R.string.notification_channel_tracking),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = context.getString(R.string.notification_channel_tracking_desc)
            setShowBadge(false)
        }

        val neutral = proximityChannel(
            id = CHANNEL_PROXIMITY,
            name = R.string.notification_channel_proximity,
            description = R.string.notification_channel_proximity_desc,
            sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
            pattern = NEUTRAL_VIBRATION,
        )
        val friend = proximityChannel(
            id = CHANNEL_FRIEND,
            name = R.string.notification_channel_friend,
            description = R.string.notification_channel_friend_desc,
            sound = rawSound(R.raw.friend_chime),
            pattern = FRIEND_VIBRATION,
        )
        val disliked = proximityChannel(
            id = CHANNEL_DISLIKED,
            name = R.string.notification_channel_disliked,
            description = R.string.notification_channel_disliked_desc,
            sound = rawSound(R.raw.disliked_alarm),
            pattern = DISLIKED_VIBRATION,
        )

        context.getSystemService(NotificationManager::class.java)
            ?.createNotificationChannels(listOf(tracking, neutral, friend, disliked))
    }

    private fun proximityChannel(
        id: String,
        name: Int,
        description: Int,
        sound: Uri,
        pattern: LongArray,
    ): NotificationChannel = NotificationChannel(
        id,
        context.getString(name),
        NotificationManager.IMPORTANCE_HIGH,
    ).apply {
        this.description = context.getString(description)
        enableVibration(true)
        vibrationPattern = pattern
        setSound(
            sound,
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build(),
        )
    }

    /** A bundled tone in `res/raw`, addressed the way the notification service can read it. */
    private fun rawSound(resId: Int): Uri =
        Uri.parse("android.resource://${context.packageName}/$resId")

    fun trackingNotification(): Notification =
        NotificationCompat.Builder(context, CHANNEL_TRACKING)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(context.getString(R.string.tracking_title))
            .setContentText(context.getString(R.string.tracking_text))
            .setContentIntent(openAppIntent())
            .addAction(0, context.getString(R.string.action_stop), stopWalkIntent())
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    /**
     * Alert about the walkers that just came inside the warning distance.
     *
     * When several relationships are present at once the most urgent one wins:
     * a dog yours does not like matters more than one it does, and a stranger
     * matters more than a friend. Otherwise a friend arriving would replace the
     * warning with a chime, which is the wrong way round.
     *
     * The POST_NOTIFICATIONS check is [canPostNotifications] below. Lint's
     * dataflow does not cross a private method, so the suppression is for the
     * analyser rather than for a missing guard.
     */
    @SuppressLint("MissingPermission")
    fun notifyProximity(walkers: List<NearbyWalker>, marked: Map<String, MarkedDog>) {
        if (walkers.isEmpty()) return
        if (!canPostNotifications()) return

        val relationship = walkers
            .groupBy { marked[it.id]?.relationship ?: DogRelationship.UNKNOWN }
            .minByOrNull { urgency(it.key) }
            ?.key
            ?: return

        val group = walkers.filter { (marked[it.id]?.relationship ?: DogRelationship.UNKNOWN) == relationship }
        val closest = group.minByOrNull { it.distanceMeters } ?: return

        val title = when {
            relationship == DogRelationship.DISLIKED ->
                context.getString(R.string.proximity_title_disliked, closest.name)
            group.size == 1 -> context.getString(R.string.proximity_title_single, closest.name)
            else -> context.getString(R.string.proximity_title_many, group.size)
        }
        val body = context.getString(
            R.string.proximity_body,
            closest.name,
            closest.distanceMeters.roundToInt(),
        )

        val notification = NotificationCompat.Builder(context, channelFor(relationship))
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setColor(ContextCompat.getColor(context, colorFor(relationship)))
            .setContentIntent(openAppIntent())
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_SOCIAL)
            .build()

        runCatching { manager.notify(PROXIMITY_NOTIFICATION_ID, notification) }
    }

    /**
     * Withdraw the proximity alert.
     *
     * [notifyProximity] posts into a single notification slot, so this is the only
     * way an alert goes away without the user tapping it — `setAutoCancel(true)`
     * covers the tap and nothing else.
     */
    fun cancelProximity() {
        runCatching { manager.cancel(PROXIMITY_NOTIFICATION_ID) }
    }

    private fun canPostNotifications(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED

    private fun openAppIntent(): PendingIntent = PendingIntent.getActivity(
        context,
        0,
        Intent(context, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun stopWalkIntent(): PendingIntent = PendingIntent.getService(
        context,
        1,
        Intent(context, LocationTrackingService::class.java)
            .setAction(LocationTrackingService.ACTION_STOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private companion object {
        const val CHANNEL_TRACKING = "dogwalk_tracking"
        const val CHANNEL_PROXIMITY = "dogwalk_proximity"
        const val CHANNEL_FRIEND = "dogwalk_proximity_friend"
        const val CHANNEL_DISLIKED = "dogwalk_proximity_disliked"
        const val PROXIMITY_NOTIFICATION_ID = 2001

        /** Short double tap: a greeting. */
        val FRIEND_VIBRATION = longArrayOf(0, 80, 90, 80)

        /** The original pattern, for a dog that is neither marked nor disliked. */
        val NEUTRAL_VIBRATION = longArrayOf(0, 250, 150, 250)

        /** Long and insistent: this is the one that should make you look. */
        val DISLIKED_VIBRATION = longArrayOf(0, 400, 120, 400, 120, 400)

        /** Lower is more urgent. */
        fun urgency(relationship: DogRelationship): Int = when (relationship) {
            DogRelationship.DISLIKED -> 0
            DogRelationship.UNKNOWN -> 1
            DogRelationship.FRIEND -> 2
        }

        fun channelFor(relationship: DogRelationship): String = when (relationship) {
            DogRelationship.DISLIKED -> CHANNEL_DISLIKED
            DogRelationship.FRIEND -> CHANNEL_FRIEND
            DogRelationship.UNKNOWN -> CHANNEL_PROXIMITY
        }

        fun colorFor(relationship: DogRelationship): Int = when (relationship) {
            DogRelationship.DISLIKED -> R.color.marker_disliked
            DogRelationship.FRIEND -> R.color.marker_friend
            DogRelationship.UNKNOWN -> R.color.marker_unknown
        }
    }
}
