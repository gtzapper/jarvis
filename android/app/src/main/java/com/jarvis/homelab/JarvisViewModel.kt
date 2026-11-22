package com.jarvis.homelab

import android.app.Application
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
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

    var theme by mutableStateOf(prefs.theme)
        private set

    // app updates
    var update by mutableStateOf<AppUpdate?>(null)
        private set
    var showUpdateDialog by mutableStateOf(false)
    var updateStatus by mutableStateOf("")
        private set
    var downloadProgress by mutableStateOf<Float?>(null)
        private set
    val version: String = BuildConfig.VERSION_NAME
    private var downloaded: Pair<String, java.io.File>? = null

    private var tts: TextToSpeech? = null
    private var ttsReady = false

    init {
        // A first start with no password on Jarvis has no cookie, so try once.
        if (prefs.server.isNotEmpty()) refreshAll()
        if (prefs.autoUpdate && System.currentTimeMillis() - prefs.lastUpdateCheck > 24 * 3600_000L) checkForUpdate(manual = false)
    }

    fun chooseTheme(id: String) {
        theme = id
        prefs.theme = id
    }

    // ------------------------------------------------------------ app updates

    fun checkForUpdate(manual: Boolean) {
        updateStatus = "Checking..."
        viewModelScope.launch {
            val result = withContext(Dispatchers.IO) { runCatching { Updater.check(version) } }
            prefs.lastUpdateCheck = System.currentTimeMillis()
            result.onSuccess { u ->
                update = u
                updateStatus = if (u == null) "You're on the latest version ($version)." else "Version ${u.version} is available."
                // automatic checks don't nag about a version you chose to skip
                if (u != null && (manual || prefs.skippedVersion != u.version)) showUpdateDialog = true
            }.onFailure { e ->
                updateStatus = "Couldn't check for updates: ${e.message ?: e.javaClass.simpleName}"
            }
        }
    }

    fun skipUpdate() {
        update?.let { prefs.skippedVersion = it.version }
        showUpdateDialog = false
    }

    fun installUpdate() {
        val u = update ?: return
        showUpdateDialog = false
        val app = getApplication<Application>()
        downloaded?.takeIf { it.first == u.version && it.second.exists() }?.let { (_, f) ->
            // already downloaded: the person just came back from allowing installs
            updateStatus = if (Updater.install(app, f)) "Opening the installer..." else "Allow Jarvis to install apps, then tap Update again."
            return
        }
        downloadProgress = 0f
        updateStatus = "Downloading ${u.version}..."
        viewModelScope.launch {
            var last = 0f
            val file = withContext(Dispatchers.IO) {
                runCatching {
                    Updater.download(app, u) { p ->
                        if (p - last >= 0.02f || p >= 1f) { last = p; viewModelScope.launch { downloadProgress = p } }
                    }
                }
            }
            downloadProgress = null
            file.onSuccess { f ->
                downloaded = u.version to f
                updateStatus = if (Updater.install(app, f)) "Opening the installer..."
                    else "Allow Jarvis to install apps (the setting that just opened), then tap Update again."
            }.onFailure { e ->
                updateStatus = "Update failed: ${e.message ?: e.javaClass.simpleName}"
            }
        }
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
        loadMode(); loadStatus(); loadActivity(); loadPending()
    }

    /** Questions waiting for an answer that started elsewhere, e.g. "Hey Jarvis, reboot the NAS" from a listening device. */
    fun loadPending() = viewModelScope.launch {
        val d = io { api.get("/api/pending") } ?: return@launch
        val arr = d.optJSONArray("pending") ?: return@launch
        val shown = messages.mapNotNull { it.jobId }.toSet()
        for (i in 0 until arr.length()) {
            val job = arr.getJSONObject(i)
            val id = job.optString("id")
            if (id in shown) continue
            val origin = job.optString("origin").takeIf { it.isNotEmpty() && it != "null" }
            if (origin != null) messages += ChatMsg(true, "$origin heard: \"${job.optString("text")}\"")
            messages += ChatMsg(false, jobId = id)
            val index = messages.lastIndex
            launch { follow(index, job, false) }
        }
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
            } else follow(index, job, id in spokenJobs)
        }
    }

    private suspend fun follow(index: Int, first: JSONObject, spoken: Boolean) {
        var job = first
        if (spoken) spokenJobs += first.optString("id")
        while (true) {
            show(index, job)
            when (job.optString("status")) {
                "input" -> {                 // wait for the person's tap; answer() picks it up again
                    if (spoken) {
                        // approvals are always a tap, never a spoken "yes": a TV or someone else talking can't approve anything
                        voiceState = "waiting"
                        say(if (job.optJSONObject("prompt")?.optString("type") == "confirm") "I need your OK on the screen." else "Pick one on the screen.")
                    }
                    return
                }
                "done" -> {
                    busy = false
                    loadMode()               // "auto mode" typed in chat
                    if (spoken) afterReply(job.optString("reply"))
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

    // ------------------------------------------------------------ voice

    /** idle | listening | thinking | speaking | waiting (for a tap) */
    var voiceState by mutableStateOf("idle")
        private set
    var heard by mutableStateOf("")
        private set
    var handsFree by mutableStateOf(prefs.handsFree)
        private set
    private val spokenJobs = mutableSetOf<String>()
    private var recognizer: SpeechRecognizer? = null
    private val stopWords = Regex("^(stop|stop listening|that'?s all|thanks?( you)?( jarvis)?|goodbye|bye|cancel|never ?mind)[.!]?$", RegexOption.IGNORE_CASE)

    val canListenInApp: Boolean get() = SpeechRecognizer.isRecognitionAvailable(getApplication())

    fun toggleHandsFree(): Boolean {
        handsFree = !handsFree
        prefs.handsFree = handsFree
        if (!handsFree) stopVoice()
        return handsFree
    }

    /** Start listening (must be called on the main thread, with the microphone permission granted). */
    fun listen() {
        if (busy && voiceState != "speaking") return
        tts?.stop()
        recognizer?.destroy()
        val r = SpeechRecognizer.createSpeechRecognizer(getApplication())
        recognizer = r
        heard = ""
        voiceState = "listening"
        r.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() { voiceState = "thinking" }
            override fun onEvent(eventType: Int, params: Bundle?) {}
            override fun onPartialResults(partial: Bundle?) {
                partial?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.let { heard = it }
            }
            override fun onError(error: Int) {
                // silence or nothing understood: stop quietly; the person taps the mic to talk again
                voiceState = "idle"
                heard = if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) "Microphone permission is off" else ""
            }
            override fun onResults(results: Bundle?) {
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim().orEmpty()
                heard = ""
                when {
                    text.isEmpty() -> voiceState = "idle"
                    stopWords.matches(text) -> {
                        if (handsFree) toggleHandsFree()
                        voiceState = "idle"
                        say("Okay.")
                    }
                    else -> { voiceState = "thinking"; send(text, spoken = true) }
                }
            }
        })
        r.startListening(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag()))
    }

    fun stopVoice() {
        recognizer?.cancel()
        tts?.stop()
        voiceState = "idle"
        heard = ""
    }

    private fun afterReply(reply: String) {
        if (prefs.speak && reply.isNotBlank()) {
            voiceState = "speaking"
            say(reply) { viewModelScope.launch { if (handsFree && voiceState == "speaking") listen() else if (voiceState == "speaking") voiceState = "idle" } }
        } else if (handsFree) listen() else voiceState = "idle"
    }

    /** Speak [text]; [done] runs when it finishes (on any thread). */
    private fun say(text: String, done: (() -> Unit)? = null) {
        if (text.isBlank()) { done?.invoke(); return }
        val engine = tts
        fun go(e: TextToSpeech) {
            e.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {}
                override fun onDone(utteranceId: String?) { done?.invoke() }
                @Deprecated("Deprecated in Java")
                override fun onError(utteranceId: String?) { done?.invoke() }
            })
            e.speak(text.take(3500), TextToSpeech.QUEUE_FLUSH, null, "reply-" + System.nanoTime())
        }
        if (engine != null && ttsReady) { go(engine); return }
        tts = TextToSpeech(getApplication()) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            val e = tts
            if (ttsReady && e != null) { e.language = Locale.getDefault(); go(e) } else done?.invoke()
        }
    }

    fun stopSpeaking() = stopVoice()

    override fun onCleared() {
        recognizer?.destroy()
        tts?.shutdown()
    }
}
