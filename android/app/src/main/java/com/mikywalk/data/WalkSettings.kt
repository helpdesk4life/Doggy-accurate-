package com.mikywalk.data

import kotlinx.coroutines.flow.StateFlow

/**
 * The slice of [SettingsStore] the walk session reads.
 *
 * An interface rather than the store itself so
 * [com.mikywalk.data.repository.WalkRepository] can be exercised on the JVM: the
 * store takes a [android.content.Context] for SharedPreferences, which does not
 * exist outside a device or emulator.
 */
interface WalkSettings {
    val backendUrl: StateFlow<String>
    val displayName: StateFlow<String>
    val warningDistanceMeters: StateFlow<Int>
}
