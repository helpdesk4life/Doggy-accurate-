package com.dogwalk.service

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Looper
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import com.dogwalk.data.SettingsStore
import com.dogwalk.data.auth.AuthState
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.repository.WalkRepository
import com.dogwalk.model.WalkerLocation
import com.dogwalk.notifications.NotificationHelper
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import javax.inject.Inject

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

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val location = result.lastLocation ?: return
            repository.publishLocation(
                WalkerLocation(
                    latitude = location.latitude,
                    longitude = location.longitude,
                    accuracyMeters = location.accuracy,
                    timestampMillis = location.time,
                ),
            )
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
        startLocationUpdates()
        observeProximity()
        observeAuth()
        return START_STICKY
    }

    override fun onDestroy() {
        alertJob?.cancel()
        alertJob = null
        authJob?.cancel()
        authJob = null
        if (updatesRunning) {
            fusedLocationClient.removeLocationUpdates(locationCallback)
            updatesRunning = false
        }
        repository.stopTracking()
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

    private fun startLocationUpdates() {
        if (updatesRunning) return

        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, UPDATE_INTERVAL_MS)
            .setMinUpdateIntervalMillis(UPDATE_INTERVAL_MS)
            .setMaxUpdateDelayMillis(UPDATE_INTERVAL_MS)
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

    private fun observeProximity() {
        if (alertJob != null) return
        alertJob = lifecycleScope.launch {
            repository.proximityEvents.collect { walkers ->
                // Read the marks at alert time rather than caching them, so a dog
                // marked mid-walk changes the sound on the very next alert.
                notifications.notifyProximity(walkers, settings.markedDogs.value)
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
        const val ACTION_STOP = "com.dogwalk.action.STOP_TRACKING"

        private const val NOTIFICATION_ID = 1001
        private const val UPDATE_INTERVAL_MS = 5_000L

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
