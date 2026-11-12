package com.dogwalk.ui.auth

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Pets
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dogwalk.R

@Composable
fun LoginScreen(viewModel: LoginViewModel = hiltViewModel()) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val reset by viewModel.reset.collectAsStateWithLifecycle()

    Surface(
        modifier = Modifier.fillMaxSize(),
        color = MaterialTheme.colorScheme.background,
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 28.dp, vertical = 24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Icon(
                imageVector = Icons.Filled.Pets,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(56.dp),
            )
            Spacer(Modifier.height(16.dp))
            Text(
                text = stringResource(R.string.app_name),
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = stringResource(
                    if (state.changeToken != null) {
                        R.string.auth_change_subtitle
                    } else {
                        R.string.auth_subtitle
                    },
                ),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(32.dp))

            if (state.changeToken == null) {
                OutlinedTextField(
                    value = state.email,
                    onValueChange = viewModel::setEmail,
                    label = { Text(stringResource(R.string.auth_email)) },
                    singleLine = true,
                    enabled = !state.isBusy,
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Email,
                        imeAction = ImeAction.Next,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = state.password,
                    onValueChange = viewModel::setPassword,
                    label = { Text(stringResource(R.string.auth_password)) },
                    singleLine = true,
                    enabled = !state.isBusy,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Password,
                        imeAction = if (state.isRegistering) ImeAction.Next else ImeAction.Done,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )

                // Registration is invite-gated. The field only exists in register
                // mode, so signing in stays a two-field form.
                if (state.isRegistering) {
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = state.inviteCode,
                        onValueChange = viewModel::setInviteCode,
                        label = { Text(stringResource(R.string.auth_invite_code)) },
                        supportingText = { Text(stringResource(R.string.auth_invite_hint)) },
                        singleLine = true,
                        enabled = !state.isBusy,
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Text,
                            imeAction = ImeAction.Done,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            } else {
                // The account still has the password an operator handed it. The
                // server issued a ticket instead of a session, so the only thing
                // this form can do is replace it.
                OutlinedTextField(
                    value = state.newPassword,
                    onValueChange = viewModel::setNewPassword,
                    label = { Text(stringResource(R.string.auth_new_password)) },
                    singleLine = true,
                    enabled = !state.isBusy,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Password,
                        imeAction = ImeAction.Next,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = state.confirmPassword,
                    onValueChange = viewModel::setConfirmPassword,
                    label = { Text(stringResource(R.string.auth_confirm_password)) },
                    singleLine = true,
                    enabled = !state.isBusy,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Password,
                        imeAction = ImeAction.Done,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            state.error?.let { error ->
                Spacer(Modifier.height(12.dp))
                Text(
                    text = errorText(error),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            Spacer(Modifier.height(24.dp))
            Button(
                onClick = if (state.changeToken != null) {
                    viewModel::submitNewPassword
                } else {
                    viewModel::submit
                },
                enabled = !state.isBusy,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(52.dp),
            ) {
                if (state.isBusy) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        strokeWidth = 2.dp,
                        color = MaterialTheme.colorScheme.onPrimary,
                    )
                } else {
                    Text(
                        stringResource(
                            when {
                                state.changeToken != null -> R.string.auth_save_password
                                state.isRegistering -> R.string.auth_create_account
                                else -> R.string.auth_sign_in
                            },
                        ),
                    )
                }
            }

            // Neither switching to registration nor a reset makes sense while
            // the account is mid-way through replacing a handed-over password.
            if (state.changeToken == null) {
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = viewModel::toggleMode, enabled = !state.isBusy) {
                    Text(
                        stringResource(
                            if (state.isRegistering) {
                                R.string.auth_switch_to_login
                            } else {
                                R.string.auth_switch_to_register
                            },
                        ),
                    )
                }
                if (!state.isRegistering) {
                    TextButton(onClick = viewModel::openReset, enabled = !state.isBusy) {
                        Text(stringResource(R.string.auth_forgot_password))
                    }
                }
            }
        }
    }

    if (reset.visible) {
        ResetDialog(
            state = reset,
            onEmail = viewModel::setResetEmail,
            onCode = viewModel::setResetCode,
            onPassword = viewModel::setResetPassword,
            onSendCode = viewModel::requestResetCode,
            onSubmit = viewModel::confirmReset,
            onDismiss = viewModel::closeReset,
        )
    }
}

@Composable
private fun ResetDialog(
    state: ResetUiState,
    onEmail: (String) -> Unit,
    onCode: (String) -> Unit,
    onPassword: (String) -> Unit,
    onSendCode: () -> Unit,
    onSubmit: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.reset_title)) },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                if (!state.done) {
                    Text(
                        text = stringResource(R.string.reset_body),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    OutlinedTextField(
                        value = state.email,
                        onValueChange = onEmail,
                        label = { Text(stringResource(R.string.auth_email)) },
                        singleLine = true,
                        enabled = !state.isBusy && !state.codeSent,
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Email,
                            imeAction = ImeAction.Next,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }

                if (state.codeSent && !state.done) {
                    OutlinedTextField(
                        value = state.code,
                        onValueChange = onCode,
                        label = { Text(stringResource(R.string.reset_code)) },
                        singleLine = true,
                        enabled = !state.isBusy,
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.NumberPassword,
                            imeAction = ImeAction.Next,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = state.password,
                        onValueChange = onPassword,
                        label = { Text(stringResource(R.string.reset_new_password)) },
                        singleLine = true,
                        enabled = !state.isBusy,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Password,
                            imeAction = ImeAction.Done,
                        ),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }

                state.notice?.let { notice ->
                    Text(
                        text = errorText(notice),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary,
                    )
                }
                state.error?.let { error ->
                    Text(
                        text = errorText(error),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        },
        confirmButton = {
            when {
                state.done -> TextButton(onClick = onDismiss) {
                    Text(stringResource(R.string.reset_close))
                }

                state.codeSent -> TextButton(onClick = onSubmit, enabled = !state.isBusy) {
                    Text(stringResource(R.string.reset_submit))
                }

                else -> TextButton(onClick = onSendCode, enabled = !state.isBusy) {
                    Text(stringResource(R.string.reset_send_code))
                }
            }
        },
        dismissButton = if (state.done) {
            null
        } else {
            { TextButton(onClick = onDismiss) { Text(stringResource(R.string.reset_close)) } }
        },
    )
}

/** Maps the ViewModel's sentinel values to strings; anything else is server text. */
@Composable
private fun errorText(error: String): String = when (error) {
    LoginViewModel.SHORT_PASSWORD -> stringResource(R.string.auth_error_short_password)
    LoginViewModel.NEEDS_INVITE -> stringResource(R.string.auth_error_needs_invite)
    LoginViewModel.UNREACHABLE -> stringResource(R.string.auth_error_unreachable)
    LoginViewModel.RESET_SENT -> stringResource(R.string.reset_sent)
    LoginViewModel.RESET_DONE -> stringResource(R.string.reset_done)
    LoginViewModel.RESET_BAD_CODE -> stringResource(R.string.reset_error_code)
    LoginViewModel.PASSWORDS_DIFFER -> stringResource(R.string.auth_error_passwords_differ)
    else -> error
}
