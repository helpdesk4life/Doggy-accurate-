package com.dogwalk

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dogwalk.data.auth.AuthState
import com.dogwalk.data.auth.AuthStore
import com.dogwalk.ui.auth.LoginScreen
import com.dogwalk.ui.map.MapScreen
import com.dogwalk.ui.theme.DogWalkTheme
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
            DogWalkTheme {
                val authState by authStore.state.collectAsStateWithLifecycle()
                when (authState) {
                    is AuthState.SignedIn -> MapScreen()
                    AuthState.SignedOut -> LoginScreen()
                }
            }
        }
    }
}
