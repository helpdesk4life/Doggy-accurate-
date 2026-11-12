package com.dogwalk.data.remote

import android.util.Log
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.model.ConnectionState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.net.URLEncoder
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.math.min

/**
 * OkHttp WebSocket transport for the walk session.
 *
 * Owns reconnection with exponential backoff (1s -> 30s cap) so callers only have to
 * observe [state] and [frames]. All mutable fields are guarded by [lock] because
 * OkHttp delivers callbacks on its own reader thread.
 *
 * The access token travels in the `Authorization` header rather than the query
 * string, so it never appears in the server's access log. When the server closes
 * with [UNAUTHORIZED] the socket does not retry on its own: it emits
 * [authExpired] and waits for the repository to refresh the token and call
 * [reconnect]. Retrying with the same dead token would just loop.
 */
@Singleton
class WalkSocketClient @Inject constructor(
    private val json: Json,
    private val authStore: AuthStore,
) {
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lock = Any()

    private val _state = MutableStateFlow(ConnectionState.DISCONNECTED)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _frames = MutableSharedFlow<ServerFrame>(extraBufferCapacity = 128)
    val frames: SharedFlow<ServerFrame> = _frames.asSharedFlow()

    private val _authExpired = MutableSharedFlow<Unit>(extraBufferCapacity = 1)
    val authExpired: SharedFlow<Unit> = _authExpired.asSharedFlow()

    private var webSocket: WebSocket? = null
    private var endpoint: String? = null
    private var displayName: String = ""
    private var reconnectAttempt = 0
    private var reconnectJob: Job? = null
    private var stopped = true

    fun connect(endpoint: String, displayName: String) {
        synchronized(lock) {
            this.endpoint = endpoint
            this.displayName = displayName
            stopped = false
            reconnectAttempt = 0
            reconnectJob?.cancel()
            reconnectJob = null
            openLocked()
        }
    }

    fun disconnect() {
        synchronized(lock) {
            stopped = true
            reconnectJob?.cancel()
            reconnectJob = null
            webSocket?.close(NORMAL_CLOSURE, "walk finished")
            webSocket = null
            _state.value = ConnectionState.DISCONNECTED
        }
    }

    /** Re-open after the caller has refreshed the access token. */
    fun reconnect() {
        synchronized(lock) {
            if (stopped) return
            reconnectAttempt = 0
            openLocked()
        }
    }

    /** Returns false when the socket is not open, so the caller can drop the fix. */
    fun sendLocation(
        latitude: Double,
        longitude: Double,
        accuracyMeters: Float,
        timestampMillis: Long,
    ): Boolean {
        val socket = synchronized(lock) { webSocket } ?: return false
        val frame = LocationFrame(
            lat = latitude,
            lon = longitude,
            accuracy = accuracyMeters,
            ts = timestampMillis,
        )
        return socket.send(json.encodeToString(frame))
    }

    private fun openLocked() {
        val base = endpoint ?: return
        val token = authStore.accessToken()
        if (token == null) {
            _state.value = ConnectionState.DISCONNECTED
            _authExpired.tryEmit(Unit)
            return
        }

        webSocket?.cancel()
        _state.value = ConnectionState.CONNECTING
        val request = Request.Builder()
            .url(buildUrl(base))
            .header("Authorization", "Bearer $token")
            .build()
        webSocket = httpClient.newWebSocket(request, listener)
    }

    private fun buildUrl(base: String): String {
        val separator = if (base.contains('?')) '&' else '?'
        val name = URLEncoder.encode(displayName, "UTF-8")
        return "$base${separator}name=$name"
    }

    private fun scheduleReconnect() {
        synchronized(lock) {
            if (stopped) return
            if (reconnectJob?.isActive == true) return
            val backoffMs = min(MAX_BACKOFF_MS, BASE_BACKOFF_MS shl min(reconnectAttempt, 5))
            reconnectAttempt++
            reconnectJob = scope.launch {
                delay(backoffMs)
                synchronized(lock) {
                    if (!stopped) openLocked()
                }
            }
        }
    }

    private val listener = object : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.i(TAG, "socket open")
            synchronized(lock) { reconnectAttempt = 0 }
            _state.value = ConnectionState.CONNECTED
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val frame = runCatching { json.decodeFromString<ServerFrame>(text) }.getOrNull() ?: return
            _frames.tryEmit(frame)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            // Without this the reason a connection failed is invisible: the UI only
            // shows "Offline", which looks identical for a TLS error, a refused
            // connection and a rejected token.
            Log.w(TAG, "socket failure (http=${response?.code})", t)
            _state.value = ConnectionState.DISCONNECTED
            scheduleReconnect()
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            // OkHttp only calls onClosed once *both* peers have sent a close frame.
            // When the server closes first, acknowledging here is what completes the
            // handshake and lets onClosed run. Without it a server-initiated close is
            // completely silent: no onClosed, no onFailure, so the client never
            // refreshes its token and never reconnects, while the UI keeps showing
            // the last state it saw.
            webSocket.close(NORMAL_CLOSURE, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "socket closed code=$code reason=$reason")
            _state.value = ConnectionState.DISCONNECTED
            if (code == UNAUTHORIZED) {
                _authExpired.tryEmit(Unit)
            } else {
                scheduleReconnect()
            }
        }
    }

    private companion object {
        const val TAG = "WalkSocket"
        const val NORMAL_CLOSURE = 1000
        const val UNAUTHORIZED = 4401
        const val BASE_BACKOFF_MS = 1_000L
        const val MAX_BACKOFF_MS = 30_000L
    }
}
