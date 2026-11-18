package com.mikywalk.data.auth

/**
 * The key-value surface [AuthStore] persists through.
 *
 * An interface rather than [SecurePrefs] directly so the store can be exercised
 * on the JVM. The real implementation needs the Android Keystore, which does not
 * exist outside a device or emulator, and a test that cannot construct the store
 * cannot test the state transitions that matter.
 */
interface SecureStore {
    fun getString(key: String): String?
    fun putString(key: String, value: String)
    fun clear()
}
