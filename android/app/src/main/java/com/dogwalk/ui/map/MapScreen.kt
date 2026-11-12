package com.dogwalk.ui.map

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Pets
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.MyLocation
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.colorResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dogwalk.R
import com.dogwalk.data.SettingsStore
import com.dogwalk.model.ConnectionState
import com.dogwalk.model.DogRelationship
import com.dogwalk.model.MarkedDog
import com.dogwalk.model.NearbyWalker
import com.dogwalk.service.LocationTrackingService

@Composable
fun MapScreen(viewModel: MapViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val deleteState by viewModel.deleteAccount.collectAsStateWithLifecycle()
    val context = LocalContext.current

    var locationGranted by remember { mutableStateOf(hasLocationPermission(context)) }
    var showSettings by remember { mutableStateOf(false) }
    var showDeleteAccount by remember { mutableStateOf(false) }
    var recenterSignal by remember { mutableIntStateOf(0) }
    var markTarget by remember { mutableStateOf<NearbyWalker?>(null) }

    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestMultiplePermissions(),
    ) {
        locationGranted = hasLocationPermission(context)
    }

    Box(modifier = Modifier.fillMaxSize()) {
        MapLibreMap(
            self = state.self,
            selfName = state.displayName,
            nearby = state.nearby,
            marked = state.markedDogs,
            radiusMeters = state.radiusMeters,
            warningDistanceMeters = state.warningDistanceMeters.toDouble(),
            recenterSignal = recenterSignal,
            onWalkerTapped = { markTarget = it },
            modifier = Modifier.fillMaxSize(),
        )

        StatusPill(
            state = state,
            modifier = Modifier
                .align(Alignment.TopCenter)
                .statusBarsPadding()
                .padding(top = 12.dp),
        )

        SmallFloatingActionButton(
            onClick = { recenterSignal++ },
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .navigationBarsPadding()
                .padding(end = 16.dp, bottom = 220.dp),
            containerColor = MaterialTheme.colorScheme.surface,
        ) {
            Icon(
                imageVector = Icons.Outlined.MyLocation,
                contentDescription = stringResource(R.string.action_recenter),
            )
        }

        Surface(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .navigationBarsPadding()
                .padding(16.dp),
            shape = RoundedCornerShape(24.dp),
            tonalElevation = 3.dp,
            shadowElevation = 8.dp,
        ) {
            Column(modifier = Modifier.padding(20.dp)) {
                if (locationGranted) {
                    WalkControls(
                        state = state,
                        onToggleTracking = {
                            if (state.isTracking) {
                                LocationTrackingService.stop(context)
                            } else {
                                LocationTrackingService.start(context)
                            }
                        },
                        onOpenSettings = { showSettings = true },
                    )
                } else {
                    PermissionPrompt(
                        onRequest = { permissionLauncher.launch(requiredPermissions()) },
                    )
                }
            }
        }
    }

    if (showSettings) {
        SettingsDialog(
            initialUrl = state.backendUrl,
            initialName = state.displayName,
            initialWarningDistance = state.warningDistanceMeters,
            email = state.email,
            markedDogs = state.markedDogs,
            onDismiss = { showSettings = false },
            onSave = { url, name, warningDistance ->
                viewModel.saveSettings(url, name, warningDistance)
                showSettings = false
            },
            onClearRelationship = viewModel::clearRelationship,
            onSignOut = {
                showSettings = false
                viewModel.signOut()
            },
            onDeleteAccount = {
                showSettings = false
                showDeleteAccount = true
            },
        )
    }

    markTarget?.let { walker ->
        MarkDogDialog(
            walker = walker,
            current = state.markedDogs[walker.id]?.relationship,
            onSelect = { relationship ->
                viewModel.setRelationship(walker.id, walker.name, relationship)
                markTarget = null
            },
            onClear = {
                viewModel.clearRelationship(walker.id)
                markTarget = null
            },
            onDismiss = { markTarget = null },
        )
    }

    if (showDeleteAccount) {
        DeleteAccountDialog(
            state = deleteState,
            onConfirm = viewModel::deleteAccount,
            onDismiss = {
                showDeleteAccount = false
                viewModel.clearDeleteError()
            },
        )
    }
}

@Composable
private fun StatusPill(state: MapUiState, modifier: Modifier = Modifier) {
    val label = when {
        !state.isTracking -> stringResource(R.string.status_idle)
        state.connection == ConnectionState.CONNECTED -> stringResource(R.string.status_live)
        state.connection == ConnectionState.CONNECTING -> stringResource(R.string.status_connecting)
        else -> stringResource(R.string.status_offline)
    }
    val dotColor = when {
        !state.isTracking -> MaterialTheme.colorScheme.outline
        state.connection == ConnectionState.CONNECTED -> MaterialTheme.colorScheme.primary
        state.connection == ConnectionState.CONNECTING -> MaterialTheme.colorScheme.tertiary
        else -> MaterialTheme.colorScheme.error
    }

    Surface(
        modifier = modifier,
        shape = RoundedCornerShape(percent = 50),
        color = MaterialTheme.colorScheme.surface,
        tonalElevation = 3.dp,
        shadowElevation = 4.dp,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Box(
                modifier = Modifier
                    .size(8.dp)
                    .clip(CircleShape)
                    .background(dotColor),
            )
            Text(text = label, style = MaterialTheme.typography.labelLarge)
            if (state.nearby.isNotEmpty()) {
                Text(
                    text = stringResource(R.string.nearby_count, state.nearby.size),
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.secondary,
                )
            }
        }
    }
}

@Composable
private fun WalkControls(
    state: MapUiState,
    onToggleTracking: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(
            imageVector = Icons.Filled.Pets,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
        )
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                text = stringResource(R.string.app_name),
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
            )
            Text(
                text = if (state.isTracking) {
                    stringResource(R.string.status_live)
                } else {
                    stringResource(R.string.status_idle)
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        IconButton(onClick = onOpenSettings) {
            Icon(
                imageVector = Icons.Outlined.Settings,
                contentDescription = stringResource(R.string.action_settings),
            )
        }
    }

    Spacer(Modifier.height(16.dp))

    Button(
        onClick = onToggleTracking,
        modifier = Modifier
            .fillMaxWidth()
            .height(52.dp),
        colors = if (state.isTracking) {
            ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)
        } else {
            ButtonDefaults.buttonColors()
        },
    ) {
        Text(
            text = if (state.isTracking) {
                stringResource(R.string.action_stop)
            } else {
                stringResource(R.string.action_start)
            },
        )
    }

    if (state.nearby.isNotEmpty()) {
        Spacer(Modifier.height(12.dp))
        Text(
            text = stringResource(R.string.nearby_count, state.nearby.size),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.secondary,
        )
    }
}

@Composable
private fun PermissionPrompt(onRequest: () -> Unit) {
    Text(
        text = stringResource(R.string.permission_rationale_title),
        style = MaterialTheme.typography.titleMedium,
    )
    Spacer(Modifier.height(8.dp))
    Text(
        text = stringResource(R.string.permission_rationale_body),
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    Spacer(Modifier.height(16.dp))
    Button(onClick = onRequest, modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(R.string.permission_grant))
    }
}

@Composable
private fun SettingsDialog(
    initialUrl: String,
    initialName: String,
    initialWarningDistance: Int,
    email: String,
    markedDogs: Map<String, MarkedDog>,
    onDismiss: () -> Unit,
    onSave: (String, String, Int) -> Unit,
    onClearRelationship: (String) -> Unit,
    onSignOut: () -> Unit,
    onDeleteAccount: () -> Unit,
) {
    var url by remember { mutableStateOf(initialUrl) }
    var name by remember { mutableStateOf(initialName) }
    var warningDistance by remember { mutableIntStateOf(initialWarningDistance) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.settings_title)) },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = { Text(stringResource(R.string.settings_backend_url)) },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text(stringResource(R.string.settings_display_name)) },
                    singleLine = true,
                )

                Text(
                    text = stringResource(R.string.settings_warning_distance),
                    style = MaterialTheme.typography.labelLarge,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    SettingsStore.WARNING_DISTANCE_OPTIONS.forEach { meters ->
                        FilterChip(
                            selected = warningDistance == meters,
                            onClick = { warningDistance = meters },
                            label = {
                                Text(
                                    stringResource(
                                        R.string.settings_warning_distance_value,
                                        meters,
                                    ),
                                )
                            },
                        )
                    }
                }
                Text(
                    text = stringResource(R.string.settings_warning_hint),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )

                HorizontalDivider()
                Text(
                    text = stringResource(R.string.settings_marked_title),
                    style = MaterialTheme.typography.labelLarge,
                )
                if (markedDogs.isEmpty()) {
                    Text(
                        text = stringResource(R.string.settings_marked_empty),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    MarkedDogGroup(
                        title = stringResource(R.string.settings_friends),
                        dogs = markedDogs.values.filter {
                            it.relationship == DogRelationship.FRIEND
                        },
                        onRemove = onClearRelationship,
                    )
                    MarkedDogGroup(
                        title = stringResource(R.string.settings_disliked),
                        dogs = markedDogs.values.filter {
                            it.relationship == DogRelationship.DISLIKED
                        },
                        onRemove = onClearRelationship,
                    )
                }

                if (email.isNotBlank()) {
                    HorizontalDivider()
                    Text(
                        text = stringResource(R.string.auth_signed_in_as, email),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    TextButton(onClick = onSignOut) {
                        Text(stringResource(R.string.settings_sign_out))
                    }
                    TextButton(onClick = onDeleteAccount) {
                        Text(
                            text = stringResource(R.string.settings_delete_account),
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { onSave(url, name, warningDistance) }) {
                Text(stringResource(R.string.settings_save))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.settings_cancel))
            }
        },
    )
}

/**
 * One relationship group inside the settings sheet. Empty groups render nothing,
 * so the sheet does not grow two headings when only one kind is marked.
 */
@Composable
private fun MarkedDogGroup(
    title: String,
    dogs: List<MarkedDog>,
    onRemove: (String) -> Unit,
) {
    if (dogs.isEmpty()) return

    Text(
        text = title,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    dogs.sortedBy { it.name.lowercase() }.forEach { dog ->
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = dog.name,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f),
            )
            IconButton(onClick = { onRemove(dog.id) }) {
                Icon(
                    imageVector = Icons.Outlined.Close,
                    contentDescription = stringResource(R.string.settings_marked_remove, dog.name),
                )
            }
        }
    }
}

/**
 * Mark a neighbour as liked or disliked.
 *
 * The relationship is stored on this device only. Nothing about it is sent to the
 * server, so the social graph cannot leak from the backend.
 */
@Composable
private fun MarkDogDialog(
    walker: NearbyWalker,
    current: DogRelationship?,
    onSelect: (DogRelationship) -> Unit,
    onClear: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.mark_title, walker.name)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    text = stringResource(R.string.mark_body),
                    style = MaterialTheme.typography.bodyMedium,
                )
                Button(
                    onClick = { onSelect(DogRelationship.FRIEND) },
                    modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = colorResource(R.color.marker_friend),
                    ),
                ) {
                    Text(stringResource(R.string.mark_friend))
                }
                Button(
                    onClick = { onSelect(DogRelationship.DISLIKED) },
                    modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = colorResource(R.color.marker_disliked),
                    ),
                ) {
                    Text(stringResource(R.string.mark_disliked))
                }
            }
        },
        confirmButton = {
            if (current != null) {
                TextButton(onClick = onClear) {
                    Text(stringResource(R.string.mark_clear))
                }
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.mark_cancel))
            }
        },
    )
}

/**
 * Confirmation for account deletion.
 *
 * The password is required as well as the session: an unlocked phone should not
 * be enough to destroy someone's account.
 */
@Composable
private fun DeleteAccountDialog(
    state: DeleteAccountState,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var password by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.settings_delete_account)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    text = stringResource(R.string.settings_delete_account_body),
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    label = { Text(stringResource(R.string.settings_delete_account_password)) },
                    singleLine = true,
                    enabled = !state.isBusy,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Password,
                        imeAction = ImeAction.Done,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                state.error?.let {
                    Text(
                        text = stringResource(R.string.settings_delete_account_error),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(password) },
                enabled = !state.isBusy && password.isNotBlank(),
            ) {
                Text(
                    text = stringResource(R.string.settings_delete_account_confirm),
                    color = MaterialTheme.colorScheme.error,
                )
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.settings_cancel))
            }
        },
    )
}

private fun requiredPermissions(): Array<String> = buildList {    add(Manifest.permission.ACCESS_FINE_LOCATION)
    add(Manifest.permission.ACCESS_COARSE_LOCATION)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        add(Manifest.permission.POST_NOTIFICATIONS)
    }
}.toTypedArray()

private fun hasLocationPermission(context: Context): Boolean =
    ContextCompat.checkSelfPermission(
        context,
        Manifest.permission.ACCESS_FINE_LOCATION,
    ) == PackageManager.PERMISSION_GRANTED
