package com.dogwalk.data.history

import com.dogwalk.model.WalkSummary
import com.dogwalk.testing.InMemoryWalkHistoryStore
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The list rules of [WalkHistoryStore]: newest first, capped, removable — the
 * rules both the prefs implementation and the in-memory fake share.
 */
class WalkHistoryStoreTest {

    private fun summary(id: String, startedAtMillis: Long = 0) = WalkSummary(
        id = id,
        startedAtMillis = startedAtMillis,
        endedAtMillis = startedAtMillis + 60_000,
        distanceMeters = 100.0,
        dogsMet = emptyList(),
    )

    @Test
    fun `withAdded prepends so the newest walk is first`() {
        val list = listOf(summary("old", 1_000)).withAdded(summary("new", 2_000))
        assertEquals(listOf("new", "old"), list.map { it.id })
    }

    @Test
    fun `withAdded caps the list and drops the oldest`() {
        var list = emptyList<WalkSummary>()
        repeat(WalkHistoryStore.MAX_ENTRIES + 10) { index ->
            list = list.withAdded(summary("walk-$index", startedAtMillis = index.toLong()))
        }
        assertEquals(WalkHistoryStore.MAX_ENTRIES, list.size)
        assertEquals("walk-${WalkHistoryStore.MAX_ENTRIES + 9}", list.first().id)
        assertEquals("walk-10", list.last().id)
    }

    @Test
    fun `without removes only the matching id`() {
        val list = listOf(summary("a"), summary("b"), summary("c")).without("b")
        assertEquals(listOf("a", "c"), list.map { it.id })
    }

    @Test
    fun `the in-memory store serves add and remove through its flow`() {
        val store = InMemoryWalkHistoryStore()
        store.add(summary("a"))
        store.add(summary("b"))
        assertEquals(listOf("b", "a"), store.walks.value.map { it.id })

        store.remove("b")
        assertEquals(listOf("a"), store.walks.value.map { it.id })
    }
}
