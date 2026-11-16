@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package com.dogwalk.data.repository

import app.cash.turbine.test
import com.dogwalk.data.SettingsStore
import com.dogwalk.data.WalkSettings
import com.dogwalk.data.auth.AuthException
import com.dogwalk.data.auth.AuthState
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.auth.TokenRefresher
import com.dogwalk.data.auth.TokenResponse
import com.dogwalk.data.history.WalkHistoryStore
import com.dogwalk.data.remote.FrameTypes
import com.dogwalk.data.remote.NearbyWalkerFrame
import com.dogwalk.data.remote.ServerFrame
import com.dogwalk.data.remote.WalkTransport
import com.dogwalk.geo.GeoMath
import com.dogwalk.model.ConnectionState
import com.dogwalk.model.NearbyWalker
import com.dogwalk.model.ProximityEvent
import com.dogwalk.model.WalkerLocation
import com.dogwalk.testing.InMemorySecureStore
import com.dogwalk.testing.InMemoryWalkHistoryStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The session logic in [WalkRepository], on the JVM.
 *
 * Everything it needs is faked here on purpose. The three interfaces it depends
 * on exist because the concrete stores drag in a Context, an OkHttp socket and
 * an HTTP client respectively — none of which survive the JVM — so the fakes
 * are the deal: the tests pin the proximity bookkeeping and the three renewal
 * branches, and the real implementations are covered where they live, against
 * a MockWebServer ([WalkSocketClientTest], [AuthStoreTest]).
 */
class WalkRepositoryTest {

    private val fix = WalkerLocation(
        latitude = 52.0907,
        longitude = 5.1214,
        accuracyMeters = 8f,
        timestampMillis = 1_000,
    )

    /**
     * The scope the session runs on for the length of one test: the test's own
     * scheduler, in a supervisor of its own. The repository's collectors are
     * therefore ordinary tasks [advanceUntilIdle] drives, and they are cancelled
     * in `finally` rather than left to hang `runTest` — its own scope would wait
     * for a collector subscribed to an never-ending flow, and `backgroundScope`
     * would not run the collectors until the test body happens to suspend.
     */
    private inline fun TestScope.session(block: (CoroutineScope) -> Unit) {
        val scope = CoroutineScope(SupervisorJob() + StandardTestDispatcher(testScheduler))
        try {
            block(scope)
        } finally {
            scope.cancel()
        }
    }

    private fun TestScope.repository(
        scope: CoroutineScope,
        transport: FakeTransport,
        authStore: AuthStore,
        refresher: FakeRefresher,
        settings: FakeSettings = FakeSettings(),
        history: WalkHistoryStore = InMemoryWalkHistoryStore(),
    ): WalkRepository = WalkRepository(transport, settings, authStore, refresher, history, scope)

    /**
     * Start the walk and let the collectors [WalkRepository.startTracking]
     * launches actually subscribe. Without this pump the first frame a test
     * emits is gone: [MutableSharedFlow] drops values emitted before anyone is
     * collecting.
     */
    private fun TestScope.startTracking(repository: WalkRepository) {
        repository.startTracking()
        advanceUntilIdle()
    }

    /** Signed-in store, as it looks mid-walk with a live refresh token. */
    private fun signedInStore(): AuthStore = AuthStore(InMemorySecureStore()).apply {
        save(
            TokenResponse(
                accessToken = "access-1",
                refreshToken = "refresh-1",
                expiresIn = 900,
                userId = "user-1",
            ),
            email = "human@example.com",
        )
    }

    /** The server radius is 500 m here, as in [WalkRepository.DEFAULT_RADIUS_METERS]. */
    private fun alertFrame(vararg walkers: NearbyWalkerFrame) = ServerFrame(
        type = FrameTypes.PROXIMITY_ALERT,
        radiusMeters = 500.0,
        walkers = walkers.toList(),
    )

    private fun walker(id: String, distanceMeters: Double) = NearbyWalkerFrame(
        id = id,
        name = id.replaceFirstChar { it.uppercase() },
        distanceMeters = distanceMeters,
        bearingDegrees = 90.0,
    )

    private fun nearby(id: String, distanceMeters: Double) = NearbyWalker(
        id = id,
        name = id.replaceFirstChar { it.uppercase() },
        bearingDegrees = 90.0,
        distanceMeters = distanceMeters,
    )

    @Test
    fun `startTracking connects with the backend url and display name from settings`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val settings = FakeSettings()
            val repository = repository(scope, transport, signedInStore(), FakeRefresher(), settings)

            startTracking(repository)

            assertEquals(listOf(settings.backendUrl.value to settings.displayName.value), transport.connects)
        }
    }

    @Test
    fun `publishLocation mirrors the fix into self and hands it to the transport either way`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(scope, transport, signedInStore(), FakeRefresher())

            repository.publishLocation(fix)
            assertEquals(fix, repository.self.value)
            assertEquals(listOf(fix), transport.sendAttempts)
            assertFalse(transport.delivered)

            startTracking(repository)
            repository.publishLocation(fix.copy(timestampMillis = 2_000))
            assertTrue(transport.delivered)
        }
    }

    @Test
    fun `welcome frame sets the discovery radius`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(scope, transport, signedInStore(), FakeRefresher())
            startTracking(repository)

            transport.receive(ServerFrame(type = FrameTypes.WELCOME, radiusMeters = 250.0))
            advanceUntilIdle()

            assertEquals(250.0, repository.radiusMeters.value, 0.0)
        }
    }

    @Test
    fun `walkers beyond the warning distance stay on the map without raising an alert`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0), walker("rex", 300.0)))

                assertEquals(ProximityEvent.Arrived(listOf(nearby("buddy", 80.0))), awaitItem())
                assertEquals(
                    listOf("buddy", "rex"),
                    repository.nearby.value.map { it.id },
                )
            }
        }
    }

    @Test
    fun `a walker inside the warning distance alerts on arrival and not on every fix`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0)))
                assertEquals(ProximityEvent.Arrived(listOf(nearby("buddy", 80.0))), awaitItem())

                transport.receive(alertFrame(walker("buddy", 90.0)))
                transport.receive(alertFrame(walker("buddy", 80.0), walker("rex", 300.0)))

                expectNoEvents()
            }
        }
    }

    @Test
    fun `the last walker inside the warning distance leaving withdraws the alert`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0), walker("rex", 300.0)))
                assertEquals(ProximityEvent.Arrived(listOf(nearby("buddy", 80.0))), awaitItem())

                // Buddy wandered off. Rex is still on the map, but the alert is stale.
                transport.receive(alertFrame(walker("rex", 300.0)))

                assertEquals(ProximityEvent.Cleared, awaitItem())
                assertEquals(listOf("rex"), repository.nearby.value.map { it.id })
            }
        }
    }

    @Test
    fun `one of several walkers leaving refreshes the alert instead of withdrawing it`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0), walker("rex", 60.0)))
                assertEquals(
                    ProximityEvent.Arrived(listOf(nearby("buddy", 80.0), nearby("rex", 60.0))),
                    awaitItem(),
                )

                // Buddy wandered off. Rex is still inside, so the alert stands —
                // but it must stop naming the dog that left.
                transport.receive(alertFrame(walker("rex", 60.0)))

                assertEquals(ProximityEvent.Updated(listOf(nearby("rex", 60.0))), awaitItem())
            }
        }
    }

    @Test
    fun `a walker leaving while another arrives alerts on the arrival and does not also refresh`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0)))
                assertEquals(ProximityEvent.Arrived(listOf(nearby("buddy", 80.0))), awaitItem())

                // One frame, both changes. The arrival already carries the new
                // text, so a refresh on top of it would only be a second post.
                transport.receive(alertFrame(walker("rex", 60.0)))

                assertEquals(ProximityEvent.Arrived(listOf(nearby("rex", 60.0))), awaitItem())
                expectNoEvents()
            }
        }
    }

    @Test
    fun `a proximity clear frame empties the map and withdraws the alert`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
            )
            startTracking(repository)

            repository.proximityEvents.test {
                transport.receive(alertFrame(walker("buddy", 80.0)))
                assertEquals(ProximityEvent.Arrived(listOf(nearby("buddy", 80.0))), awaitItem())

                transport.receive(ServerFrame(type = FrameTypes.PROXIMITY_CLEAR))

                assertEquals(ProximityEvent.Cleared, awaitItem())
                assertTrue(repository.nearby.value.isEmpty())
            }
        }
    }

    @Test
    fun `expired token refreshes the pair and reconnects`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val refresher = FakeRefresher {
                assertEquals("refresh-1", it)
                TokenResponse(accessToken = "access-2", refreshToken = "refresh-2", expiresIn = 900, userId = "user-1")
            }
            val store = signedInStore()
            val repository = repository(scope, transport, store, refresher)

            startTracking(repository)

            transport.expireAuth()
            advanceUntilIdle()

            assertEquals(listOf("refresh-1"), refresher.refreshRequests)
            assertEquals("access-2", store.accessToken())
            assertEquals("refresh-2", store.refreshToken())
            assertEquals(1, transport.reconnects)
            assertTrue(repository.isTracking.value)
            assertEquals(AuthState.SignedIn("user-1", "human@example.com"), store.state.value)
        }
    }

    @Test
    fun `a refused refresh signs out and tears the walk down`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val refresher = FakeRefresher { throw AuthException("refresh token expired") }
            val store = signedInStore()
            val repository = repository(scope, transport, store, refresher)

            startTracking(repository)
            transport.receive(alertFrame(walker("buddy", 80.0)))
            repository.publishLocation(fix)
            advanceUntilIdle()

            transport.expireAuth()
            advanceUntilIdle()

            assertEquals(listOf("refresh-1"), refresher.refreshRequests)
            assertEquals(AuthState.SignedOut, store.state.value)
            assertNull(store.refreshToken())
            assertFalse(repository.isTracking.value)
            assertTrue(repository.nearby.value.isEmpty())
            assertNull(repository.self.value)
            assertEquals(1, transport.disconnects)
        }
    }

    @Test
    fun `no refresh token signs out without calling the server`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val refresher = FakeRefresher { throw AssertionError("the renewal path must not call home") }
            val store = AuthStore(InMemorySecureStore())
            val repository = repository(scope, transport, store, refresher)

            startTracking(repository)

            transport.expireAuth()
            advanceUntilIdle()

            assertEquals(emptyList<String>(), refresher.refreshRequests)
            assertEquals(AuthState.SignedOut, store.state.value)
            assertFalse(repository.isTracking.value)
            assertEquals(1, transport.disconnects)
        }
    }

    @Test
    fun `stopTracking persists the walk with its distance and the dogs met`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val history = InMemoryWalkHistoryStore()
            val repository = repository(
                scope,
                transport,
                signedInStore(),
                FakeRefresher(),
                settings = FakeSettings(warningDistanceMeters = 100),
                history = history,
            )
            startTracking(repository)

            val second = GeoMath.destination(fix.latitude, fix.longitude, 0.0, 100.0)
            repository.publishLocation(fix)
            repository.publishLocation(
                WalkerLocation(second.first, second.second, 5f, 6_000),
            )
            transport.receive(alertFrame(walker("buddy", 80.0)))
            advanceUntilIdle()

            repository.stopTracking()

            val walks = history.walks.value
            assertEquals(1, walks.size)
            val summary = walks.single()
            assertEquals(100.0, summary.distanceMeters, 1.0)
            assertEquals(listOf("buddy"), summary.dogsMet.map { it.id })
            assertEquals("Buddy", summary.dogsMet.single().name)
        }
    }

    @Test
    fun `a session that dies mid-walk still records the walk`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val refresher = FakeRefresher { throw AuthException("refresh token expired") }
            val history = InMemoryWalkHistoryStore()
            val repository = repository(scope, transport, signedInStore(), refresher, history = history)

            startTracking(repository)
            transport.receive(alertFrame(walker("buddy", 80.0)))
            repository.publishLocation(fix)
            advanceUntilIdle()

            transport.expireAuth()
            advanceUntilIdle()

            assertEquals(1, history.walks.value.size)
            assertEquals(listOf("buddy"), history.walks.value.single().dogsMet.map { it.id })
        }
    }

    @Test
    fun `starting a second walk without stopping does not record twice`() = runTest {
        session { scope ->
            val transport = FakeTransport(scope)
            val history = InMemoryWalkHistoryStore()
            val repository = repository(scope, transport, signedInStore(), FakeRefresher(), history = history)

            startTracking(repository)
            repository.startTracking()
            repository.publishLocation(fix)
            repository.stopTracking()

            assertEquals(1, history.walks.value.size)
        }
    }

    // ---- fakes ----

    /**
     * Counterpart of [com.dogwalk.data.remote.WalkSocketClient] minus the socket.
     *
     * [receive] and [expireAuth] enqueue their emission as a coroutine on the
     * session scope rather than emitting inline: [MutableSharedFlow] discards
     * values emitted with no subscriber, and the repository's collectors are
     * themselves coroutines. Queue order makes the delivery deterministic under
     * the test scheduler.
     */
    private class FakeTransport(
        private val scope: CoroutineScope,
    ) : WalkTransport {
        override val state = MutableStateFlow(ConnectionState.DISCONNECTED)
        private val _frames = MutableSharedFlow<ServerFrame>()
        override val frames: SharedFlow<ServerFrame> = _frames
        private val _authExpired = MutableSharedFlow<Unit>()
        override val authExpired: SharedFlow<Unit> = _authExpired

        val connects = mutableListOf<Pair<String, String>>()
        val sendAttempts = mutableListOf<WalkerLocation>()
        var reconnects = 0
            private set
        var disconnects = 0
            private set
        var delivered = false
            private set

        private var open = false

        override fun connect(endpoint: String, displayName: String) {
            connects += endpoint to displayName
            open = true
            state.value = ConnectionState.CONNECTED
        }

        override fun disconnect() {
            disconnects++
            open = false
            state.value = ConnectionState.DISCONNECTED
        }

        override fun reconnect() {
            reconnects++
        }

        override fun sendLocation(
            latitude: Double,
            longitude: Double,
            accuracyMeters: Float,
            timestampMillis: Long,
        ): Boolean {
            sendAttempts += WalkerLocation(latitude, longitude, accuracyMeters, timestampMillis)
            delivered = open
            return open
        }

        fun receive(frame: ServerFrame) {
            scope.launch { _frames.emit(frame) }
        }

        fun expireAuth() {
            scope.launch { _authExpired.emit(Unit) }
        }
    }

    private class FakeSettings(
        backendUrl: String = "wss://test.example/ws/walk",
        displayName: String = "Biscuit",
        warningDistanceMeters: Int = SettingsStore.DEFAULT_WARNING_DISTANCE_METERS,
    ) : WalkSettings {
        override val backendUrl = MutableStateFlow(backendUrl)
        override val displayName = MutableStateFlow(displayName)
        override val warningDistanceMeters = MutableStateFlow(warningDistanceMeters)
    }

    /**
     * [TokenRefresher] with the branch the test needs wired in: a returned
     * [TokenResponse] renews, a thrown [AuthException] is a refusal.
     */
    private class FakeRefresher(
        var answer: suspend (refreshToken: String) -> TokenResponse = {
            throw AssertionError("no refresh behaviour configured")
        },
    ) : TokenRefresher {
        val refreshRequests = mutableListOf<String>()

        override suspend fun refresh(refreshToken: String): TokenResponse {
            refreshRequests += refreshToken
            return answer(refreshToken)
        }
    }
}
