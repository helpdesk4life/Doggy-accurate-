package com.mikywalk.data.history

import android.content.Context
import com.mikywalk.model.WalkSummary
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.json.Json
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Walk history in SharedPreferences as one JSON list, newest first and capped at
 * [WalkHistoryStore.MAX_ENTRIES].
 *
 * Local on purpose, like the marked-dogs list: the stats are derived from what
 * the app already received, the server never sees them, and a reinstall loses
 * them — the same trade as the social graph. Only aggregates are stored, never
 * a route, so the file is not a position log.
 */
@Singleton
class PrefsWalkHistoryStore @Inject constructor(
    @ApplicationContext context: Context,
    private val json: Json,
) : WalkHistoryStore {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _walks = MutableStateFlow(load())
    override val walks: StateFlow<List<WalkSummary>> = _walks.asStateFlow()

    override fun add(summary: WalkSummary) {
        val next = _walks.value.withAdded(summary)
        _walks.value = next
        persist(next)
    }

    override fun remove(id: String) {
        val next = _walks.value.without(id)
        if (next.size != _walks.value.size) {
            _walks.value = next
            persist(next)
        }
    }

    private fun persist(walks: List<WalkSummary>) {
        prefs.edit()
            .putString(KEY_WALKS, json.encodeToString(walks))
            .apply()
    }

    /**
     * A corrupt or older-format value must not stop the app from starting, so a
     * failed decode falls back to an empty list rather than throwing.
     */
    private fun load(): List<WalkSummary> {
        val raw = prefs.getString(KEY_WALKS, null) ?: return emptyList()
        return runCatching { json.decodeFromString<List<WalkSummary>>(raw) }
            .getOrDefault(emptyList())
    }

    companion object {
        private const val PREFS_NAME = "mikywalk_history"
        private const val KEY_WALKS = "walks"
    }
}
