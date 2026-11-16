package com.dogwalk.data.repository

import com.dogwalk.data.WalkSettings
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.auth.TokenRefresher
import com.dogwalk.data.history.WalkHistoryStore
import com.dogwalk.data.history.WalkStatsRecorder
import com.dogwalk.data.remote.FrameTypes
import com.dogwalk.data.remote.ServerFrame
import com.dogwalk.data.remote.WalkTransport
import com.dogwalk.di.ApplicationScope
import com.dogwalk.model.ConnectionState
import com.dogwalk.model.NearbyWalker
import com.dogwalk.model.ProximityEvent
import com.dogwalk.model.WalkerLocation
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Single source of truth for the walk session. The foreground service pushes GPS fixes
 * in, the socket pushes proximity frames back, and the UI observes the resulting state.
 *
 * Also owns session renewal: when the socket reports an expired access token it
 * exchanges the refresh token for a new pair and reconnects. If the refresh token
 * is dead too, the session is cleared and the UI falls back to the login screen.
 */
@Singleton
class WalkRepository @Inject constructor(
    private val socket: WalkTransport,
    private val settings: WalkSettings,
    private val authStore: AuthStore,
    private val authApi: TokenRefresher,
    private val history: WalkHistoryStore,
    @param:ApplicationScope private val scope: CoroutineScope,
) {
    private val _isTracking = MutableStateFlow(false)
    val isTracking: StateFlow<Boolean> = _isTracking.asStateFlow()

    private val _self = MutableStateFlow<WalkerLocation?>(null)
    val self: StateFlow<WalkerLocation?> = _self.asStateFlow()

    private val _nearby = MutableStateFlow<List<NearbyWalker>>(emptyList())
    val nearby: StateFlow<List<NearbyWalker>> = _nearby.asStateFlow()

    private val _radiusMeters = MutableStateFlow(DEFAULT_RADIUS_METERS)
    val radiusMeters: StateFlow<Double> = _radiusMeters.asStateFlow()

    /**
     * Emits when walkers arrive inside the warning distance, when the set changes
     * without a new arrival so the service can rebuild the alert's text, and when
     * the last one leaves so it can withdraw the alert it posted.
     */
    private val _proximityEvents = MutableSharedFlow<ProximityEvent>(extraBufferCapacity = 16)
    val proximityEvents: SharedFlow<ProximityEvent> = _proximityEvents.asSharedFlow()

    val connection: StateFlow<ConnectionState> = socket.state

    private var frameJob: Job? = null
    private var authJob: Job? = null

    /** Walkers already alerted on, so a walker standing still does not buzz every fix. */
    private var alertedIds: Set<String> = emptySet()

    /** Stats of the walk in progress; null when not tracking. */
    private var recorder: WalkStatsRecorder? = null

    fun startTracking() {
        if (_isTracking.value) return
        _isTracking.value = true
        recorder = WalkStatsRecorder().apply { start(System.currentTimeMillis()) }
        socket.connect(
            endpoint = settings.backendUrl.value,
            displayName = settings.displayName.value,
        )
        frameJob?.cancel()
        frameJob = scope.launch {
            socket.frames.collect(::handleFrame)
        }
        authJob?.cancel()
        authJob = scope.launch {
            socket.authExpired.collect { renewSession() }
        }
    }

    fun stopTracking() {
        finishWalk()
        _isTracking.value = false
        frameJob?.cancel()
        frameJob = null
        authJob?.cancel()
        authJob = null
        socket.disconnect()
        _nearby.value = emptyList()
        _self.value = null
        alertedIds = emptySet()
    }

    /**
     * Persist the finished walk, whether it ended because the user stopped it or
     * because the session died mid-walk. Both paths funnel through here.
     */
    private fun finishWalk() {
        recorder?.let { recorder ->
            recorder.finish(System.currentTimeMillis())?.let(history::add)
        }
        recorder = null
    }

    fun publishLocation(location: WalkerLocation) {
        _self.value = location
        recorder?.onLocation(location)
        socket.sendLocation(
            latitude = location.latitude,
            longitude = location.longitude,
            accuracyMeters = location.accuracyMeters,
            timestampMillis = location.timestampMillis,
        )
    }

    private suspend fun renewSession() {
        val refresh = authStore.refreshToken()
        if (refresh == null) {
            signOut()
            return
        }

        val renewed = runCatching { authApi.refresh(refresh) }.getOrNull()
        if (renewed == null) {
            signOut()
            return
        }

        authStore.updateTokens(renewed)
        socket.reconnect()
    }

    /**
     * Tear the walk down and drop the session. Deliberately does not cancel
     * [authJob]: this runs inside that coroutine, and cancelling it here would
     * abort before the session is cleared.
     */
    private fun signOut() {
        finishWalk()
        _isTracking.value = false
        frameJob?.cancel()
        frameJob = null
        socket.disconnect()
        _nearby.value = emptyList()
        _self.value = null
        alertedIds = emptySet()
        authStore.clear()
    }

    private fun handleFrame(frame: ServerFrame) {
        when (frame.type) {
            FrameTypes.PROXIMITY_ALERT -> {
                frame.radiusMeters?.let { _radiusMeters.value = it }
                val walkers = frame.walkers.map { dto ->
                    NearbyWalker(
                        id = dto.id,
                        name = dto.name,
                        bearingDegrees = dto.bearingDegrees,
                        distanceMeters = dto.distanceMeters,
                    )
                }
                _nearby.value = walkers

                // Everyone inside the server radius stays on the map; only walkers inside
                // the user's warning distance raise an alert, and only on entry.
                val warningDistance = settings.warningDistanceMeters.value
                val withinWarning = walkers.filter { it.distanceMeters <= warningDistance }
                val ids = withinWarning.map { it.id }.toSet()
                if (ids != alertedIds) {
                    // Only the ones that just arrived. Re-alerting about a dog that
                    // was already inside would replay its sound every time somebody
                    // else appeared, which is especially wrong now that the sound
                    // depends on who it is.
                    val arrived = withinWarning.filter { it.id !in alertedIds }
                    val emptied = ids.isEmpty() && alertedIds.isNotEmpty()
                    alertedIds = ids
                    when {
                        arrived.isNotEmpty() -> {
                            recorder?.onDogsMet(arrived)
                            _proximityEvents.tryEmit(ProximityEvent.Arrived(arrived))
                        }

                        // The last one left. Withdraw the alert rather than leave it
                        // in the shade claiming a dog is nearby when it is not.
                        emptied -> _proximityEvents.tryEmit(ProximityEvent.Cleared)

                        // Someone left while others stayed. The alert still applies,
                        // but it names the walker that left, so its text has to be
                        // rebuilt from the ones still inside — without sounding again.
                        else -> _proximityEvents.tryEmit(ProximityEvent.Updated(withinWarning))
                    }
                }
            }

            FrameTypes.PROXIMITY_CLEAR -> {
                _nearby.value = emptyList()
                val wasAlerting = alertedIds.isNotEmpty()
                alertedIds = emptySet()
                if (wasAlerting) _proximityEvents.tryEmit(ProximityEvent.Cleared)
            }

            FrameTypes.WELCOME -> frame.radiusMeters?.let { _radiusMeters.value = it }
        }
    }

    companion object {
        const val DEFAULT_RADIUS_METERS = 500.0
    }
}
