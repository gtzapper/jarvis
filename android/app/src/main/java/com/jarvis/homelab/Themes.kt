package com.jarvis.homelab

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color

/** The same themes as the web dashboard. "system" follows the phone's light/dark setting. */
data class AppTheme(val id: String, val name: String, val bg: Color, val panel: Color, val accent: Color)

val THEMES = listOf(
    AppTheme("system", "System", Color(0xFF0F1216), Color(0xFFF4F5F7), Color(0xFF3B82F6)),
    AppTheme("light", "Light", Color(0xFFF4F5F7), Color(0xFFFFFFFF), Color(0xFF2563EB)),
    AppTheme("dark", "Dark", Color(0xFF0F1216), Color(0xFF171B21), Color(0xFF3B82F6)),
    AppTheme("midnight", "Midnight", Color(0xFF070B14), Color(0xFF0D1424), Color(0xFF22D3EE)),
    AppTheme("ember", "Ember", Color(0xFF140F0C), Color(0xFF1C1612), Color(0xFFF97316)),
    AppTheme("forest", "Forest", Color(0xFF0C130F), Color(0xFF121B16), Color(0xFF22C55E)),
    AppTheme("contrast", "High contrast", Color(0xFF000000), Color(0xFF000000), Color(0xFFFFD400)),
)

private fun dark(
    bg: Long, panel: Long, panel2: Long, text: Long, muted: Long, line: Long, accent: Long, onAccent: Long,
    crit: Long, critBg: Long, onCritBg: Long, warn: Long, warnBg: Long, onWarnBg: Long, accentBg: Long, onAccentBg: Long,
) = darkColorScheme(
    primary = Color(accent), onPrimary = Color(onAccent),
    background = Color(bg), onBackground = Color(text),
    surface = Color(panel), onSurface = Color(text),
    surfaceVariant = Color(panel2), onSurfaceVariant = Color(muted),
    surfaceContainer = Color(panel), surfaceContainerHigh = Color(panel2), surfaceContainerLow = Color(panel),
    surfaceContainerHighest = Color(panel2), surfaceContainerLowest = Color(bg),
    outline = Color(line), outlineVariant = Color(line),
    error = Color(crit), errorContainer = Color(critBg), onErrorContainer = Color(onCritBg),
    tertiary = Color(warn), tertiaryContainer = Color(warnBg), onTertiaryContainer = Color(onWarnBg),
    secondaryContainer = Color(accentBg), onSecondaryContainer = Color(onAccentBg),
)

val LightScheme: ColorScheme = lightColorScheme(
    primary = Color(0xFF2563EB), onPrimary = Color.White,
    background = Color(0xFFF4F5F7), onBackground = Color(0xFF1B1F24),
    surface = Color.White, onSurface = Color(0xFF1B1F24),
    surfaceVariant = Color(0xFFF9FAFB), onSurfaceVariant = Color(0xFF667085),
    surfaceContainer = Color.White, surfaceContainerHigh = Color(0xFFF9FAFB), surfaceContainerLow = Color.White,
    surfaceContainerHighest = Color(0xFFF1F3F6), surfaceContainerLowest = Color(0xFFF4F5F7),
    outline = Color(0xFFE4E7EC), outlineVariant = Color(0xFFE4E7EC),
    error = Color(0xFFDC2626), errorContainer = Color(0xFFFEE2E2), onErrorContainer = Color(0xFF991B1B),
    tertiary = Color(0xFFD97706), tertiaryContainer = Color(0xFFFEF3C7), onTertiaryContainer = Color(0xFF92400E),
    secondaryContainer = Color(0xFFDBEAFE), onSecondaryContainer = Color(0xFF1E3A8A),
)

val DarkScheme = dark(0xFF0F1216, 0xFF171B21, 0xFF1D222A, 0xFFE6E8EB, 0xFF98A2B3, 0xFF2A313B, 0xFF3B82F6, 0xFFFFFFFF,
    0xFFEF4444, 0xFF3B1515, 0xFFFCA5A5, 0xFFF59E0B, 0xFF3A2A0C, 0xFFFCD34D, 0xFF1E3A5F, 0xFFDBEAFE)
private val MidnightScheme = dark(0xFF070B14, 0xFF0D1424, 0xFF121B30, 0xFFE2E8F0, 0xFF8B9BB4, 0xFF1E2A44, 0xFF22D3EE, 0xFF04131A,
    0xFFF87171, 0xFF34121A, 0xFFFCA5A5, 0xFFFBBF24, 0xFF2E240B, 0xFFFDE68A, 0xFF0E3A47, 0xFFCFFAFE)
private val EmberScheme = dark(0xFF140F0C, 0xFF1C1612, 0xFF241C17, 0xFFF3ECE6, 0xFFA8998C, 0xFF362A22, 0xFFF97316, 0xFF1A0B02,
    0xFFF87171, 0xFF3A1513, 0xFFFCA5A5, 0xFFFACC15, 0xFF33290A, 0xFFFEF08A, 0xFF4A2410, 0xFFFFEDD5)
private val ForestScheme = dark(0xFF0C130F, 0xFF121B16, 0xFF18241D, 0xFFE5EFE8, 0xFF93A89B, 0xFF24352B, 0xFF22C55E, 0xFF04140A,
    0xFFF87171, 0xFF361616, 0xFFFCA5A5, 0xFFFBBF24, 0xFF30280C, 0xFFFDE68A, 0xFF12381F, 0xFFDCFCE7)
private val ContrastScheme = dark(0xFF000000, 0xFF000000, 0xFF121212, 0xFFFFFFFF, 0xFFD4D4D4, 0xFF8A8A8A, 0xFFFFD400, 0xFF000000,
    0xFFFF5252, 0xFF4A0000, 0xFFFFCDD2, 0xFFFFD400, 0xFF3D3300, 0xFFFFF59D, 0xFF3D3300, 0xFFFFF59D)

fun schemeFor(id: String, systemDark: Boolean): ColorScheme = when (id) {
    "light" -> LightScheme
    "dark" -> DarkScheme
    "midnight" -> MidnightScheme
    "ember" -> EmberScheme
    "forest" -> ForestScheme
    "contrast" -> ContrastScheme
    else -> if (systemDark) DarkScheme else LightScheme
}

fun isLightTheme(id: String, systemDark: Boolean) = id == "light" || (id == "system" && !systemDark)
