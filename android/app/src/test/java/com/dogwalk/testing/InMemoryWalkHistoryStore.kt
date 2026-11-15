package com.dogwalk.testing

import com.dogwalk.data.history.WalkHistoryStore
import com.dogwalk.data.history.withAdded
import com.dogwalk.data.history.without
import com.dogwalk.model.WalkSummary
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * In-memory stand-in for the SharedPreferences-backed history store.
 *
 * The real [com.dogwalk.data.history.PrefsWalkHistoryStore] needs a Context,
 * which does not exist on the JVM. The cap and ordering rules live in the
 * [WalkHistoryStore] companion, so the fake gets them for free.
 */
class InMemoryWalkHistoryStore : WalkHistoryStore {

    private val _walks = MutableStateFlow<List<WalkSummary>>(emptyList())
    override val walks: StateFlow<List<WalkSummary>> = _walks.asStateFlow()

    override fun add(summary: WalkSummary) {
        _walks.value = _walks.value.withAdded(summary)
    }

    override fun remove(id: String) {
        _walks.value = _walks.value.without(id)
    }
}
