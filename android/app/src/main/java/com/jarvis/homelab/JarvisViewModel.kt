package com.jarvis.homelab

import android.app.Application
import android.speech.tts.TextToSpeech
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/** A question Jarvis is waiting on: yes/no, or pick one of several. */
data class Prompt(val type: String, val question: String, val options: List<String>, val destructive: Boolean)

data class ChatMsg(
    val fromYou: Boolean,
    val text: String = "",
    val steps: List<String> = emptyList(),
    val working: Boolean = false,
    val prompt: Prompt? = null,
    val jobId: String? = null,
)

class JarvisViewModel(app: Application) : AndroidViewModel(app) {
    val prefs = Prefs(app)
    private val api = Api(prefs)

    var connected by mutableStateOf(prefs.server.isNotEmpty() && prefs.cookie != null)
        private set
    var connectError by mutableStateOf<String?>(null)
    var connecting by mutableStateOf(false)
        private set
    var banner by mutableStateOf<String?>(null)

    val messages = mutableStateListOf(
        ChatMsg(false, "Hi. Tell me what you want done and I'll work out the steps, showing you what I'm doing as I go. Destructive things always ask you first.")
    )
    var busy by mutableStateOf(false)
        private set
    var mode by mutableStateOf("auto")
        private set
    var modeText by mutableStateOf("")
        private set
    var status by mutableStateOf<JSONObject?>(null)
        private set
    var activity by mutableStateOf<List<String>>(emptyList())
        private set
    var loading by mutableStateOf(false)
        private set

    private var tts: TextToSpeech? = null
    private var ttsReady = false

    init {
        // A first start with no password on Jarvis has no cookie, so try once.
        if (prefs.server.isNotEmpty()) refreshAll()
    }

    // ------------------------------------------------------------ connecting

    fun connect(server: String, password: String) {
        connecting = true
        connectError = null
        viewModelScope.launch {
            val err = withContext(Dispatchers.IO) { api.connect(server, password) }
            connecting = false
            if (err == null) {
                connected = true
                prefs.loginNagged = false
                AlertWorker.schedule(getApplication())
                refreshAll()
            } else connectError = err
        }
    }

    fun logout() {
        viewModelScope.launch(Dispatchers.IO) { api.logout() }
        connected = false
    }

    /** Runs a call off the main thread; handles "log in again" and network trouble the same way everywhere. */
    private suspend fun <T> io(block: () -> T): T? = withContext(Dispatchers.IO) {
        try {
            block().also { withContext(Dispatchers.Main) { banner = null; connected = true } }
        } catch (e: AuthNeeded) {
            withContext(Dispatchers.Main) { connected = false; connectError = e.message }
            null
        } catch (e: Exception) {
            withContext(Dispatchers.Main) {
                banner = "Can't reach Jarvis: ${e.message ?: e.javaClass.simpleName}"
            }
            null
        }
    }

    fun refreshAll() {
        loadMode(); loadStatus(); loadActivity()
    }

    // ------------------------------------------------------------ mode

    fun loadMode() = viewModelScope.launch { io { api.get("/api/mode") }?.let(::applyMode) }

    fun setMode(m: String) = viewModelScope.launch {
        io { api.post("/api/mode", JSONObject().put("mode", m)) }?.let(::applyMode)
    }

    private fun applyMode(j: JSONObject) {
        mode = j.optString("mode", mode)
        modeText = j.optString("description").substringAfter(": ")
    }

    // ------------------------------------------------------------ status and activity

    fun loadStatus() = viewModelScope.launch {
        loading = true
        io { api.get("/api/status") }?.let { status = it }
        loading = false
    }

    fun refreshNow() = viewModelScope.launch {
        loading = true
        io { api.post("/api/refresh") }
        delay(2500)  // give the checks a moment
        io { api.get("/api/status") }?.let { status = it }
        loading = false
    }

    fun loadActivity() = viewModelScope.launch {
        io { api.get("/api/activity") }?.let { j ->
            val arr = j.optJSONArray("lines") ?: JSONArray()
            activity = List(arr.length()) { arr.getString(it) }
        }
    }

    // ------------------------------------------------------------ chat

    fun send(text: String, spoken: Boolean = false) {
        val t = text.trim()
        if (t.isEmpty() || busy) return
        messages += ChatMsg(true, t)
        messages += ChatMsg(false, working = true)
        val index = messages.lastIndex
        busy = true
        viewModelScope.launch {
            val job = io { api.post("/api/chat", JSONObject().put("text", t)) }
            if (job == null) {
                put(index, ChatMsg(false, banner ?: "I couldn't send that."))
                busy = false
                return@launch
            }
            follow(index, job, spoken)
        }
    }

    /** Answer the question in message [index]: true/false for yes/no, an option number or null for pick-one. */
    fun answer(index: Int, value: Any?) {
        val msg = messages.getOrNull(index) ?: return
        val id = msg.jobId ?: return
        put(index, msg.copy(prompt = null, working = true))
        viewModelScope.launch {
            val job = io { api.post("/api/chat/$id/answer", JSONObject().put("answer", value ?: JSONObject.NULL)) }
            if (job == null) {
                put(index, ChatMsg(false, "I couldn't send your answer."))
                busy = false
            } else follow(index, job, false)
        }
    }

    private suspend fun follow(index: Int, first: JSONObject, spoken: Boolean) {
        var job = first
        while (true) {
            show(index, job)
            when (job.optString("status")) {
                "input" -> return            // wait for the person's answer; answer() picks it up again
                "done" -> {
                    busy = false
                    loadMode()               // "auto mode" typed in chat
                    if (spoken && prefs.speak) say(job.optString("reply"))
                    return
                }
            }
            delay(800)
            job = io { api.get("/api/chat/${job.getString("id")}") } ?: run {
                put(index, (messages.getOrNull(index) ?: ChatMsg(false)).copy(working = false, prompt = null,
                    text = "Lost contact with Jarvis while it was working."))
                busy = false
                return
            }
        }
    }

    private fun show(index: Int, job: JSONObject) {
        val progress = job.optJSONArray("progress") ?: JSONArray()
        val steps = List(progress.length()) { progress.getString(it) }
        val prompt = job.optJSONObject("prompt")?.let { p ->
            val q = p.optString("question")
            val opts = p.optJSONArray("options")
            Prompt(
                type = p.optString("type"),
                question = q.removePrefix("[destructive] "),
                options = if (opts != null) List(opts.length()) { opts.getString(it) } else emptyList(),
                destructive = q.startsWith("[destructive] "),
            )
        }
        val done = job.optString("status") == "done"
        put(index, ChatMsg(
            fromYou = false,
            text = if (done) job.optString("reply") else "",
            steps = steps,
            working = job.optString("status") == "running",
            prompt = prompt,
            jobId = job.optString("id"),
        ))
    }

    private fun put(index: Int, msg: ChatMsg) {
        if (index in messages.indices) messages[index] = msg
    }

    fun newChat() {
        messages.clear()
        messages += ChatMsg(false, "Fresh start. What can I do?")
        viewModelScope.launch { io { api.post("/api/chat/reset") } }
    }

    // ------------------------------------------------------------ speaking replies

    private fun say(text: String) {
        if (text.isBlank()) return
        val engine = tts
        if (engine != null && ttsReady) {
            engine.speak(text.take(3500), TextToSpeech.QUEUE_FLUSH, null, "reply")
            return
        }
        tts = TextToSpeech(getApplication()) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = Locale.getDefault()
                tts?.speak(text.take(3500), TextToSpeech.QUEUE_FLUSH, null, "reply")
            }
        }
    }

    fun stopSpeaking() { tts?.stop() }

    override fun onCleared() {
        tts?.shutdown()
    }
}
