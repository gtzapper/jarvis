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
    var loginNagged: Boolean
        get() = sp.getBoolean("login_nagged", false)
        set(v) = sp.edit().putBoolean("login_nagged", v).apply()
}

class AuthNeeded : IOException("Jarvis wants you to log in again.")

/** Talks to the Jarvis dashboard's own API, the same one the web page uses. */
class Api(private val prefs: Prefs) {

    private fun call(path: String, method: String = "GET", body: String? = null,
                     type: String = "application/json", timeoutMs: Int = 30_000): Pair<Int, String> {
        val c = URL(prefs.server.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            c.instanceFollowRedirects = false
            c.connectTimeout = 8_000
            c.readTimeout = timeoutMs
            c.requestMethod = method
            c.setRequestProperty("Accept", "application/json")
            c.setRequestProperty("X-Jarvis", "1") // Jarvis's protection against cross-site requests
            prefs.cookie?.let { c.setRequestProperty("Cookie", it) }
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", type)
                c.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = c.responseCode
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
    fun connect(server: String, password: String): String? {
        var url = server.trim().trimEnd('/')
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        prefs.server = url
        prefs.cookie = null
        val (code, _) = try { call("/api/mode") } catch (e: Exception) {
            return "Can't reach $url (${e.message ?: e.javaClass.simpleName}). Are you on your home network?"
        }
        if (code == 200) return null           // no dashboard password set
        if (code != 401) return "That address answered HTTP $code. Is it Jarvis?"
        if (password.isEmpty()) return "This Jarvis has a dashboard password. Enter it above."
        call("/login", "POST", "password=" + URLEncoder.encode(password, "UTF-8"),
             "application/x-www-form-urlencoded")
        if (prefs.cookie == null) return "Wrong password."
        return if (call("/api/mode").first == 200) null else "Logged in, but Jarvis still said no. Try again."
    }

    fun logout() {
        try { call("/logout", "POST", "{}") } catch (_: Exception) {}
        prefs.cookie = null
    }
}
