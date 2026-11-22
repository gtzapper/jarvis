package com.jarvis.homelab

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.isSystemInDarkTheme
import android.app.Activity
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

class MainActivity : ComponentActivity() {
    private val vm: JarvisViewModel by viewModels()
    private val startTab = mutableStateOf("chat")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        intent?.getStringExtra("tab")?.let { startTab.value = it }
        if (vm.prefs.server.isNotEmpty() && vm.prefs.alerts) AlertWorker.schedule(this)
        if (vm.prefs.server.isNotEmpty() && vm.prefs.shareLocation) LocationShare.schedule(this)
        setContent {
            JarvisTheme(vm.theme) {
                JarvisApp(vm, startTab)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra("tab")?.let { startTab.value = it }
    }

    override fun onResume() {
        super.onResume()
        if (vm.connected) vm.refreshAll()
    }
}

@Composable
fun JarvisTheme(themeId: String, content: @Composable () -> Unit) {
    val systemDark = isSystemInDarkTheme()
    val light = isLightTheme(themeId, systemDark)
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = light
                isAppearanceLightNavigationBars = light
            }
        }
    }
    MaterialTheme(colorScheme = schemeFor(themeId, systemDark), content = content)
}
