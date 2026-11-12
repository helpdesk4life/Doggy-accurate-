package com.dogwalk.ui.auth

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.dogwalk.data.auth.AuthApi
import com.dogwalk.data.auth.AuthException
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.auth.LoginResult
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

data class LoginUiState(
    val email: String = "",
    val password: String = "",
    val inviteCode: String = "",
    val isRegistering: Boolean = false,
    val isBusy: Boolean = false,
    val error: String? = null,
    /**
     * Set when the account still has the password an operator handed it. While
     * this is non-null the form asks for a new password instead of a sign-in,
     * and the ticket is the only credential the server will accept.
     */
    val changeToken: String? = null,
    val newPassword: String = "",
    val confirmPassword: String = "",
)

/**
 * The password-reset sheet. Two steps in one dialog: ask for a code, then spend
 * it. [codeSent] is what moves the UI from the first to the second, and [done]
 * is the terminal state after the password has actually changed.
 */
data class ResetUiState(
    val visible: Boolean = false,
    val email: String = "",
    val code: String = "",
    val password: String = "",
    val codeSent: Boolean = false,
    val done: Boolean = false,
    val isBusy: Boolean = false,
    val error: String? = null,
    val notice: String? = null,
)

@HiltViewModel
class LoginViewModel @Inject constructor(
    private val authApi: AuthApi,
    private val authStore: AuthStore,
) : ViewModel() {

    private val _state = MutableStateFlow(LoginUiState())
    val state: StateFlow<LoginUiState> = _state.asStateFlow()

    private val _reset = MutableStateFlow(ResetUiState())
    val reset: StateFlow<ResetUiState> = _reset.asStateFlow()

    fun setEmail(value: String) {
        _state.value = _state.value.copy(email = value, error = null)
    }

    fun setPassword(value: String) {
        _state.value = _state.value.copy(password = value, error = null)
    }

    fun setInviteCode(value: String) {
        _state.value = _state.value.copy(inviteCode = value, error = null)
    }

    fun toggleMode() {
        _state.value = _state.value.copy(
            isRegistering = !_state.value.isRegistering,
            error = null,
        )
    }

    fun submit() {
        val current = _state.value
        if (current.isBusy) return

        val email = current.email.trim()
        if (email.isBlank() || current.password.length < MIN_PASSWORD_LENGTH) {
            _state.value = current.copy(error = SHORT_PASSWORD)
            return
        }
        if (current.isRegistering && current.inviteCode.isBlank()) {
            _state.value = current.copy(error = NEEDS_INVITE)
            return
        }

        _state.value = current.copy(isBusy = true, error = null)
        viewModelScope.launch {
            val result = runCatching {
                if (current.isRegistering) {
                    authApi.register(email, current.password, current.inviteCode.trim())
                } else {
                    authApi.login(email, current.password)
                }
            }

            result.onSuccess { outcome ->
                // This ViewModel is activity-scoped, so it outlives the screen.
                // Clear the spinner before saving: otherwise signing out brings
                // the form back disabled, with no way to submit it again. Drop
                // the password too, so it is not held for the app's lifetime.
                _state.value = _state.value.copy(isBusy = false, password = "")
                when (outcome) {
                    is LoginResult.SignedIn -> authStore.save(outcome.session, email)
                    // Not a session. The account has to pick a password first,
                    // and the ticket it got is good for nothing else.
                    is LoginResult.MustChangePassword -> _state.value = _state.value.copy(
                        changeToken = outcome.changeToken,
                        newPassword = "",
                        confirmPassword = "",
                    )
                }
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    isBusy = false,
                    error = (error as? AuthException)?.message ?: UNREACHABLE,
                )
            }
        }
    }

    // --- first sign-in on a handed-over password ---------------------------

    fun setNewPassword(value: String) {
        _state.value = _state.value.copy(newPassword = value, error = null)
    }

    fun setConfirmPassword(value: String) {
        _state.value = _state.value.copy(confirmPassword = value, error = null)
    }

    /**
     * Spend the ticket from login on a password of the account's own. The pair
     * that comes back is a real session, so there is nothing left to sign in to.
     */
    fun submitNewPassword() {
        val current = _state.value
        if (current.isBusy) return
        val ticket = current.changeToken ?: return

        if (current.newPassword.length < MIN_PASSWORD_LENGTH) {
            _state.value = current.copy(error = SHORT_PASSWORD)
            return
        }
        if (current.newPassword != current.confirmPassword) {
            _state.value = current.copy(error = PASSWORDS_DIFFER)
            return
        }

        _state.value = current.copy(isBusy = true, error = null)
        viewModelScope.launch {
            runCatching { authApi.changePassword(ticket, current.newPassword) }
                .onSuccess { session ->
                    _state.value = _state.value.copy(
                        isBusy = false,
                        newPassword = "",
                        confirmPassword = "",
                        changeToken = null,
                    )
                    authStore.save(session, _state.value.email.trim())
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        isBusy = false,
                        error = (error as? AuthException)?.message ?: UNREACHABLE,
                    )
                }
        }
    }

    // --- password reset ---------------------------------------------------

    fun openReset() {
        _reset.value = ResetUiState(visible = true, email = _state.value.email.trim())
    }

    fun closeReset() {
        _reset.value = ResetUiState()
    }

    fun setResetEmail(value: String) {
        _reset.value = _reset.value.copy(email = value, error = null)
    }

    fun setResetCode(value: String) {
        _reset.value = _reset.value.copy(code = value.filter(Char::isDigit).take(6), error = null)
    }

    fun setResetPassword(value: String) {
        _reset.value = _reset.value.copy(password = value, error = null)
    }

    fun requestResetCode() {
        val current = _reset.value
        if (current.isBusy) return

        val email = current.email.trim()
        if (email.isBlank()) {
            _reset.value = current.copy(error = UNREACHABLE)
            return
        }

        _reset.value = current.copy(isBusy = true, error = null, notice = null)
        viewModelScope.launch {
            runCatching { authApi.requestPasswordReset(email) }
                .onSuccess {
                    // The server answers 202 either way, so this message must not
                    // claim the address exists.
                    _reset.value = _reset.value.copy(
                        isBusy = false,
                        codeSent = true,
                        notice = RESET_SENT,
                    )
                }
                .onFailure { error ->
                    _reset.value = _reset.value.copy(
                        isBusy = false,
                        error = (error as? AuthException)?.message ?: UNREACHABLE,
                    )
                }
        }
    }

    fun confirmReset() {
        val current = _reset.value
        if (current.isBusy) return

        if (current.code.length != CODE_LENGTH) {
            _reset.value = current.copy(error = RESET_BAD_CODE)
            return
        }
        if (current.password.length < MIN_PASSWORD_LENGTH) {
            _reset.value = current.copy(error = SHORT_PASSWORD)
            return
        }

        _reset.value = current.copy(isBusy = true, error = null)
        viewModelScope.launch {
            runCatching {
                authApi.confirmPasswordReset(
                    email = current.email.trim(),
                    code = current.code,
                    password = current.password,
                )
            }.onSuccess {
                // Every session was revoked server-side, so the only thing left
                // to do is sign in again with the new password.
                _state.value = _state.value.copy(
                    email = current.email.trim(),
                    password = "",
                    error = null,
                )
                _reset.value = ResetUiState(visible = true, done = true, notice = RESET_DONE)
            }.onFailure { error ->
                _reset.value = _reset.value.copy(
                    isBusy = false,
                    error = (error as? AuthException)?.message ?: UNREACHABLE,
                )
            }
        }
    }

    companion object {
        const val MIN_PASSWORD_LENGTH = 8
        const val CODE_LENGTH = 6

        /** Sentinel values the screen maps to string resources. */
        const val SHORT_PASSWORD = "short-password"
        const val NEEDS_INVITE = "needs-invite"
        const val UNREACHABLE = "unreachable"
        const val RESET_SENT = "reset-sent"
        const val RESET_DONE = "reset-done"
        const val RESET_BAD_CODE = "reset-bad-code"
        const val PASSWORDS_DIFFER = "passwords-differ"
    }
}
