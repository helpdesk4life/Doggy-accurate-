package com.mikywalk

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mikywalk.data.auth.AuthState
import com.mikywalk.data.auth.AuthStore
import com.mikywalk.ui.auth.LoginScreen
import com.mikywalk.ui.map.MapScreen
import com.mikywalk.ui.theme.MikywalkTheme
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject
    lateinit var authStore: AuthStore

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            MikywalkTheme {
                val authState by authStore.state.collectAsStateWithLifecycle()
                when (authState) {
                    is AuthState.SignedIn -> MapScreen()
                    AuthState.SignedOut -> LoginScreen()
                }
            }
        }
    }
}
