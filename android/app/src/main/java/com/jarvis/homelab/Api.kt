package com.jarvis.homelab

import android.content.Context
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** Settings kept on the phone. Only the session cookie is stored, never the dashboard password. */
class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    var server: String
        get() = sp.getString("server", "") ?: ""
        set(v) = sp.edit().putString("server", v).apply()
    var cookie: String?
        get() = sp.getString("cookie", null)
        set(v) = sp.edit().putString("cookie", v).apply()
    var alerts: Boolean
        get() = sp.getBoolean("alerts", true)
        set(v) = sp.edit().putBoolean("alerts", v).apply()
    var speak: Boolean
        get() = sp.getBoolean("speak", true)
        set(v) = sp.edit().putBoolean("speak", v).apply()
    var knownProblems: Set<String>?
        get() = sp.getStringSet("known_problems", null)
        set(v) = sp.edit().putStringSet("known_problems", v).apply()
    var shareLocation: Boolean
        get() = sp.getBoolean("share_location", false)
        set(v) = sp.edit().putBoolean("share_location", v).apply()
    var phoneCalendar: Boolean
        get() = sp.getBoolean("phone_calendar", false)
        set(v) = sp.edit().putBoolean("phone_calendar", v).apply()
    var outboxLast: Int
        get() = sp.getInt("outbox_last", -1)
        set(v) = sp.edit().putInt("outbox_last", v).apply()
    var alarmCodes: Set<Int>
        get() = (sp.getStringSet("alarm_codes", emptySet()) ?: emptySet()).mapNotNull { it.toIntOrNull() }.toSet()
        set(v) = sp.edit().putStringSet("alarm_codes", v.map { it.toString() }.toSet()).apply()
    var recentReminders: Set<String>
        get() = sp.getStringSet("recent_reminders", emptySet()) ?: emptySet()
        set(v) = sp.edit().putStringSet("recent_reminders", v).apply()
    var handsFree: Boolean
        get() = sp.getBoolean("hands_free", false)
        set(v) = sp.edit().putBoolean("hands_free", v).apply()
    var theme: String
        get() = sp.getString("theme", "system") ?: "system"
        set(v) = sp.edit().putString("theme", v).apply()
    var autoUpdate: Boolean
        get() = sp.getBoolean("auto_update", true)
        set(v) = sp.edit().putBoolean("auto_update", v).apply()
    var lastUpdateCheck: Long
        get() = sp.getLong("last_update_check", 0)
        set(v) = sp.edit().putLong("last_update_check", v).apply()
    var skippedVersion: String?
        get() = sp.getString("skipped_version", null)
        set(v) = sp.edit().putString("skipped_version", v).apply()
    /** "admin" or "family" (what this account may do), learnt from /api/me after signing in. */
    var role: String
        get() = sp.getString("role", "admin") ?: "admin"
        set(v) = sp.edit().putString("role", v).apply()
    var username: String
        get() = sp.getString("username", "") ?: ""
        set(v) = sp.edit().putString("username", v).apply()
    var loginNagged: Boolean
        get() = sp.getBoolean("login_nagged", false)
        set(v) = sp.edit().putBoolean("login_nagged", v).apply()
}

class AuthNeeded : IOException("Jarvis wants you to log in again.")

/** Talks to the Jarvis dashboard's own API, the same one the web page uses. */
class Api(private val prefs: Prefs) {

    private fun call(path: String, method: String = "GET", body: String? = null,
                     type: String = "application/json", timeoutMs: Int = 30_000, raw: ByteArray? = null): Pair<Int, String> {
        val c = URL(prefs.server.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            c.instanceFollowRedirects = false
            c.connectTimeout = 8_000
            c.readTimeout = timeoutMs
            c.requestMethod = method
            c.setRequestProperty("Accept", "application/json")
            c.setRequestProperty("X-Jarvis", "1") // Jarvis's protection against cross-site requests
            prefs.cookie?.let { c.setRequestProperty("Cookie", it) }
            if (body != null || raw != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", type)
                c.outputStream.use { it.write(raw ?: body!!.toByteArray()) }
            }
            val code = c.responseCode
            lastLocation = c.getHeaderField("Location") ?: ""
            c.headerFields.entries.firstOrNull { it.key.equals("Set-Cookie", true) }?.value
                ?.firstOrNull { it.startsWith("session=") }
                ?.let { prefs.cookie = it.substringBefore(";") }
            val stream = if (code >= 400) c.errorStream else c.inputStream
            return code to (stream?.bufferedReader()?.use { it.readText() } ?: "")
        } finally {
            c.disconnect()
        }
    }

    private fun check(code: Int, text: String): JSONObject {
        if (code == 401) throw AuthNeeded()
        if (code !in 200..299) throw IOException("Jarvis answered HTTP $code")
        return try { JSONObject(text) } catch (e: Exception) { throw IOException("That doesn't look like Jarvis") }
    }

    fun get(path: String): JSONObject = call(path).let { (c, t) -> check(c, t) }

    fun post(path: String, json: JSONObject = JSONObject()): JSONObject =
        call(path, "POST", json.toString()).let { (c, t) -> check(c, t) }

    /** Point the app at a Jarvis and log in. Returns null on success, else a message for the person. */
    /** Where the last answer redirected to (the sign-in form says what went wrong this way). */
    @Volatile var lastLocation: String = ""

    fun connect(server: String, password: String, username: String = ""): String? {
        var url = server.trim().trimEnd('/')
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        prefs.server = url
        prefs.cookie = null
        val (code, _) = try { call("/api/mode") } catch (e: Exception) {
            return "Can't reach $url (${e.message ?: e.javaClass.simpleName}). Are you on your home network?"
        }
        if (code == 200) return null           // no dashboard password set
        if (code != 401) return "That address answered HTTP $code. Is it Jarvis?"
        if (password.isEmpty()) return "This Jarvis needs you to sign in. Enter your password above, or use single sign-on."
        call("/login", "POST", "username=" + URLEncoder.encode(username.trim(), "UTF-8") + "&password=" + URLEncoder.encode(password, "UTF-8"),
             "application/x-www-form-urlencoded")
        when {
            "step=code" in lastLocation -> { prefs.cookie = null; return "This account asks for an authenticator code. Tap Sign in above and enter it there." }
            "error=2fa_setup" in lastLocation -> return "From outside home this account needs an authenticator code, and it doesn't have one yet. Set it up at home (your account, bottom of the menu), or use single sign-on."
            "error=locked" in lastLocation -> return "Too many failed attempts. Wait 15 minutes."
            "error=sso" in lastLocation -> return "This account signs in with single sign-on. Tap Sign in above."
        }
        if (prefs.cookie == null || "error=" in lastLocation) return "Wrong username or password."
        return if (call("/api/mode").first == 200) null else "Logged in, but Jarvis still said no. Try again."
    }

    /** Upload a file (multipart form), e.g. a document or a receipt photo. */
    fun upload(path: String, filename: String, data: ByteArray, mime: String, fields: Map<String, String> = emptyMap()): JSONObject {
        val boundary = "----jarvis" + System.nanoTime()
        val out = java.io.ByteArrayOutputStream()
        for ((k, v) in fields) out.write("--$boundary\r\nContent-Disposition: form-data; name=\"$k\"\r\n\r\n$v\r\n".toByteArray())
        out.write("--$boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"${filename.replace("\"", "")}\"\r\nContent-Type: $mime\r\n\r\n".toByteArray())
        out.write(data)
        out.write("\r\n--$boundary--\r\n".toByteArray())
        val (c, t) = call(path, "POST", null, "multipart/form-data; boundary=$boundary", 300_000, out.toByteArray())
        return check(c, t)
    }

    /** Point the app at a Jarvis without signing in (for single sign-on, which happens in a web view). */
    fun setServer(server: String): String {
        var url = server.trim().trimEnd('/')
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        prefs.server = url
        prefs.cookie = null
        return url
    }

    fun logout() {
        try { call("/logout", "POST", "{}") } catch (_: Exception) {}
        prefs.cookie = null
    }
}
