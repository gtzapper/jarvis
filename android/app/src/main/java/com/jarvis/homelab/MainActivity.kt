package com.jarvis.homelab

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.Color

class MainActivity : ComponentActivity() {
    private val vm: JarvisViewModel by viewModels()
    private val startTab = mutableStateOf("chat")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        intent?.getStringExtra("tab")?.let { startTab.value = it }
        if (vm.prefs.server.isNotEmpty() && vm.prefs.alerts) AlertWorker.schedule(this)
        setContent {
            JarvisTheme {
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

// Same colours as the web dashboard.
private val Dark = darkColorScheme(
    primary = Color(0xFF3B82F6), onPrimary = Color.White,
    background = Color(0xFF0F1216), onBackground = Color(0xFFE6E8EB),
    surface = Color(0xFF171B21), onSurface = Color(0xFFE6E8EB),
    surfaceVariant = Color(0xFF1D222A), onSurfaceVariant = Color(0xFF98A2B3),
    surfaceContainer = Color(0xFF171B21), surfaceContainerHigh = Color(0xFF1D222A),
    outline = Color(0xFF2A313B), outlineVariant = Color(0xFF2A313B),
    error = Color(0xFFEF4444), errorContainer = Color(0xFF3B1515), onErrorContainer = Color(0xFFFCA5A5),
    tertiary = Color(0xFFF59E0B), tertiaryContainer = Color(0xFF3A2A0C), onTertiaryContainer = Color(0xFFFCD34D),
    secondaryContainer = Color(0xFF1E3A5F), onSecondaryContainer = Color(0xFFDBEAFE),
)
private val Light = lightColorScheme(
    primary = Color(0xFF2563EB), onPrimary = Color.White,
    background = Color(0xFFF4F5F7), onBackground = Color(0xFF1B1F24),
    surface = Color.White, onSurface = Color(0xFF1B1F24),
    surfaceVariant = Color(0xFFF9FAFB), onSurfaceVariant = Color(0xFF667085),
    surfaceContainer = Color.White, surfaceContainerHigh = Color(0xFFF9FAFB),
    outline = Color(0xFFE4E7EC), outlineVariant = Color(0xFFE4E7EC),
    error = Color(0xFFDC2626), errorContainer = Color(0xFFFEE2E2), onErrorContainer = Color(0xFF991B1B),
    tertiary = Color(0xFFD97706), tertiaryContainer = Color(0xFFFEF3C7), onTertiaryContainer = Color(0xFF92400E),
    secondaryContainer = Color(0xFFDBEAFE), onSecondaryContainer = Color(0xFF1E3A8A),
)

@Composable
fun JarvisTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}
