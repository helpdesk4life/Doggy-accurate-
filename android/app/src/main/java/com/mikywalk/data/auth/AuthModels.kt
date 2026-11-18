package com.mikywalk.data.auth

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

/**
 * What `/auth/login` answers. Either a token pair, or — when the account still
 * has the password an operator handed it — a ticket to set a real one. The
 * token fields are nullable because the server omits them in that second case
 * rather than sending blanks.
 */
@Serializable
data class LoginResponse(
    @SerialName("user_id") val userId: String,
    @SerialName("must_change_password") val mustChangePassword: Boolean = false,
    @SerialName("access_token") val accessToken: String? = null,
    @SerialName("refresh_token") val refreshToken: String? = null,
    @SerialName("expires_in") val expiresIn: Int = 0,
    @SerialName("change_token") val changeToken: String? = null,
)

@Serializable
data class ChangePasswordRequest(
    @SerialName("new_password") val newPassword: String,
)

/** The two things a successful sign-in can mean. */
sealed interface LoginResult {
    data class SignedIn(val session: TokenResponse) : LoginResult

    /**
     * The account has a handed-over password. [changeToken] opens
     * `/auth/password/change` and nothing else, so it is not a session and must
     * not be stored as one.
     */
    data class MustChangePassword(val changeToken: String, val userId: String) : LoginResult
}

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
