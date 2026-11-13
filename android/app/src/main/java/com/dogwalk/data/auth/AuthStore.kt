package com.dogwalk.data.auth

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Holds the signed-in session and persists it across launches.
 *
 * Tokens are written through [SecurePrefs], so the file on disk is AES-GCM
 * ciphertext under a key that lives in the Android Keystore and cannot be
 * exported. Combined with `android:allowBackup="false"` in the manifest, a
 * 30-day refresh token is no longer sitting in the user's Google account in
 * cleartext.
 *
 * The walker id is not generated here. It is the account id the server returns,
 * so a client cannot claim to be someone else.
 */
@Singleton
class AuthStore @Inject constructor(
    private val prefs: SecureStore,
) {
    @Volatile
    private var access: String? = prefs.getString(KEY_ACCESS)

    @Volatile
    private var refresh: String? = prefs.getString(KEY_REFRESH)

    private val _state = MutableStateFlow(restoreState())
    val state: StateFlow<AuthState> = _state.asStateFlow()

    fun accessToken(): String? = access

    fun refreshToken(): String? = refresh

    fun save(session: TokenResponse, email: String) {
        access = session.accessToken
        refresh = session.refreshToken
        prefs.putString(KEY_ACCESS, session.accessToken)
        prefs.putString(KEY_REFRESH, session.refreshToken)
        prefs.putString(KEY_USER_ID, session.userId)
        prefs.putString(KEY_EMAIL, email)
        _state.value = AuthState.SignedIn(session.userId, email)
    }

    fun updateTokens(session: TokenResponse) {
        access = session.accessToken
        refresh = session.refreshToken
        prefs.putString(KEY_ACCESS, session.accessToken)
        prefs.putString(KEY_REFRESH, session.refreshToken)
    }

    fun clear() {
        access = null
        refresh = null
        prefs.clear()
        _state.value = AuthState.SignedOut
    }

    private fun restoreState(): AuthState {
        val userId = prefs.getString(KEY_USER_ID)
        val email = prefs.getString(KEY_EMAIL)
        // A refresh token is what makes a session resumable; the access token is
        // short lived and will be renewed on the first 4401.
        return if (userId != null && email != null && refresh != null) {
            AuthState.SignedIn(userId, email)
        } else {
            AuthState.SignedOut
        }
    }

    companion object {
        internal const val PREFS_NAME = "dogwalk_auth"
        private const val KEY_ACCESS = "access_token"
        private const val KEY_REFRESH = "refresh_token"
        private const val KEY_USER_ID = "user_id"
        private const val KEY_EMAIL = "email"
    }
}
