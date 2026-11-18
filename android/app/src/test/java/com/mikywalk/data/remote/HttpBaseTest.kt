package com.mikywalk.data.remote

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The one address the settings sheet asks for, and the REST base derived from it.
 *
 * Both API clients go through this, so a mistake here breaks every HTTP call at
 * once. The WebSocket URL is what the user types; the HTTP URL is never shown.
 */
class HttpBaseTest {

    @Test
    fun `plain ws becomes http`() {
        assertEquals("http://10.0.2.2:8000", httpBaseOf("ws://10.0.2.2:8000/ws/walk"))
    }

    @Test
    fun `secure ws becomes https`() {
        assertEquals("https://mikywalk.example", httpBaseOf("wss://mikywalk.example/ws/walk"))
    }

    @Test
    fun `a query string on the socket url is dropped`() {
        assertEquals(
            "http://10.0.2.2:8000",
            httpBaseOf("ws://10.0.2.2:8000/ws/walk?name=Alice"),
        )
    }

    @Test
    fun `a url without the ws path is used as is`() {
        assertEquals("http://10.0.2.2:8000", httpBaseOf("http://10.0.2.2:8000"))
    }
}
