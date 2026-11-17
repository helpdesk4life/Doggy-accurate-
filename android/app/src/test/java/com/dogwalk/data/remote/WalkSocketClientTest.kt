package com.dogwalk.data.remote

import app.cash.turbine.test
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.auth.TokenResponse
import com.dogwalk.model.ConnectionState
import com.dogwalk.testing.InMemorySecureStore
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * The transport half of the walk session.
 *
 * These run against a real [MockWebServer] rather than a mocked OkHttp, because
 * the behaviour worth testing lives in the interaction between the two: whether
 * a close is acknowledged, whether a dead token is retried, whether the socket
 * comes back on its own.
 *
 * `runBlocking` rather than `runTest` on purpose. OkHttp delivers its callbacks
 * on its own threads, so virtual time would advance past a real event and turn
 * every wait into a timeout.
 */
class WalkSocketClientTest {

    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    private lateinit var server: MockWebServer
    private lateinit var scope: CoroutineScope
    private lateinit var client: WalkSocketClient

    /** How many sockets the server has accepted, so a reconnect is observable. */
    private val opens = AtomicInteger()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        client = WalkSocketClient(json, signedInStore("token-1"), scope)
    }

    @After
    fun tearDown() {
        client.disconnect()
        // Let the close handshake finish before the server goes away: shutdown()
        // waits for its queues to go idle and throws if a socket is still open.
        Thread.sleep(250)
        scope.cancel()
        server.shutdown()
    }

    @Test
    fun `connect opens the socket and reports connected`() {
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")

        assertTrue("expected CONNECTED, was ${client.state.value}", awaitState(ConnectionState.CONNECTED))
    }

    @Test
    fun `sends the access token as a bearer header`() {
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")
        assertTrue(awaitState(ConnectionState.CONNECTED))

        val request = server.takeRequest(5, TimeUnit.SECONDS)
        assertEquals("Bearer token-1", request?.getHeader("Authorization"))
    }

    @Test
    fun `sends the display name as a query parameter`() {
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")
        assertTrue(awaitState(ConnectionState.CONNECTED))

        val request = server.takeRequest(5, TimeUnit.SECONDS)
        assertEquals("Pickle", request?.requestUrl?.queryParameter("name"))
    }

    @Test
    fun `emits decoded server frames`() = runBlocking {
        server.enqueue(
            upgrade { socket ->
                socket.send(
                    """{"type":"PROXIMITY_ALERT","radius_m":500.0,"walkers":[""" +
                        """{"id":"w1","name":"Buddy","distance_m":50.0,"bearing_deg":180.0}]}""",
                )
            },
        )

        client.frames.test {
            client.connect(server.url("/ws/walk").toString(), "Pickle")

            val frame = awaitItem()
            assertEquals(FrameTypes.PROXIMITY_ALERT, frame.type)
            assertEquals(500.0, frame.radiusMeters ?: 0.0, 0.001)
            assertEquals(1, frame.walkers.size)
            assertEquals("Buddy", frame.walkers.first().name)
            assertEquals(50.0, frame.walkers.first().distanceMeters, 0.001)
            assertEquals(180.0, frame.walkers.first().bearingDegrees, 0.001)
        }
    }

    @Test
    fun `4401 emits authExpired and does not reconnect`() = runBlocking {
        server.enqueue(upgrade { it.close(4401, "unauthorized") })
        // A second response, so a wrong reconnect would show up as a second open
        // rather than hanging on an empty queue.
        server.enqueue(upgrade())

        client.authExpired.test {
            client.connect(server.url("/ws/walk").toString(), "Pickle")
            awaitItem()
        }

        // The first rung of the ladder is 1 s, so this is long enough to catch a
        // retry if one were scheduled.
        delay(1_500)

        assertEquals("a 4401 must not be retried with the same dead token", 1, opens.get())
    }

    @Test
    fun `a server-initiated close is acknowledged and reconnects`() {
        // Regression test for the onClosing bug. OkHttp only calls onClosed once
        // both peers have sent a close frame, so a client that does not reply in
        // onClosing never learns the socket died: no onClosed, no onFailure, no
        // reconnect, and the UI keeps showing the last state it saw.
        server.enqueue(upgrade { it.close(1000, "bye") })
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")

        assertTrue("expected a reconnect, opens=${opens.get()}", awaitOpens(2))
    }

    @Test
    fun `sendLocation returns false when the socket is not open`() {
        assertFalse(client.sendLocation(52.37, 4.89, 5f, 0L))
    }

    @Test
    fun `sendLocation sends a location frame`() {
        val received = CompletableDeferred<String>()
        server.enqueue(upgrade(onMessage = { _, text -> received.complete(text) }))

        client.connect(server.url("/ws/walk").toString(), "Pickle")
        assertTrue(awaitState(ConnectionState.CONNECTED))

        assertTrue(client.sendLocation(52.37, 4.89, 5f, 123L))

        val text = runBlocking { withTimeout(5_000) { received.await() } }
        val frame = json.decodeFromString<LocationFrame>(text)
        assertEquals(52.37, frame.lat, 0.0001)
        assertEquals(4.89, frame.lon, 0.0001)
        assertEquals(5f, frame.accuracy, 0.001f)
        assertEquals(123L, frame.ts)
    }

    @Test
    fun `disconnect stops the reconnect loop`() {
        server.enqueue(upgrade { it.close(1000, "bye") })
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")
        assertTrue(awaitState(ConnectionState.CONNECTED))

        client.disconnect()
        val settled = opens.get()

        Thread.sleep(1_500)

        assertEquals(ConnectionState.DISCONNECTED, client.state.value)
        assertEquals("a disconnected client must not keep retrying", settled, opens.get())
    }

    @Test
    fun `connect without a token emits authExpired instead of opening`() = runBlocking {
        val anonymous = WalkSocketClient(json, AuthStore(InMemorySecureStore()), scope)

        anonymous.authExpired.test {
            anonymous.connect(server.url("/ws/walk").toString(), "Pickle")
            awaitItem()
        }

        assertEquals(ConnectionState.DISCONNECTED, anonymous.state.value)
        assertEquals("no token means no socket", 0, opens.get())
    }

    @Test
    fun `the reconnect ladder doubles to a 30 s cap`() {
        val ladder = (0..7).map { WalkSocketClient.backoffMillis(it) }

        assertEquals(
            listOf(1_000L, 2_000L, 4_000L, 8_000L, 16_000L, 30_000L, 30_000L, 30_000L),
            ladder,
        )
    }

    @Test
    fun `a backend that keeps failing is retried on a growing ladder`() {
        // Every attempt gets a plain 500, which OkHttp reports as a failed
        // upgrade. What matters is that the loop keeps going and the gaps grow:
        // a client that stalled after the first retry, or retried at a fixed
        // 1 s, looks identical from the UI, which only ever shows "Offline".
        repeat(6) { server.enqueue(MockResponse().setResponseCode(500)) }

        client.connect(server.url("/ws/walk").toString(), "Pickle")

        val stamps = mutableListOf<Long>()
        val deadline = System.currentTimeMillis() + 4_500
        while (System.currentTimeMillis() < deadline) {
            if (server.takeRequest(200, TimeUnit.MILLISECONDS) != null) {
                stamps.add(System.currentTimeMillis())
            }
        }

        assertTrue("expected at least 3 attempts, saw ${stamps.size}", stamps.size >= 3)

        val firstGap = stamps[1] - stamps[0]
        val secondGap = stamps[2] - stamps[1]
        assertTrue("first gap was ${firstGap}ms, expected about 1 s", firstGap in 700..1_800)
        assertTrue("second gap was ${secondGap}ms, expected about 2 s", secondGap in 1_500..3_200)
    }

    @Test
    fun `reconnect cancels a retry that was already scheduled`() {
        // A failure schedules a retry one second out. If reconnect() does not
        // cancel it, that stale job fires afterwards and calls openLocked(),
        // which cancels the socket it just opened and dials again — a healthy
        // connection replaced for no reason, and one more attempt on a ladder
        // that should have reset.
        server.enqueue(MockResponse().setResponseCode(500))
        server.enqueue(upgrade())
        server.enqueue(upgrade())

        client.connect(server.url("/ws/walk").toString(), "Pickle")
        assertTrue("expected the first attempt", server.takeRequest(5, TimeUnit.SECONDS) != null)

        // Let onFailure run and schedule the retry before reconnecting.
        Thread.sleep(300)

        client.reconnect()
        assertTrue(awaitState(ConnectionState.CONNECTED))

        // Past the 1 s rung, where a stale job would fire.
        Thread.sleep(1_500)

        assertEquals("a scheduled retry must not fire after reconnect()", 2, server.requestCount)
    }

    private fun signedInStore(access: String): AuthStore =
        AuthStore(InMemorySecureStore()).apply {
            save(
                TokenResponse(
                    accessToken = access,
                    refreshToken = "refresh-1",
                    expiresIn = 900,
                    userId = "user-1",
                ),
                "walker@example.com",
            )
        }

    /**
     * A WebSocket upgrade whose server side counts opens and optionally acts.
     *
     * `onOpen` is last so the common case reads as a trailing lambda.
     */
    private fun upgrade(
        onMessage: ((WebSocket, String) -> Unit)? = null,
        onOpen: ((WebSocket) -> Unit)? = null,
    ): MockResponse =
        MockResponse().withWebSocketUpgrade(
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    opens.incrementAndGet()
                    onOpen?.invoke(webSocket)
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    onMessage?.invoke(webSocket, text)
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    // Ack the client's close. Without this the connection stays
                    // half-open and MockWebServer.shutdown() gives up waiting for
                    // its queue to go idle — the same handshake rule the client
                    // has to follow, on the other side.
                    webSocket.close(1000, null)
                }
            },
        )

    private fun awaitState(expected: ConnectionState, timeoutMs: Long = 5_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (client.state.value == expected) return true
            Thread.sleep(20)
        }
        return false
    }

    private fun awaitOpens(expected: Int, timeoutMs: Long = 5_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (opens.get() >= expected) return true
            Thread.sleep(20)
        }
        return false
    }
}
