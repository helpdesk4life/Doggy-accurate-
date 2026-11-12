package com.dogwalk.data.auth

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class AuthRequest(
    val email: String,
    val password: String,
    /** Only read by /auth/register. Login ignores it. */
    @SerialName("invite_code") val inviteCode: String? = null,
)

@Serializable
data class RefreshRequest(
    @SerialName("refresh_token") val refreshToken: String,
)

@Serializable
data class EmailRequest(
    val email: String,
)

@Serializable
data class PasswordResetConfirm(
    val email: String,
    val code: String,
    val password: String,
)

@Serializable
data class DeleteAccountRequest(
    val password: String,
)

@Serializable
data class TokenResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("expires_in") val expiresIn: Int = 0,
    @SerialName("user_id") val userId: String,
)

@Serializable
data class ErrorResponse(
    val detail: String = "",
)

/** Raised for any auth call the server refused, carrying its message. */
class AuthException(message: String) : Exception(message)

/** What the UI needs to know. Tokens stay out of this so they cannot leak into a recomposition. */
sealed interface AuthState {
    data object SignedOut : AuthState
    data class SignedIn(val userId: String, val email: String) : AuthState
}
