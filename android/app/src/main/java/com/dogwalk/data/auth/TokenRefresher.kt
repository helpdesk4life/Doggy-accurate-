package com.dogwalk.data.auth

/**
 * The one auth call [com.dogwalk.data.repository.WalkRepository] makes on its
 * own, split out of [AuthApi] so the renewal path can be tested without an HTTP
 * server.
 *
 * What matters there is the branch — a refreshed pair, a refused refresh, or no
 * refresh token at all — not how the request is framed.
 */
interface TokenRefresher {
    suspend fun refresh(refreshToken: String): TokenResponse
}
