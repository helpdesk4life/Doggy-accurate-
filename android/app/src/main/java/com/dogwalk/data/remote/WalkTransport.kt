package com.dogwalk.data.remote

import com.dogwalk.model.ConnectionState
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * The transport surface [com.dogwalk.data.repository.WalkRepository] drives.
 *
 * An interface so the repository's frame handling and session renewal can be
 * tested without a socket. [WalkSocketClient] owns OkHttp reader threads and
 * real backoff delays, so a test that waits on those is a test that flakes;
 * the transport itself is covered separately against a MockWebServer.
 */
interface WalkTransport {
    val state: StateFlow<ConnectionState>
    val frames: SharedFlow<ServerFrame>
    val authExpired: SharedFlow<Unit>

    fun connect(endpoint: String, displayName: String)

    fun disconnect()

    /** Re-open after the caller has refreshed the access token. */
    fun reconnect()

    /** Returns false when the socket is not open, so the caller can drop the fix. */
    fun sendLocation(
        latitude: Double,
        longitude: Double,
        accuracyMeters: Float,
        timestampMillis: Long,
    ): Boolean
}
