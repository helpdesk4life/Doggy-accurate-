package com.dogwalk.data.remote

/**
 * The REST base URL behind a configured WebSocket URL.
 *
 * The settings sheet only ever asks for one address, so the HTTP side is derived
 * from it: `ws://host:8000/ws/walk` becomes `http://host:8000`. Shared by every
 * API client so the mapping cannot drift between them.
 */
fun httpBaseOf(backendUrl: String): String {
    val base = backendUrl.substringBefore("/ws/")
    return when {
        base.startsWith("wss://") -> "https://" + base.removePrefix("wss://")
        base.startsWith("ws://") -> "http://" + base.removePrefix("ws://")
        else -> base
    }
}
