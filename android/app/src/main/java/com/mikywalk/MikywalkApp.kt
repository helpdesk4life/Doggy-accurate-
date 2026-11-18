package com.mikywalk

import android.app.Application
import dagger.hilt.android.HiltAndroidApp
import org.maplibre.android.MapLibre

@HiltAndroidApp
class MikywalkApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // MapLibre must be initialised once, before any MapView is inflated.
        MapLibre.getInstance(this)
    }
}
