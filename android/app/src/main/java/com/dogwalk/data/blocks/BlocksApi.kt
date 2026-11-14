package com.dogwalk.data.blocks

import com.dogwalk.data.SettingsStore
import com.dogwalk.data.remote.httpBaseOf
import com.dogwalk.model.BlockedWalker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/** The name is only a label for the settings list; the block is the id. */
@Serializable
private data class BlockRequest(val name: String)

@Serializable
private data class ErrorResponse(val detail: String = "")

/** A block or unblock the server refused. */
class BlocksException(message: String) : Exception(message)

/**
 * The block list, which lives on the server rather than on the device.
 *
 * Friend and disliked are local because they only change how a marker looks. A
 * block has to stop the other walker receiving your bearing at all, so it is the
 * one social signal the backend has to know about — and the one that survives a
 * reinstall.
 */
@Singleton
class BlocksApi @Inject constructor(
    private val json: Json,
    private val settings: SettingsStore,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    private val jsonMedia = "application/json; charset=utf-8".toMediaType()

    suspend fun list(accessToken: String): List<BlockedWalker> =
        json.decodeFromString(send("GET", "/blocks", accessToken = accessToken))

    suspend fun block(accessToken: String, walkerId: String, name: String) {
        send(
            "POST",
            "/blocks/$walkerId",
            json.encodeToString(BlockRequest(name)),
            accessToken,
        )
    }

    suspend fun unblock(accessToken: String, walkerId: String) {
        send("DELETE", "/blocks/$walkerId", accessToken = accessToken)
    }

    private suspend fun send(
        method: String,
        path: String,
        body: String? = null,
        accessToken: String,
    ): String = withContext(Dispatchers.IO) {
        val builder = Request.Builder()
            .url(httpBaseOf(settings.backendUrl.value) + path)
            .header("Authorization", "Bearer $accessToken")

        when (method) {
            "GET" -> builder.get()
            "POST" -> builder.post(requireNotNull(body).toRequestBody(jsonMedia))
            "DELETE" -> builder.delete()
            else -> error("unsupported method $method")
        }

        client.newCall(builder.build()).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching {
                    json.decodeFromString<ErrorResponse>(text).detail
                }.getOrNull().orEmpty()
                throw BlocksException(detail.ifBlank { "HTTP ${response.code}" })
            }
            text
        }
    }
}
