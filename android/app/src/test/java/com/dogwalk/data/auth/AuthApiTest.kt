package com.dogwalk.data.auth

import com.dogwalk.data.WalkSettings
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The account-request call, against a real [MockWebServer].
 *
 * The property worth pinning is the shape of the request: the address goes to
 * `/auth/account-request` and nothing else does. The server answers 202 whether
 * or not the address is registered, so there is no success/failure branch here
 * to get wrong — only a refusal (429, or an unreachable backend) to surface.
 */
class AuthApiTest {

    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun api(): AuthApi = AuthApi(json, FakeSettings(server.url("/ws/walk").toString()))

    @Test
    fun `requesting an account posts the address`() = runTest {
        server.enqueue(MockResponse().setResponseCode(202))

        api().requestAccount("alice@example.com")

        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/auth/account-request", recorded.path)
        assertEquals("""{"email":"alice@example.com"}""", recorded.body.readUtf8())
    }

    @Test
    fun `a refused request surfaces the server message`() = runTest {
        server.enqueue(
            MockResponse().setResponseCode(429).setBody("""{"detail":"too many requests"}"""),
        )

        val error = runCatching { api().requestAccount("alice@example.com") }.exceptionOrNull()

        assertTrue("expected AuthException, was $error", error is AuthException)
        assertEquals("too many requests", error?.message)
    }

    private class FakeSettings(backendUrl: String) : WalkSettings {
        override val backendUrl = MutableStateFlow(backendUrl)
        override val displayName = MutableStateFlow("Pickle")
        override val warningDistanceMeters = MutableStateFlow(100)
    }
}
