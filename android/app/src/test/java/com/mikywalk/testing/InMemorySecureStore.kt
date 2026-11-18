package com.mikywalk.testing

import com.mikywalk.data.auth.SecureStore

/**
 * In-memory stand-in for the Keystore-backed store.
 *
 * The real [com.mikywalk.data.auth.SecurePrefs] needs the Android Keystore, which
 * does not exist on the JVM. What the tests care about is the state machine on
 * top of the store, not the cipher underneath it.
 */
class InMemorySecureStore : SecureStore {
    private val values = mutableMapOf<String, String>()

    override fun getString(key: String): String? = values[key]

    override fun putString(key: String, value: String) {
        values[key] = value
    }

    override fun clear() {
        values.clear()
    }
}
