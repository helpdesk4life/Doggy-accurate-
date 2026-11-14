package com.dogwalk.data.auth

import com.dogwalk.data.SettingsStore
import com.dogwalk.data.remote.httpBaseOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * The HTTP half of the backend: register, login, refresh, logout, password
 * reset and account deletion.
 *
 * The base URL is derived from the configured WebSocket URL, so the settings
 * sheet only ever has one address to get right.
 */
@Singleton
class AuthApi @Inject constructor(
    private val json: Json,
    private val settings: SettingsStore,
) : TokenRefresher {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    /**
     * Create an account. Registration is invite-gated: without a valid code the
     * server answers 403 and says nothing about whether the address is free.
     */
    suspend fun register(email: String, password: String, inviteCode: String): TokenResponse =
        json.decodeFromString(
            send(
                "POST",
                "/auth/register",
                json.encodeToString(AuthRequest(email, password, inviteCode.ifBlank { null })),
            ),
        )

    /**
     * Sign in. The answer is either a session or a ticket to set a password:
     * an account an operator set up still has the password they were handed,
     * and the server will not turn that shared secret into a session.
     */
    suspend fun login(email: String, password: String): LoginResult {
        val body = json.decodeFromString<LoginResponse>(
            send("POST", "/auth/login", json.encodeToString(AuthRequest(email, password))),
        )

        val access = body.accessToken
        val refresh = body.refreshToken
        return if (body.mustChangePassword || access == null || refresh == null) {
            LoginResult.MustChangePassword(
                changeToken = body.changeToken.orEmpty(),
                userId = body.userId,
            )
        } else {
            LoginResult.SignedIn(
                TokenResponse(
                    accessToken = access,
                    refreshToken = refresh,
                    expiresIn = body.expiresIn,
                    userId = body.userId,
                ),
            )
        }
    }

    /**
     * Replace a handed-over password. The ticket is the only credential this
     * endpoint accepts, and it dies with the change, so the pair it returns is
     * the session — there is no second sign-in.
     */
    suspend fun changePassword(changeToken: String, newPassword: String): TokenResponse =
        json.decodeFromString(
            send(
                "POST",
                "/auth/password/change",
                json.encodeToString(ChangePasswordRequest(newPassword)),
                accessToken = changeToken,
            ),
        )

    override suspend fun refresh(refreshToken: String): TokenResponse =
        json.decodeFromString(
            send("POST", "/auth/refresh", json.encodeToString(RefreshRequest(refreshToken))),
        )

    /** Best effort: a failed logout still clears the local session. */
    suspend fun logout(refreshToken: String) {
        runCatching {
            send("POST", "/auth/logout", json.encodeToString(RefreshRequest(refreshToken)))
        }
    }

    /**
     * Ask for a reset code. The server answers 202 whether or not the address
     * has an account, so this cannot be used to find out who is registered.
     */
    suspend fun requestPasswordReset(email: String) {
        send("POST", "/auth/password-reset/request", json.encodeToString(EmailRequest(email)))
    }

    /** Exchange a reset code for a new password. Every existing session dies. */
    suspend fun confirmPasswordReset(email: String, code: String, password: String) {
        send(
            "POST",
            "/auth/password-reset/confirm",
            json.encodeToString(PasswordResetConfirm(email, code, password)),
        )
    }

    /** Delete the account. Needs the password as well as the token. */
    suspend fun deleteAccount(accessToken: String, password: String) {
        send(
            "DELETE",
            "/auth/account",
            json.encodeToString(DeleteAccountRequest(password)),
            accessToken = accessToken,
        )
    }

    private suspend fun send(
        method: String,
        path: String,
        body: String,
        accessToken: String? = null,
    ): String = withContext(Dispatchers.IO) {
        val requestBody = body.toRequestBody(jsonMedia)
        val builder = Request.Builder()
            .url(httpBaseOf(settings.backendUrl.value) + path)
            .apply { if (accessToken != null) header("Authorization", "Bearer $accessToken") }

        when (method) {
            "POST" -> builder.post(requestBody)
            "DELETE" -> builder.delete(requestBody)
            else -> error("unsupported method $method")
        }

        client.newCall(builder.build()).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching {
                    json.decodeFromString<ErrorResponse>(text).detail
                }.getOrNull().orEmpty()
                throw AuthException(detail.ifBlank { "HTTP ${response.code}" })
            }
            text
        }
    }
}
