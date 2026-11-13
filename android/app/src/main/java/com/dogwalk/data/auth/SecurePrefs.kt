package com.dogwalk.data.auth

import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * A `SharedPreferences` wrapper that encrypts values with a key held in the
 * Android Keystore.
 *
 * The key is generated inside the Keystore, marked non-exportable, and only
 * usable through the `Cipher` API. A copy of the preferences file — from an
 * auto-backup, a rooted device, or `adb pull` — is ciphertext with no way to
 * decrypt it off-device.
 *
 * AES-GCM is authenticated, so a tampered value fails to decrypt rather than
 * silently returning garbage. When that happens the whole store is dropped and
 * the user signs in again, which is the only safe response.
 *
 * This is deliberately hand-rolled rather than `EncryptedSharedPreferences`:
 * that library is deprecated, and the whole job here is one key and one cipher.
 */
class SecurePrefs(
    private val prefs: SharedPreferences,
    private val keyAlias: String = DEFAULT_KEY_ALIAS,
) : SecureStore {
    private val keyStore: KeyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    private val key: SecretKey by lazy { loadOrCreateKey() }

    override fun getString(key: String): String? {
        val stored = prefs.getString(key, null) ?: return null
        val plaintext = decrypt(stored)
        if (plaintext == null) {
            // A value we cannot read is worse than no value: drop everything and
            // make the user sign in again.
            Log.w(TAG, "dropping the encrypted store: $key could not be decrypted")
            clear()
        }
        return plaintext
    }

    override fun putString(key: String, value: String) {
        prefs.edit().putString(key, encrypt(value)).apply()
    }

    override fun clear() {
        prefs.edit().clear().apply()
    }

    private fun loadOrCreateKey(): SecretKey {
        val existing = runCatching {
            (keyStore.getEntry(keyAlias, null) as? KeyStore.SecretKeyEntry)?.secretKey
        }.getOrNull()
        if (existing != null) return existing

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                keyAlias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(KEY_SIZE_BITS)
                .build(),
        )
        return generator.generateKey()
    }

    private fun encrypt(plaintext: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(cipher.iv, Base64.NO_WRAP) +
            SEPARATOR +
            Base64.encodeToString(ciphertext, Base64.NO_WRAP)
    }

    private fun decrypt(stored: String): String? {
        val parts = stored.split(SEPARATOR, limit = 2)
        if (parts.size != 2) return null

        return runCatching {
            val iv = Base64.decode(parts[0], Base64.NO_WRAP)
            val ciphertext = Base64.decode(parts[1], Base64.NO_WRAP)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_BITS, iv))
            String(cipher.doFinal(ciphertext), Charsets.UTF_8)
        }.getOrNull()
    }

    private companion object {
        const val TAG = "SecurePrefs"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val DEFAULT_KEY_ALIAS = "dogwalk_auth_key"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val KEY_SIZE_BITS = 256
        const val TAG_BITS = 128
        const val SEPARATOR = ":"
    }
}
