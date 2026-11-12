package com.dogwalk.data

import android.content.Context
import com.dogwalk.BuildConfig
import com.dogwalk.model.DogRelationship
import com.dogwalk.model.MarkedDog
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.json.Json
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Small persisted settings surface: where the backend lives, what this dog is
 * called, how close is too close, and which other dogs it likes.
 *
 * Identity lives in [com.dogwalk.data.auth.AuthStore]; the walker id is the
 * account id the server assigns, not something this class invents.
 *
 * The like/dislike list is deliberately local. The server has no idea who likes
 * whom, which means the social graph cannot leak from Redis and a compromised
 * backend cannot infer it.
 */
@Singleton
class SettingsStore @Inject constructor(
    @ApplicationContext context: Context,
    private val json: Json,
) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _backendUrl = MutableStateFlow(
        prefs.getString(KEY_BACKEND_URL, null) ?: BuildConfig.DEFAULT_BACKEND_URL,
    )
    val backendUrl: StateFlow<String> = _backendUrl.asStateFlow()

    private val _displayName = MutableStateFlow(
        prefs.getString(KEY_DISPLAY_NAME, null) ?: DEFAULT_DOG_NAMES.random().also {
            prefs.edit().putString(KEY_DISPLAY_NAME, it).apply()
        },
    )
    val displayName: StateFlow<String> = _displayName.asStateFlow()

    /**
     * How close another walker has to be before the phone buzzes. Independent of the
     * server's discovery radius: everyone inside that radius still shows on the map,
     * but only walkers inside this one raise an alert.
     */
    private val _warningDistanceMeters = MutableStateFlow(
        prefs.getInt(KEY_WARNING_DISTANCE, DEFAULT_WARNING_DISTANCE_METERS),
    )
    val warningDistanceMeters: StateFlow<Int> = _warningDistanceMeters.asStateFlow()

    /**
     * Dogs the user has marked, keyed by walker id. A walker absent from this map
     * is [DogRelationship.UNKNOWN].
     */
    private val _markedDogs = MutableStateFlow(loadMarkedDogs())
    val markedDogs: StateFlow<Map<String, MarkedDog>> = _markedDogs.asStateFlow()

    fun setBackendUrl(url: String) {
        val trimmed = url.trim()
        if (trimmed.isEmpty()) return
        prefs.edit().putString(KEY_BACKEND_URL, trimmed).apply()
        _backendUrl.value = trimmed
    }

    fun setDisplayName(name: String) {
        val trimmed = name.trim().ifEmpty { _displayName.value }
        prefs.edit().putString(KEY_DISPLAY_NAME, trimmed).apply()
        _displayName.value = trimmed
    }

    fun setWarningDistance(meters: Int) {
        val clamped = meters.coerceIn(MIN_WARNING_DISTANCE_METERS, MAX_WARNING_DISTANCE_METERS)
        prefs.edit().putInt(KEY_WARNING_DISTANCE, clamped).apply()
        _warningDistanceMeters.value = clamped
    }

    /**
     * Mark a dog as liked or disliked. Passing [DogRelationship.UNKNOWN] forgets
     * it, which is what the "clear" affordance in settings does.
     *
     * The name is stored alongside the id so the settings list can still show
     * "Buddy" when Buddy is not on the map.
     */
    fun setRelationship(id: String, name: String, relationship: DogRelationship) {
        if (id.isBlank()) return
        val next = _markedDogs.value.toMutableMap()
        if (relationship == DogRelationship.UNKNOWN) {
            next.remove(id)
        } else {
            next[id] = MarkedDog(id = id, name = name, relationship = relationship)
        }
        persistMarkedDogs(next)
    }

    fun clearRelationship(id: String) {
        val next = _markedDogs.value.toMutableMap()
        if (next.remove(id) != null) persistMarkedDogs(next)
    }

    private fun persistMarkedDogs(next: Map<String, MarkedDog>) {
        _markedDogs.value = next
        prefs.edit()
            .putString(KEY_MARKED_DOGS, json.encodeToString(next.values.toList()))
            .apply()
    }

    /**
     * A corrupt or older-format value must not stop the app from starting, so a
     * failed decode falls back to an empty list rather than throwing.
     */
    private fun loadMarkedDogs(): Map<String, MarkedDog> {
        val raw = prefs.getString(KEY_MARKED_DOGS, null) ?: return emptyMap()
        return runCatching {
            json.decodeFromString<List<MarkedDog>>(raw).associateBy { it.id }
        }.getOrDefault(emptyMap())
    }

    companion object {
        const val DEFAULT_WARNING_DISTANCE_METERS = 100
        const val MIN_WARNING_DISTANCE_METERS = 10
        const val MAX_WARNING_DISTANCE_METERS = 500

        /** Presets offered in the settings sheet. */
        val WARNING_DISTANCE_OPTIONS = listOf(30, 50, 100)

        private val DEFAULT_DOG_NAMES = listOf(
            "Biscuit", "Pepper", "Mochi", "Waffles", "Noodle", "Pickle", "Mango", "Ziggy",
        )

        private const val PREFS_NAME = "dogwalk_settings"
        private const val KEY_BACKEND_URL = "backend_url"
        private const val KEY_DISPLAY_NAME = "display_name"
        private const val KEY_WARNING_DISTANCE = "warning_distance_m"
        private const val KEY_MARKED_DOGS = "marked_dogs"
    }
}
