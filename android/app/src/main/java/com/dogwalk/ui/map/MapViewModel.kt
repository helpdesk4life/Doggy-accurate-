package com.dogwalk.ui.map

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.dogwalk.data.SettingsStore
import com.dogwalk.data.auth.AuthApi
import com.dogwalk.data.auth.AuthState
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.repository.WalkRepository
import com.dogwalk.model.ConnectionState
import com.dogwalk.model.DogRelationship
import com.dogwalk.model.MarkedDog
import com.dogwalk.model.NearbyWalker
import com.dogwalk.model.WalkerLocation
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

data class MapUiState(
    val self: WalkerLocation? = null,
    val nearby: List<NearbyWalker> = emptyList(),
    val connection: ConnectionState = ConnectionState.DISCONNECTED,
    val isTracking: Boolean = false,
    val radiusMeters: Double = WalkRepository.DEFAULT_RADIUS_METERS,
    val warningDistanceMeters: Int = SettingsStore.DEFAULT_WARNING_DISTANCE_METERS,
    val displayName: String = "",
    val backendUrl: String = "",
    val email: String = "",
    val markedDogs: Map<String, MarkedDog> = emptyMap(),
)

/** Progress of the delete-account confirmation in the settings sheet. */
data class DeleteAccountState(
    val isBusy: Boolean = false,
    val error: String? = null,
)

@HiltViewModel
class MapViewModel @Inject constructor(
    private val repository: WalkRepository,
    private val settings: SettingsStore,
    private val authStore: AuthStore,
    private val authApi: AuthApi,
) : ViewModel() {

    private data class Session(
        val self: WalkerLocation?,
        val nearby: List<NearbyWalker>,
        val connection: ConnectionState,
        val isTracking: Boolean,
        val radiusMeters: Double,
        val markedDogs: Map<String, MarkedDog>,
    )

    // Nested because combine has typed overloads only up to five flows, and the
    // marked dogs belong with the rest of the map data rather than the settings.
    private val session = combine(
        combine(
            repository.self,
            repository.nearby,
            repository.connection,
            repository.isTracking,
            repository.radiusMeters,
        ) { self, nearby, connection, isTracking, radiusMeters ->
            Session(self, nearby, connection, isTracking, radiusMeters, emptyMap())
        },
        settings.markedDogs,
    ) { current, markedDogs -> current.copy(markedDogs = markedDogs) }

    private val email = authStore.state.map { state ->
        (state as? AuthState.SignedIn)?.email.orEmpty()
    }

    private val _deleteAccount = MutableStateFlow(DeleteAccountState())
    val deleteAccount: StateFlow<DeleteAccountState> = _deleteAccount.asStateFlow()

    val uiState: StateFlow<MapUiState> = combine(
        session,
        settings.displayName,
        settings.backendUrl,
        settings.warningDistanceMeters,
        email,
    ) { current, displayName, backendUrl, warningDistanceMeters, email ->
        MapUiState(
            self = current.self,
            nearby = current.nearby,
            connection = current.connection,
            isTracking = current.isTracking,
            radiusMeters = current.radiusMeters,
            warningDistanceMeters = warningDistanceMeters,
            displayName = displayName,
            backendUrl = backendUrl,
            email = email,
            markedDogs = current.markedDogs,
        )
    }.stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000),
        initialValue = MapUiState(),
    )

    fun saveSettings(backendUrl: String, displayName: String, warningDistanceMeters: Int) {
        settings.setBackendUrl(backendUrl)
        settings.setDisplayName(displayName)
        settings.setWarningDistance(warningDistanceMeters)
    }

    /**
     * Record how our dog feels about a neighbour. Kept on the device only: the
     * server never learns who likes whom, so the social graph cannot leak from it.
     */
    fun setRelationship(id: String, name: String, relationship: DogRelationship) {
        settings.setRelationship(id, name, relationship)
    }

    fun clearRelationship(id: String) {
        settings.clearRelationship(id)
    }

    fun signOut() {
        viewModelScope.launch {
            repository.stopTracking()
            authStore.refreshToken()?.let { authApi.logout(it) }
            authStore.clear()
        }
    }

    /**
     * Delete the account for good. The server needs the password as well as the
     * token, so an unlocked phone is not enough on its own.
     */
    fun deleteAccount(password: String) {
        if (_deleteAccount.value.isBusy) return

        val token = authStore.accessToken()
        if (token == null) {
            signOut()
            return
        }

        _deleteAccount.value = DeleteAccountState(isBusy = true)
        viewModelScope.launch {
            runCatching { authApi.deleteAccount(token, password) }
                .onSuccess {
                    // The account is gone server-side; drop the walk and the
                    // session so the UI falls back to the login screen. Reset
                    // the dialog state too, or a later sign-in reopens it busy.
                    _deleteAccount.value = DeleteAccountState()
                    repository.stopTracking()
                    authStore.clear()
                }
                .onFailure {
                    _deleteAccount.value = DeleteAccountState(error = DELETE_FAILED)
                }
        }
    }

    fun clearDeleteError() {
        _deleteAccount.value = _deleteAccount.value.copy(error = null)
    }

    companion object {
        /** Sentinel the settings sheet maps to a string resource. */
        const val DELETE_FAILED = "delete-failed"
    }
}
