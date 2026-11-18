package com.mikywalk.data.auth

import com.mikywalk.testing.InMemorySecureStore
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The state machine the UI signs in and out on.
 *
 * These are the transitions that decide whether the app opens on the map or on
 * the login screen, so a regression here locks the user out rather than showing
 * something wrong.
 */
class AuthStoreTest {

    private val prefs = InMemorySecureStore()
    private val store = AuthStore(prefs)

    private fun session(
        access: String = "access-1",
        refresh: String = "refresh-1",
        userId: String = "user-1",
    ) = TokenResponse(
        accessToken = access,
        refreshToken = refresh,
        expiresIn = 900,
        userId = userId,
    )

    @Test
    fun `starts signed out when nothing is stored`() {
        assertEquals(AuthState.SignedOut, store.state.value)
        assertNull(store.accessToken())
        assertNull(store.refreshToken())
    }

    @Test
    fun `save signs in and exposes both tokens`() {
        store.save(session(), "walker@example.com")

        assertEquals(AuthState.SignedIn("user-1", "walker@example.com"), store.state.value)
        assertEquals("access-1", store.accessToken())
        assertEquals("refresh-1", store.refreshToken())
    }

    @Test
    fun `save persists the identity as well as the tokens`() {
        store.save(session(), "walker@example.com")

        assertEquals("access-1", prefs.getString("access_token"))
        assertEquals("refresh-1", prefs.getString("refresh_token"))
        assertEquals("user-1", prefs.getString("user_id"))
        assertEquals("walker@example.com", prefs.getString("email"))
    }

    @Test
    fun `updateTokens replaces the pair without touching the identity`() {
        store.save(session(), "walker@example.com")

        store.updateTokens(session(access = "access-2", refresh = "refresh-2"))

        assertEquals("access-2", store.accessToken())
        assertEquals("refresh-2", store.refreshToken())
        assertEquals(AuthState.SignedIn("user-1", "walker@example.com"), store.state.value)
        assertEquals("walker@example.com", prefs.getString("email"))
    }

    @Test
    fun `clear signs out and empties the store`() {
        store.save(session(), "walker@example.com")

        store.clear()

        assertEquals(AuthState.SignedOut, store.state.value)
        assertNull(store.accessToken())
        assertNull(store.refreshToken())
        assertNull(prefs.getString("access_token"))
        assertNull(prefs.getString("refresh_token"))
        assertNull(prefs.getString("user_id"))
        assertNull(prefs.getString("email"))
    }

    @Test
    fun `restores a session when a refresh token survived`() {
        prefs.putString("access_token", "access-1")
        prefs.putString("refresh_token", "refresh-1")
        prefs.putString("user_id", "user-1")
        prefs.putString("email", "walker@example.com")

        val restored = AuthStore(prefs)

        assertEquals(AuthState.SignedIn("user-1", "walker@example.com"), restored.state.value)
        assertEquals("refresh-1", restored.refreshToken())
    }

    @Test
    fun `does not restore without a refresh token`() {
        // The access token is short lived and the identity alone cannot renew a
        // session, so this must not come back as signed in.
        prefs.putString("access_token", "access-1")
        prefs.putString("user_id", "user-1")
        prefs.putString("email", "walker@example.com")

        assertEquals(AuthState.SignedOut, AuthStore(prefs).state.value)
    }

    @Test
    fun `does not restore without an identity`() {
        prefs.putString("access_token", "access-1")
        prefs.putString("refresh_token", "refresh-1")

        assertEquals(AuthState.SignedOut, AuthStore(prefs).state.value)
    }
}
