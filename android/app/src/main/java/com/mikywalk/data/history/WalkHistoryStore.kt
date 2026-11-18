package com.mikywalk.data.history

import com.mikywalk.model.WalkSummary
import kotlinx.coroutines.flow.StateFlow

/**
 * The finished walks, newest first.
 *
 * An interface rather than the concrete store so [com.mikywalk.data.repository.WalkRepository]
 * can be exercised on the JVM: the real implementation persists through
 * SharedPreferences, which needs a [android.content.Context]. The tests use an
 * in-memory implementation, the same split [com.mikywalk.data.auth.SecureStore]
 * makes for the auth tokens.
 */
interface WalkHistoryStore {

    val walks: StateFlow<List<WalkSummary>>

    /** Prepend a finished walk and persist; the oldest entries drop off the end. */
    fun add(summary: WalkSummary)

    fun remove(id: String)

    companion object {

        /** Hard ceiling on the stored list, so the prefs file stays small forever. */
        const val MAX_ENTRIES = 100
    }
}

/** Newest-first insert with the cap applied, dropping the oldest entries. */
fun List<WalkSummary>.withAdded(
    summary: WalkSummary,
    maxEntries: Int = WalkHistoryStore.MAX_ENTRIES,
): List<WalkSummary> = (listOf(summary) + this).take(maxEntries)

fun List<WalkSummary>.without(id: String): List<WalkSummary> =
    filterNot { it.id == id }
