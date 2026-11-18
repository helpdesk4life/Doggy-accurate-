package com.mikywalk.service

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.Build
import android.os.Looper
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import com.mikywalk.data.SettingsStore
import com.mikywalk.data.auth.AuthState
import com.mikywalk.data.auth.AuthStore
import com.mikywalk.data.repository.WalkRepository
import com.mikywalk.geo.Cadence
import com.mikywalk.geo.MovementDetector
import com.mikywalk.model.ProximityEvent
import com.mikywalk.model.WalkerLocation
import com.mikywalk.notifications.NotificationHelper
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import javax.inject.Inject
import kotlin.coroutines.resume

/**
 * Foreground service that owns the GPS subscription for the duration of a walk.
 *
 * It pushes every fix into [WalkRepository] (which forwards it over the WebSocket) and
 * turns proximity frames into local notifications, so alerts still arrive with the UI
 * in the background.
 */
@AndroidEntryPoint
class LocationTrackingService : LifecycleService() {

    @Inject
    lateinit var repository: WalkRepository

    @Inject
    lateinit var notifications: NotificationHelper

    @Inject
    lateinit var authStore: AuthStore

    @Inject
    lateinit var settings: SettingsStore

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var updatesRunning = false
    private var alertJob: Job? = null
    private var authJob: Job? = null
    private var probeJob: Job? = null
    private val detector = MovementDetector()
    private var cadence = Cadence.MOVING

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val location = result.lastLocation ?: return
            publish(location)
        }
    }

    override fun onCreate() {
        super.onCreate()
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        notifications.createChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }

        if (!hasLocationPermission()) {
            stopSelf()
            return START_NOT_STICKY
        }

        startForegroundCompat()
        repository.startTracking()
        detector.reset()
        cadence = Cadence.MOVING
        startContinuousUpdates()
        observeProximity()
        observeAuth()
        return START_STICKY
    }

    override fun onDestroy() {
        alertJob?.cancel()
        alertJob = null
        authJob?.cancel()
        authJob = null
        stopProbes()
        stopContinuousUpdates()
        repository.stopTracking()
        // The walk is over, so an alert about a dog that was nearby is stale. The
        // collector above is already cancelled by this point, which is why this is
        // explicit rather than another ProximityEvent.
        notifications.cancelProximity()
        super.onDestroy()
    }

    private fun startForegroundCompat() {
        val notification = notifications.trackingNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    /**
     * Push one fix and let the detector decide how often the next one is needed.
     *
     * Every fix goes out either way. The server drops a walker whose last fix is
     * older than `WALKER_TTL_SECONDS` (30 s), so going quiet is not an option even
     * when the position is not changing.
     */
    private fun publish(location: Location) {
        val fix = WalkerLocation(
            latitude = location.latitude,
            longitude = location.longitude,
            accuracyMeters = location.accuracy,
            timestampMillis = location.time,
        )
        repository.publishLocation(fix)
        applyCadence(detector.onFix(fix))
    }

    private fun applyCadence(next: Cadence) {
        if (next == cadence) return
        cadence = next
        when (next) {
            Cadence.MOVING -> startContinuousUpdates()
            Cadence.STATIONARY -> startProbes()
        }
    }

    /**
     * Hold the GPS open and take a fix every [MOVING_INTERVAL_MS].
     *
     * `PRIORITY_HIGH_ACCURACY` is not negotiable here: a balanced fix is accurate to
     * roughly 100 m, which is the whole warning distance, so it would replace a good
     * position with a useless one. The saving comes from the rate, not the accuracy.
     */
    private fun startContinuousUpdates() {
        stopProbes()
        if (updatesRunning) return

        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, MOVING_INTERVAL_MS)
            .setMinUpdateIntervalMillis(MOVING_INTERVAL_MS)
            .setMaxUpdateDelayMillis(MOVING_INTERVAL_MS)
            .setWaitForAccurateLocation(false)
            .build()

        try {
            fusedLocationClient.requestLocationUpdates(
                request,
                locationCallback,
                Looper.getMainLooper(),
            )
            updatesRunning = true
        } catch (_: SecurityException) {
            stopSelf()
        }
    }

    private fun stopContinuousUpdates() {
        if (!updatesRunning) return
        fusedLocationClient.removeLocationUpdates(locationCallback)
        updatesRunning = false
    }

    /**
     * Standing still: ask for one fix every [STATIONARY_PROBE_INTERVAL_MS] instead of
     * holding the radio open, so the GPS sleeps between probes.
     *
     * The interval is a third of the server's 30 s walker TTL, so a walker who stops
     * for ten minutes still refreshes their position in Redis and stays on everyone
     * else's map.
     */
    private fun startProbes() {
        stopContinuousUpdates()
        if (probeJob?.isActive == true) return
        probeJob = lifecycleScope.launch {
            while (isActive) {
                delay(STATIONARY_PROBE_INTERVAL_MS)
                // A probe that throws must not end the loop. Unlike a framework
                // callback, an exception here kills the coroutine for good, and the
                // walker would then stop refreshing and be swept out of Redis 30 s
                // later — silently, with the walk still running.
                try {
                    probeOnce()
                } catch (cancellation: CancellationException) {
                    throw cancellation
                } catch (_: Exception) {
                    // Keep probing; the next one may succeed.
                }
            }
        }
    }

    private fun stopProbes() {
        probeJob?.cancel()
        probeJob = null
    }

    private suspend fun probeOnce() {
        val fix = currentFix()
        if (fix != null) {
            publish(fix)
            return
        }
        // No fix available. Re-send the last known position rather than let the
        // walker age out of Redis and vanish from everyone else's map.
        repository.self.value?.let(repository::publishLocation)
    }

    /** One high-accuracy fix, or null when the provider has nothing to give. */
    private suspend fun currentFix(): Location? = suspendCancellableCoroutine { continuation ->
        val token = CancellationTokenSource()
        continuation.invokeOnCancellation { token.cancel() }
        val task = try {
            fusedLocationClient.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, token.token)
        } catch (_: SecurityException) {
            // Permission revoked mid-walk. The probe falls back to the last fix.
            null
        }
        if (task == null) {
            continuation.resume(null)
        } else {
            task.addOnCompleteListener { finished ->
                if (!continuation.isActive) return@addOnCompleteListener
                continuation.resume(if (finished.isSuccessful) finished.result else null)
            }
        }
    }

    private fun observeProximity() {
        if (alertJob != null) return
        alertJob = lifecycleScope.launch {
            repository.proximityEvents.collect { event ->
                when (event) {
                    // Read the marks at post time rather than caching them, so a
                    // dog marked mid-walk changes the sound on the very next post.
                    is ProximityEvent.Changed ->
                        notifications.notifyProximity(event.walkers, settings.markedDogs.value)

                    ProximityEvent.Cleared -> notifications.cancelProximity()
                }
            }
        }
    }

    /**
     * A session that dies mid-walk (refresh token revoked, signed out elsewhere)
     * must not leave a foreground service running with no account behind it.
     */
    private fun observeAuth() {
        if (authJob != null) return
        authJob = lifecycleScope.launch {
            authStore.state.collect { state ->
                if (state is AuthState.SignedOut) stopSelf()
            }
        }
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_FINE_LOCATION,
        ) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.ACCESS_COARSE_LOCATION,
            ) == PackageManager.PERMISSION_GRANTED

    companion object {
        const val ACTION_STOP = "com.mikywalk.action.STOP_TRACKING"

        private const val NOTIFICATION_ID = 1001

        /** While moving: the cadence the walk has always used. */
        private const val MOVING_INTERVAL_MS = 5_000L

        /**
         * While standing still. A third of the server's 30 s walker TTL, so three
         * probes can be missed before the walker is swept.
         */
        private const val STATIONARY_PROBE_INTERVAL_MS = 10_000L

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, LocationTrackingService::class.java),
            )
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LocationTrackingService::class.java))
        }
    }
}
