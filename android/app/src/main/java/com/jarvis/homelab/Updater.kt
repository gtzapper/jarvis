package com.jarvis.homelab

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** A newer app version published as a GitHub release (tag "android-v1.2" with an .apk attached). */
data class AppUpdate(val version: String, val notes: String, val apkUrl: String, val size: Long, val sha256: String?, val page: String)

object Updater {
    const val REPO = "gtzapper/jarvis"

    /** Returns the newest release if it's newer than [current], else null. Throws on network trouble. */
    fun check(current: String): AppUpdate? {
        val c = URL("https://api.github.com/repos/$REPO/releases?per_page=20").openConnection() as HttpURLConnection
        val releases = try {
            c.connectTimeout = 10_000
            c.readTimeout = 15_000
            c.setRequestProperty("Accept", "application/vnd.github+json")
            c.setRequestProperty("User-Agent", "Jarvis-Android")
            if (c.responseCode == 403) throw IOException("GitHub is limiting requests right now. Try again later.")
            if (c.responseCode != 200) throw IOException("GitHub answered HTTP ${c.responseCode}")
            JSONArray(c.inputStream.bufferedReader().use { it.readText() })
        } finally {
            c.disconnect()
        }
        var best: AppUpdate? = null
        for (i in 0 until releases.length()) {
            val r = releases.getJSONObject(i)
            if (r.optBoolean("draft") || r.optBoolean("prerelease")) continue
            val tag = r.optString("tag_name")
            if (!tag.startsWith("android-v")) continue
            val version = tag.removePrefix("android-v")
            val assets = r.optJSONArray("assets") ?: continue
            val apk = (0 until assets.length()).map { assets.getJSONObject(it) }.firstOrNull { it.optString("name").endsWith(".apk") } ?: continue
            if (best != null && !isNewer(version, best.version)) continue
            best = AppUpdate(
                version = version,
                notes = r.optString("body").trim(),
                apkUrl = apk.optString("browser_download_url"),
                size = apk.optLong("size"),
                sha256 = apk.optString("digest").takeIf { it.startsWith("sha256:") }?.removePrefix("sha256:"),
                page = r.optString("html_url"),
            )
        }
        return best?.takeIf { isNewer(it.version, current) }
    }

    /** True if version [a] is newer than [b] (dotted numbers: 1.10 > 1.9). */
    fun isNewer(a: String, b: String): Boolean {
        val x = a.split('.', '-').map { it.toIntOrNull() ?: 0 }
        val y = b.split('.', '-').map { it.toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(x.size, y.size)) {
            val d = x.getOrElse(i) { 0 } - y.getOrElse(i) { 0 }
            if (d != 0) return d > 0
        }
        return false
    }

    /** Downloads the APK into the app's private cache, reporting progress 0..1, and checks its SHA-256 when GitHub gives one. */
    fun download(context: Context, u: AppUpdate, progress: (Float) -> Unit): File {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }  // old downloads
        val file = File(dir, "Jarvis-${u.version}.apk")
        val c = URL(u.apkUrl).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 15_000
            c.readTimeout = 30_000
            c.setRequestProperty("User-Agent", "Jarvis-Android")
            if (c.responseCode != 200) throw IOException("Download failed: HTTP ${c.responseCode}")
            val total = c.contentLengthLong.takeIf { it > 0 } ?: u.size
            val digest = MessageDigest.getInstance("SHA-256")
            var done = 0L
            c.inputStream.use { input ->
                file.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        digest.update(buf, 0, n)
                        done += n
                        if (total > 0) progress((done.toFloat() / total).coerceIn(0f, 1f))
                    }
                }
            }
            val hex = digest.digest().joinToString("") { "%02x".format(it) }
            if (u.sha256 != null && !hex.equals(u.sha256, ignoreCase = true)) {
                file.delete()
                throw IOException("The download was damaged (checksum mismatch). Try again.")
            }
        } finally {
            c.disconnect()
        }
        return file
    }

    /** Opens Android's installer. Returns false if the person first has to allow installs from this app. */
    fun install(context: Context, file: File): Boolean {
        if (Build.VERSION.SDK_INT >= 26 && !context.packageManager.canRequestPackageInstalls()) {
            context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return false
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
        context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        return true
    }
}
