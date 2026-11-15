package com.dogwalk.di

import android.content.Context
import com.dogwalk.data.SettingsStore
import com.dogwalk.data.WalkSettings
import com.dogwalk.data.auth.AuthApi
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.data.auth.SecurePrefs
import com.dogwalk.data.auth.SecureStore
import com.dogwalk.data.auth.TokenRefresher
import com.dogwalk.data.history.PrefsWalkHistoryStore
import com.dogwalk.data.history.WalkHistoryStore
import com.dogwalk.data.remote.WalkSocketClient
import com.dogwalk.data.remote.WalkTransport
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.serialization.json.Json
import javax.inject.Qualifier
import javax.inject.Singleton

@Qualifier
@Retention(AnnotationRetention.BINARY)
annotation class ApplicationScope

@Module
@InstallIn(SingletonComponent::class)
object AppModule {

    @Provides
    @Singleton
    @ApplicationScope
    fun provideApplicationScope(): CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @Provides
    @Singleton
    fun provideJson(): Json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    /**
     * The Keystore-backed store [AuthStore] persists through. Bound here rather
     * than constructed inside the store so the store itself stays a plain class
     * that a JVM test can build with an in-memory fake.
     */
    @Provides
    @Singleton
    fun provideSecureStore(@ApplicationContext context: Context): SecureStore =
        SecurePrefs(context.getSharedPreferences(AuthStore.PREFS_NAME, Context.MODE_PRIVATE))

    /**
     * The seams [WalkRepository] tests through. The concrete classes already
     * implement them step for step, so the bindings are one line each.
     */
    @Provides
    @Singleton
    fun provideWalkSettings(store: SettingsStore): WalkSettings = store

    @Provides
    @Singleton
    fun provideWalkTransport(client: WalkSocketClient): WalkTransport = client

    @Provides
    @Singleton
    fun provideTokenRefresher(api: AuthApi): TokenRefresher = api

    /**
     * The walk-history seam [WalkRepository] persists through, bound here rather
     * than constructed inside it so a JVM test can substitute an in-memory store.
     */
    @Provides
    @Singleton
    fun provideWalkHistoryStore(store: PrefsWalkHistoryStore): WalkHistoryStore = store
}
