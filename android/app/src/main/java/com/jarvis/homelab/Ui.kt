package com.jarvis.homelab

import android.Manifest
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.speech.RecognizerIntent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.launch
import org.json.JSONObject

private val QUICK = listOf("status", "media status", "downloads", "torrents", "who's watching", "upcoming", "requests", "hosts", "check updates on all")
private val MODES = listOf("ask" to "Ask", "auto" to "Auto", "read-only" to "Read-only")

@Composable
fun JarvisApp(vm: JarvisViewModel, tab: MutableState<String>) {
    val context = LocalContext.current
    val notifyPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(vm.connected) {
        if (vm.connected && vm.prefs.alerts && Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notifyPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    if (vm.showUpdateDialog) UpdateDialog(vm)
    if (!vm.connected) {
        ConnectScreen(vm)
        return
    }
    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        bottomBar = {
            NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                listOf(
                    Triple("chat", "Chat", ChatIcon), Triple("planner", "Planner", Icons.Filled.DateRange), Triple("status", "Status", Icons.Filled.Home),
                    Triple("activity", "Activity", Icons.AutoMirrored.Filled.List), Triple("settings", "Settings", Icons.Filled.Settings),
                ).filter { !vm.isFamily || it.first in setOf("chat", "planner", "settings") }.forEach { (key, label, icon) ->
                    NavigationBarItem(
                        selected = tab.value == key,
                        onClick = { tab.value = key; if (key == "activity") vm.loadActivity(); if (key == "status") vm.loadStatus(); if (key == "planner") vm.loadPlanner() },
                        icon = { Icon(icon, contentDescription = null) },
                        label = { Text(label) },
                    )
                }
            }
        },
    ) { inner ->
        Column(Modifier.fillMaxSize().padding(inner).consumeWindowInsets(inner)) {
            vm.downloadProgress?.let { p ->
                LinearProgressIndicator(progress = { p }, modifier = Modifier.fillMaxWidth())
            }
            vm.banner?.let {
                Text(it, color = MaterialTheme.colorScheme.onErrorContainer, fontSize = 13.sp,
                    modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.errorContainer).padding(10.dp))
            }
            when (if (vm.isFamily && tab.value in setOf("status", "activity")) "chat" else tab.value) {
                "status" -> StatusScreen(vm)
                "planner" -> PlannerScreen(vm)
                "activity" -> ActivityScreen(vm)
                "settings" -> SettingsScreen(vm)
                else -> ChatScreen(vm)
            }
        }
    }
}

// ---------------------------------------------------------------- connect

@Composable
fun ConnectScreen(vm: JarvisViewModel) {
    var server by rememberSaveable { mutableStateOf(vm.prefs.server) }
    var password by rememberSaveable { mutableStateOf("") }
    var username by rememberSaveable { mutableStateOf(vm.prefs.username) }
    var sso by remember { mutableStateOf<String?>(null) }  // the server address while the sign-on web view is open
    sso?.let { url -> SsoScreen(url, onDone = { cookie -> sso = null; vm.ssoDone(cookie) }, onCancel = { sso = null }); return }
    Column(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).statusBarsPadding().imePadding().padding(24.dp),
        verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Image(painterResource(R.mipmap.ic_launcher), null, Modifier.size(88.dp))
        Spacer(Modifier.height(14.dp))
        Text("Jarvis", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
        Text("Connect to your Jarvis", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(server, { server = it }, label = { Text("Jarvis address") },
            placeholder = { Text("http://10.0.20.5:8080") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next))
        Spacer(Modifier.height(14.dp))
        // single sign-on first: the Jarvis sign-in page (with every provider's button) opens inside the app
        Button({ sso = vm.api.setServer(server) }, enabled = server.isNotBlank() && !vm.connecting, modifier = Modifier.fillMaxWidth().height(48.dp)) {
            Text("Sign in")
        }
        Text("Opens your Jarvis sign-in page: single sign-on (Authentik, Google...) or your password.",
            color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, modifier = Modifier.padding(top = 4.dp))
        var showPassword by rememberSaveable { mutableStateOf(false) }
        TextButton({ showPassword = !showPassword }, modifier = Modifier.padding(top = 4.dp)) {
            Text(if (showPassword) "Hide username and password" else "Or type a username and password here")
        }
        if (showPassword) {
            OutlinedTextField(username, { username = it }, label = { Text("Username") }, placeholder = { Text("Blank = admin") }, singleLine = true,
                modifier = Modifier.fillMaxWidth(), keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next, autoCorrectEnabled = false))
            Spacer(Modifier.height(10.dp))
            OutlinedTextField(password, { password = it }, label = { Text("Password") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Go),
                keyboardActions = KeyboardActions(onGo = { vm.connect(server, password, username) }))
            Spacer(Modifier.height(12.dp))
            OutlinedButton({ vm.connect(server, password, username) }, enabled = server.isNotBlank() && !vm.connecting, modifier = Modifier.fillMaxWidth().height(48.dp)) {
                if (vm.connecting) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp) else Text("Connect")
            }
        }
        vm.connectError?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = MaterialTheme.colorScheme.error, fontSize = 14.sp)
        }
        Spacer(Modifier.height(20.dp))
        Text("Use the address you open the dashboard with. Your phone needs to be on your home network, or connected to it by VPN. " +
            "Your password is never stored; the app keeps the sign-in like a browser does. Got an invitation email? Open its link first to create your account.",
            color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
    }
}

// ---------------------------------------------------------------- chat

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(vm: JarvisViewModel) {
    var input by rememberSaveable { mutableStateOf("") }
    val list = rememberLazyListState()
    val lastMsg = vm.messages.lastOrNull()
    LaunchedEffect(vm.messages.size, lastMsg?.steps?.size, lastMsg?.prompt, lastMsg?.text) {
        if (vm.messages.isNotEmpty()) list.animateScrollToItem(vm.messages.lastIndex)
    }
    val voice = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
        if (res.resultCode == Activity.RESULT_OK) {
            res.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()?.let { vm.send(it, spoken = true) }
        }
    }
    val context = LocalContext.current
    fun dialogVoice() {  // fallback: Google's voice dialog when in-app recognition isn't available
        vm.stopSpeaking()
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PROMPT, "Tell Jarvis what to do")
        try { voice.launch(intent) } catch (e: ActivityNotFoundException) {
            android.widget.Toast.makeText(context, "No voice input app on this phone", android.widget.Toast.LENGTH_SHORT).show()
        }
    }
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) vm.listen()
        else android.widget.Toast.makeText(context, "Jarvis needs the microphone to hear you", android.widget.Toast.LENGTH_SHORT).show()
    }
    fun startListening() {
        if (!vm.canListenInApp) { dialogVoice(); return }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) vm.listen()
        else micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

    Column(Modifier.fillMaxSize().imePadding()) {
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = list, contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            itemsIndexed(vm.messages) { i, m -> MessageBubble(m) { value -> if (value is Pair<*, *>) vm.answer(i, value.first, value.second as String) else vm.answer(i, value) } }
        }
        LazyRow(contentPadding = PaddingValues(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            item { AssistChip(onClick = { vm.newChat() }, enabled = !vm.busy, label = { Text("new chat") }) }
            items(QUICK) { q -> AssistChip(onClick = { vm.send(q) }, enabled = !vm.busy, label = { Text(q) }) }
        }
        SingleChoiceSegmentedButtonRow(Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
            MODES.forEachIndexed { i, (key, label) ->
                SegmentedButton(selected = vm.mode == key, onClick = { vm.setMode(key) },
                    shape = SegmentedButtonDefaults.itemShape(i, MODES.size), label = { Text(label, fontSize = 13.sp) })
            }
        }
        if (vm.modeText.isNotEmpty()) {
            Text(vm.modeText, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 14.dp))
        }
        if (vm.voiceState != "idle" || vm.handsFree || vm.heard.isNotEmpty()) VoiceBar(vm)
        Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(input, { input = it }, placeholder = { Text("Tell Jarvis what to do...") },
                modifier = Modifier.weight(1f), maxLines = 4, shape = RoundedCornerShape(24.dp),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { vm.send(input); input = "" }))
            val listening = vm.voiceState == "listening"
            IconButton(onClick = { if (listening) vm.stopVoice() else startListening() }, enabled = !vm.busy || listening) {
                Icon(MicIcon, if (listening) "Stop listening" else "Speak",
                    tint = if (listening) MaterialTheme.colorScheme.error else LocalContentColor.current)
            }
            IconButton(onClick = { if (vm.toggleHandsFree() && vm.voiceState == "idle" && !vm.busy) startListening() }) {
                Icon(HeadsetIcon, if (vm.handsFree) "Turn off hands-free" else "Hands-free conversation",
                    tint = if (vm.handsFree) MaterialTheme.colorScheme.primary else LocalContentColor.current)
            }
            IconButton(onClick = { vm.send(input); input = "" }, enabled = input.isNotBlank() && !vm.busy) {
                Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = if (input.isNotBlank()) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
fun VoiceBar(vm: JarvisViewModel) {
    val c = MaterialTheme.colorScheme
    val text = when (vm.voiceState) {
        "listening" -> if (vm.heard.isNotEmpty()) vm.heard else "Listening..."
        "thinking" -> "Thinking..."
        "speaking" -> "Speaking..."
        "waiting" -> "Waiting for your tap on the question above"
        else -> vm.heard.ifEmpty { "Hands-free is on. Tap the mic to talk; after each answer I'll listen again." }
    }
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp)
            .background(c.secondaryContainer, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (vm.voiceState in listOf("listening", "thinking", "speaking")) {
            CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp, color = c.primary)
            Spacer(Modifier.width(10.dp))
        }
        Text(text, fontSize = 13.sp, color = c.onSecondaryContainer, modifier = Modifier.weight(1f), maxLines = 3)
        TextButton({ if (vm.handsFree) vm.toggleHandsFree(); vm.stopVoice() }) { Text("Stop") }
    }
}

@Composable
fun MessageBubble(m: ChatMsg, onAnswer: (Any?) -> Unit) {
    val c = MaterialTheme.colorScheme
    Box(Modifier.fillMaxWidth(), contentAlignment = if (m.fromYou) Alignment.CenterEnd else Alignment.CenterStart) {
        Column(
            Modifier.widthIn(max = 330.dp)
                .background(if (m.fromYou) c.primary else c.surface, RoundedCornerShape(16.dp))
                .then(if (m.fromYou) Modifier else Modifier.border(1.dp, c.outline, RoundedCornerShape(16.dp)))
                .padding(horizontal = 13.dp, vertical = 10.dp),
        ) {
            m.steps.forEach { s ->
                val sub = s.startsWith("  ")
                Text((if (sub) "    " else "• ") + s.trim(), fontSize = 13.sp, color = c.onSurfaceVariant, lineHeight = 17.sp)
            }
            if (m.working) {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = if (m.steps.isEmpty()) 0.dp else 4.dp)) {
                    CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp))
                    Text("working...", fontSize = 13.sp, color = c.onSurfaceVariant)
                }
            }
            m.prompt?.let { PromptCard(it, onAnswer) }
            if (m.text.isNotEmpty()) {
                if (m.steps.isNotEmpty()) Spacer(Modifier.height(6.dp))
                Text(m.text, color = if (m.fromYou) c.onPrimary else c.onSurface, fontSize = 15.sp, lineHeight = 21.sp)
            }
        }
    }
}

@Composable
fun PromptCard(p: Prompt, onAnswer: (Any?) -> Unit) {
    val c = MaterialTheme.colorScheme
    Column(Modifier.padding(top = 8.dp)) {
        if (p.type == "confirm") {
            if (p.destructive) Text("Destructive, so I always ask:", color = c.error, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            if (p.sends) Text("This sends in your name, so I always ask:", color = c.tertiary, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            Text(p.question, fontSize = 15.sp, lineHeight = 21.sp, color = c.onSurface)
            var code by remember(p.question) { mutableStateOf("") }
            if (p.code) {
                Text("Two-factor approval: enter the code from your authenticator app.", fontSize = 13.sp, color = c.onSurfaceVariant,
                    modifier = Modifier.padding(top = 6.dp))
                OutlinedTextField(code, { code = it.filter(Char::isDigit).take(6) }, label = { Text("6-digit code") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), modifier = Modifier.width(170.dp))
            }
            Row(Modifier.padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button({ if (p.code) { if (code.length == 6) onAnswer(true to code) } else onAnswer(true) },
                    colors = if (p.destructive) ButtonDefaults.buttonColors(containerColor = c.error) else ButtonDefaults.buttonColors()) {
                    Text("Yes, do it")
                }
                OutlinedButton({ onAnswer(false) }) { Text("No") }
            }
        } else {
            Text("Which one?", fontSize = 15.sp, color = c.onSurface)
            p.options.forEachIndexed { i, o ->
                OutlinedButton({ onAnswer(i) }, Modifier.fillMaxWidth().padding(top = 6.dp)) { Text(o, fontSize = 13.sp) }
            }
            TextButton({ onAnswer(null) }) { Text("Cancel") }
        }
    }
}

// ---------------------------------------------------------------- status

@Composable
fun StatusScreen(vm: JarvisViewModel) {
    val s = vm.status
    val c = MaterialTheme.colorScheme
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Lab status", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    s?.optString("updated")?.takeIf { it.isNotEmpty() && it != "null" }?.let {
                        Text("Checked at $it", fontSize = 12.sp, color = c.onSurfaceVariant)
                    }
                }
                if (vm.loading) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
                else IconButton({ vm.refreshNow() }) { Icon(Icons.Filled.Refresh, "Check now") }
            }
        }
        if (s == null) {
            item { Text("Loading...", color = c.onSurfaceVariant) }
            return@LazyColumn
        }
        item {
            val counts = s.optJSONObject("counts") ?: JSONObject()
            val crit = counts.optInt("critical"); val warn = counts.optInt("warning")
            Card(colors = CardDefaults.cardColors(containerColor = when {
                crit > 0 -> c.errorContainer; warn > 0 -> c.tertiaryContainer; else -> c.surface })) {
                Column(Modifier.padding(14.dp)) {
                    Text(when { crit > 0 -> "$crit critical, $warn warning"; warn > 0 -> "$warn warning"; else -> "All good" },
                        fontWeight = FontWeight.Bold, fontSize = 17.sp)
                    Text("${counts.optInt("systems")} systems. " + s.optString("summary"), fontSize = 13.sp, lineHeight = 18.sp)
                }
            }
        }
        val reports = s.optJSONArray("reports")
        items((0 until (reports?.length() ?: 0)).map { reports!!.getJSONObject(it) }) { r -> SystemCard(r) }
        val events = s.optJSONArray("events")
        if (events != null && events.length() > 0) {
            item { Text("Recent alerts", fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 8.dp)) }
            items((0 until events.length()).map { events.getJSONObject(it) }) { e ->
                Text("${e.optString("time")}  ${e.optString("text")}", fontSize = 13.sp, color = c.onSurfaceVariant)
            }
        }
    }
}

@Composable
fun SystemCard(r: JSONObject) {
    var open by remember { mutableStateOf(false) }
    val c = MaterialTheme.colorScheme
    val problems = r.optJSONArray("problems")
    val nProblems = problems?.length() ?: 0
    val down = !r.optBoolean("reachable", true)
    val crit = down || (0 until nProblems).any { problems!!.getJSONObject(it).optString("severity") == "critical" }
    val (label, color) = when { down -> "DOWN" to c.error; crit -> "critical" to c.error; nProblems > 0 -> "warning" to c.tertiary; else -> "ok" to Color(0xFF22C55E) }
    Card(Modifier.fillMaxWidth().clickable { open = !open }, colors = CardDefaults.cardColors(containerColor = c.surface),
        border = androidx.compose.foundation.BorderStroke(1.dp, c.outline)) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(10.dp).background(color, CircleShape))
                Spacer(Modifier.width(8.dp))
                Text(r.optString("name"), fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                Text(r.optString("kind") + " · " + label, fontSize = 12.sp, color = c.onSurfaceVariant)
            }
            for (k in 0 until nProblems) {
                val p = problems!!.getJSONObject(k)
                Text(p.optString("message"), fontSize = 13.sp, lineHeight = 18.sp,
                    color = if (p.optString("severity") == "critical") c.error else c.tertiary, modifier = Modifier.padding(top = 6.dp))
            }
            val lines = r.optJSONArray("lines")
            val n = lines?.length() ?: 0
            if (n > 0) {
                HorizontalDivider(Modifier.padding(vertical = 8.dp), color = c.outline)
                for (k in 0 until (if (open) n else minOf(3, n))) Text(lines!!.getString(k), fontSize = 13.sp, color = c.onSurfaceVariant)
                if (n > 3) Text(if (open) "Show less" else "Show ${n - 3} more", fontSize = 13.sp, color = c.primary, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}

// ---------------------------------------------------------------- planner

@Composable
fun PlannerScreen(vm: JarvisViewModel) {
    val p = vm.planner
    val c = MaterialTheme.colorScheme
    var section by rememberSaveable { mutableStateOf("upcoming") }
    val showNotes = section == "notes"
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Planner", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text("Add things by telling Jarvis: \"remind me tomorrow at 9 to call mum\", \"add bill rent 1200 on the 1st monthly\", \"note: ...\"",
                        fontSize = 12.sp, color = c.onSurfaceVariant)
                }
                IconButton({ vm.loadPlanner() }) { Icon(Icons.Filled.Refresh, "Reload") }
            }
        }
        item { PhoneCalendarRow(vm) }
        item {
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                val sections = listOf("upcoming" to "Soon", "lists" to "Lists", "mail" to "Mail", "media" to "Media", "docs" to "Docs", "notes" to "Notes")
                    .filter { !vm.isFamily || it.first !in setOf("mail", "docs") }
                sections.forEachIndexed { i, (key, label) ->
                    SegmentedButton(selected = section == key, onClick = { section = key; if (key == "media") vm.loadMedia() }, shape = SegmentedButtonDefaults.itemShape(i, sections.size),
                        icon = {}) { Text(label, fontSize = 11.sp, maxLines = 1) }
                }
            }
        }
        if (p == null) {
            item { Text("Loading...", color = c.onSurfaceVariant) }
            return@LazyColumn
        }
        if (section == "docs") {
            item { DocsPanel(vm) }
            docItems(vm)
        } else if (section == "lists") {
            listItems(vm)
        } else if (section == "mail") {
            mailItems(vm)
        } else if (section == "media") {
            mediaItems(vm)
        } else if (showNotes) {
            val notes = p.optJSONArray("notes")
            val n = notes?.length() ?: 0
            if (n == 0) item { Text("No notes yet. Say \"note:\" and what to remember.", color = c.onSurfaceVariant) }
            items((0 until n).map { notes!!.getJSONObject(it) }) { note ->
                Card(colors = CardDefaults.cardColors(containerColor = c.surface), border = androidx.compose.foundation.BorderStroke(1.dp, c.outline)) {
                    Row(Modifier.padding(12.dp), verticalAlignment = Alignment.Top) {
                        Column(Modifier.weight(1f)) {
                            Text(note.optString("text"), fontSize = 15.sp)
                            Text(note.optString("created").replace("T", " "), fontSize = 11.sp, color = c.onSurfaceVariant)
                        }
                        TextButton({ vm.plannerAction("/api/planner/note/${note.optInt("id")}/delete") }) { Text("Delete") }
                    }
                }
            }
        } else {
            val up = p.optJSONArray("upcoming")
            val list = (0 until (up?.length() ?: 0)).map { up!!.getJSONObject(it) }
            if (list.isEmpty()) item { Text("Nothing in the next month.", color = c.onSurfaceVariant) }
            var lastHead = ""
            for (it in list) {
                val head = if (it.optBoolean("overdue")) "Overdue" else it.optString("date")
                if (head != lastHead) {
                    lastHead = head
                    item { Text(dayLabel(head).uppercase(), fontSize = 12.sp, letterSpacing = 0.5.sp, color = c.onSurfaceVariant,
                        fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 8.dp)) }
                }
                item { PlannerItem(vm, it) }
            }
        }
    }
}

@Composable
fun PhoneCalendarRow(vm: JarvisViewModel) {
    val context = LocalContext.current
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted.values.all { it }) vm.usePhoneCalendar(true)
        else android.widget.Toast.makeText(context, "Jarvis needs calendar access to add your events", android.widget.Toast.LENGTH_SHORT).show()
    }
    Row(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface, RoundedCornerShape(12.dp))
        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp)).padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text("Show in phone calendar", fontWeight = FontWeight.SemiBold)
            Text("Adds a Jarvis calendar to this phone's calendar app with the next 90 days of events, bills and reminders. Updates on each sync.",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(vm.phoneCalendar, { on ->
            if (!on) vm.usePhoneCalendar(false)
            else if (PhoneCalendar.allowed(context)) vm.usePhoneCalendar(true)
            else ask.launch(arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR))
        })
    }
}

@Composable
fun DocsPanel(vm: JarvisViewModel) {
    val context = LocalContext.current
    var q by rememberSaveable { mutableStateOf("") }
    val photoFile = remember { java.io.File(context.cacheDir, "captures").apply { mkdirs() }.resolve("receipt.jpg") }
    val photoUri = remember { androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.files", photoFile) }
    val camera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        if (ok && photoFile.exists()) vm.uploadDoc("receipt-${System.currentTimeMillis() / 1000}.jpg", photoFile.readBytes(), "image/jpeg", receipt = true)
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) {
            val cr = context.contentResolver
            val name = cr.query(uri, null, null, null, null)?.use { c -> val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (i >= 0 && c.moveToFirst()) c.getString(i) else null } ?: "document"
            val bytes = cr.openInputStream(uri)?.use { it.readBytes() }
            if (bytes != null) vm.uploadDoc(name, bytes, cr.getType(uri) ?: "application/octet-stream", receipt = false)
        }
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Your documents and receipts are read only by your local AI.", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ camera.launch(photoUri) }) { Text("Receipt photo") }
            OutlinedButton({ picker.launch("*/*") }) { Text("Add file") }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(q, { q = it }, placeholder = { Text("Ask your documents...") }, singleLine = true, modifier = Modifier.weight(1f),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { if (q.isNotBlank()) vm.askDocs(q) }))
            TextButton({ if (q.isNotBlank()) vm.askDocs(q) }) { Text("Ask") }
        }
        vm.docsBusy?.let { Row(verticalAlignment = Alignment.CenterVertically) { CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp); Spacer(Modifier.width(8.dp)); Text(it, fontSize = 13.sp) } }
        vm.docsAnswer?.let {
            Text(it, fontSize = 14.sp, modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.secondaryContainer, RoundedCornerShape(12.dp)).padding(12.dp),
                color = MaterialTheme.colorScheme.onSecondaryContainer)
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.docItems(vm: JarvisViewModel) {
    val arr = vm.docs?.optJSONArray("documents") ?: return
    val list = (0 until arr.length()).map { arr.getJSONObject(it) }
    if (list.isEmpty()) item { Text("No documents yet.", fontSize = 14.sp) }
    items(list) { d ->
        Row(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface, RoundedCornerShape(12.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(d.optString("title"), fontSize = 15.sp)
                val err = d.optString("error").takeIf { it.isNotEmpty() && it != "null" }
                Text(err?.let { "Couldn't read: $it" } ?: (d.optString("kind") + (if (d.optInt("pages") > 1) " · ${d.optInt("pages")} pages" else "")),
                    fontSize = 12.sp, color = if (err != null) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (d.optString("kind") == "receipt") TextButton({ vm.docAction("/api/docs/${d.optInt("id")}/warranty") }) { Text("Warranty") }
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.listItems(vm: JarvisViewModel) {
    val d = vm.life
    if (d == null) { item { Text("Loading...") }; return }
    val all = d.optJSONArray("items")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()
    val byList = all.groupBy { it.optString("list") }
    val names = (listOf("shopping", "todo") + byList.keys).distinct()
    item { Text("Add things by telling Jarvis: \"add milk and eggs to the shopping list\".", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
    for (name in names) {
        val its = byList[name].orEmpty()
        item {
            var text by remember(name) { mutableStateOf("") }
            Column(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface, RoundedCornerShape(12.dp))
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp)).padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(name.replaceFirstChar { it.uppercase() }, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    if (its.any { it.optInt("done") == 1 }) TextButton({ vm.lifeAction("/api/life/clear", JSONObject().put("list", name)) }) { Text("Clear ticked") }
                }
                its.forEach { it ->
                    val done = it.optInt("done") == 1
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        androidx.compose.material3.Checkbox(done, { _ -> vm.lifeAction("/api/life/item/${it.optInt("id")}/${if (done) "uncheck" else "check"}") })
                        Text(it.optString("text"), modifier = Modifier.weight(1f), fontSize = 15.sp,
                            color = if (done) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
                            textDecoration = if (done) androidx.compose.ui.text.style.TextDecoration.LineThrough else null)
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(text, { text = it }, placeholder = { Text("Add to $name") }, singleLine = true, modifier = Modifier.weight(1f),
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                        keyboardActions = KeyboardActions(onDone = { if (text.isNotBlank()) { vm.lifeAction("/api/life/item", JSONObject().put("list", name).put("text", text)); text = "" } }))
                    TextButton({ if (text.isNotBlank()) { vm.lifeAction("/api/life/item", JSONObject().put("list", name).put("text", text)); text = "" } }) { Text("Add") }
                }
            }
        }
    }
    val habits = d.optJSONArray("habits")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()
    item { Text("HABITS", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 8.dp)) }
    if (habits.isEmpty()) item { Text("No habits yet. Tell Jarvis: \"add habit take meds at 21:00\".", fontSize = 14.sp) }
    items(habits) { h ->
        Row(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface, RoundedCornerShape(12.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(h.optString("name"), fontSize = 15.sp)
                val streak = h.optInt("streak")
                Text("${h.optString("time")} · ${h.optString("days")}" + if (streak > 1) " · $streak-day streak" else "", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            when {
                h.optBoolean("done_today") -> Text("done", color = Color(0xFF22C55E), fontWeight = FontWeight.SemiBold)
                h.optBoolean("due_today") -> Button({ vm.lifeAction("/api/life/habit/${h.optInt("id")}/done") }) { Text("Done") }
                else -> Text("not today", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.mediaItems(vm: JarvisViewModel) {
    item {
        var kind by remember { mutableStateOf("movie") }
        var title by remember { mutableStateOf("") }
        var who by remember { mutableStateOf("") }
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("ASK FOR A MOVIE OR SHOW", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                FilterChip(kind == "movie", { kind = "movie" }, { Text("Movie") })
                FilterChip(kind == "show", { kind = "show" }, { Text("Show") })
            }
            OutlinedTextField(title, { title = it }, Modifier.fillMaxWidth(), singleLine = true, placeholder = { Text("Title") })
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (!vm.isFamily) {
                    OutlinedTextField(who, { who = it }, Modifier.weight(1f), singleLine = true, placeholder = { Text("For (name)") })
                    Spacer(Modifier.width(8.dp))
                } else Spacer(Modifier.weight(1f))
                Button({ if (title.isNotBlank()) { vm.requestMedia(kind, title.trim(), who.trim()); title = "" } }) { Text("Request") }
            }
            vm.mediaNote?.let { Text(it, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
    }
    val rq = vm.mediaRequests?.optJSONArray("requests")
    val reqs = (0 until (rq?.length() ?: 0)).map { rq!!.getJSONObject(it) }
    if (reqs.isNotEmpty()) {
        item { Text("REQUESTS", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp)) }
        val label = mapOf("pending" to "waiting for you", "approved" to "approved, downloading", "declined" to "declined", "ready" to "ready to watch")
        items(reqs) { r ->
            val st = r.optString("status")
            InfoRow("${r.optString("title")} (${r.str("year").ifEmpty { "?" }})", "${r.optString("kind")} · for ${r.optString("who")} · ${label[st] ?: st}",
                when (st) { "pending" -> MaterialTheme.colorScheme.error; "ready" -> MaterialTheme.colorScheme.primary; else -> MaterialTheme.colorScheme.tertiary }) {
                if (st == "pending" && !vm.isFamily) {
                    TextButton({ vm.decideMedia(r.optInt("id"), true) }) { Text("Approve") }
                    TextButton({ vm.decideMedia(r.optInt("id"), false) }) { Text("No") }
                }
            }
        }
    }
    val w = vm.watch
    item { Text("WATCH NEXT", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 8.dp)) }
    if (w == null) { item { Text("Loading...") }; return }
    if (!w.optBoolean("ok")) { item { Text(w.optString("error"), fontSize = 14.sp) }; return }
    val cont = w.optJSONArray("continue"); val picks = w.optJSONArray("picks")
    val all = (0 until (cont?.length() ?: 0)).map { cont!!.getJSONObject(it) to true } + (0 until (picks?.length() ?: 0)).map { picks!!.getJSONObject(it) to false }
    items(all) { (p, isCont) ->
        val genres = p.optJSONArray("genres")?.let { g -> (0 until g.length()).joinToString(", ") { g.getString(it) } } ?: ""
        val mins = p.str("minutes").let { if (it.isEmpty()) "" else if (isCont) "$it min left" else "$it min" + if (p.optString("kind") == "show") " episodes" else "" }
        val rating = if (p.isNull("rating") || !p.has("rating")) "" else "rated %.1f".format(p.optDouble("rating"))
        InfoRow(p.optString("title"), listOf(if (isCont) p.str("why") else genres, mins, rating, if (isCont) "" else p.str("why")).filter { it.isNotEmpty() }.joinToString(" · "),
            if (isCont) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.tertiary) {}
    }
    if (all.isEmpty()) item { Text("Nothing unwatched in your library right now.", fontSize = 14.sp) }
}

private fun androidx.compose.foundation.lazy.LazyListScope.mailItems(vm: JarvisViewModel) {
    val d = vm.mail
    if (d == null) { item { Text("Loading...") }; return }
    val accounts = d.optJSONArray("accounts")
    if (accounts == null || accounts.length() == 0) {
        item { Text("No email accounts yet. Add Gmail, Outlook, Proton or another in the dashboard: Setup > Email accounts.", fontSize = 14.sp) }
        return
    }
    val msgs = d.optJSONArray("messages")
    val all = (0 until (msgs?.length() ?: 0)).map { msgs!!.getJSONObject(it) }
    val important = all.filter { it.optInt("important") == 1 }
    val bills = all.filter { !it.isNull("bill") && it.optString("bill_state").isEmpty() }
    val model = d.optString("local_model").takeIf { it.isNotEmpty() && it != "null" }
    item {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(if (model != null) "Sorted by your local model $model. Last 3 days." else "Sorted by simple rules (no local AI set). Last 3 days.",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
            TextButton({ vm.checkMailNow() }) { Text("Check now") }
        }
    }
    val states = d.optJSONArray("states")
    for (i in 0 until (states?.length() ?: 0)) {
        val st = states!!.getJSONObject(i)
        if (!st.isNull("error")) item { Text("${st.optString("account")}: ${st.optString("error")}", color = MaterialTheme.colorScheme.error, fontSize = 13.sp) }
    }
    if (important.isNotEmpty()) {
        item { Text("IMPORTANT", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        items(important) { m -> MailRow(m, MaterialTheme.colorScheme.error) {} }
    }
    if (bills.isNotEmpty()) {
        item { Text("BILLS FOUND", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp)) }
        items(bills) { m ->
            MailRow(m, MaterialTheme.colorScheme.tertiary) {
                TextButton({ vm.plannerAction("/api/mail/${m.optInt("id")}/bill") }) { Text("Add") }
                TextButton({ vm.plannerAction("/api/mail/${m.optInt("id")}/dismiss") }) { Text("Ignore") }
            }
        }
    }
    val pk = d.optJSONArray("packages")
    val packages = (0 until (pk?.length() ?: 0)).map { pk!!.getJSONObject(it) }
    if (packages.isNotEmpty()) {
        item { Text("PACKAGES", fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp)) }
        items(packages) { p -> PackageRow(p) { TextButton({ vm.plannerAction("/api/mail/package/${p.optInt("id")}/dismiss") }) { Text("Hide") } } }
    }
    val sb = d.optJSONArray("subscriptions")
    val subs = (0 until (sb?.length() ?: 0)).map { sb!!.getJSONObject(it) }
    if (subs.isNotEmpty()) {
        val totals = mutableMapOf<String, Double>()
        subs.filter { !it.isNull("amount") }.forEach { s ->
            val perMonth = s.optDouble("amount") * when (s.optString("cycle")) { "yearly" -> 1.0 / 12; "weekly" -> 52.0 / 12; else -> 1.0 }
            val cur = s.optString("currency").takeIf { it != "null" } ?: ""
            totals[cur] = (totals[cur] ?: 0.0) + perMonth
        }
        item { Text("SUBSCRIPTIONS" + if (totals.isNotEmpty()) " · about " + totals.entries.joinToString(" + ") { "${it.key} ${Math.round(it.value)}/month".trim() } else "",
            fontSize = 12.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp)) }
        items(subs) { s -> SubscriptionRow(s) { TextButton({ vm.plannerAction("/api/mail/subscription/${s.optInt("id")}/cancelled") }) { Text("Cancelled") } } }
        item { Text("Found in your email. Jarvis can't see whether you use them: cancel on the service's site, then tap Cancelled.",
            fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) }
    }
    val rest = all.filter { it.optInt("important") != 1 && it.isNull("bill") }.groupingBy { it.optString("category") }.eachCount()
    if (rest.isNotEmpty()) item {
        Text("Everything else: " + rest.entries.sortedByDescending { it.value }.joinToString(", ") { "${it.value} ${it.key}" },
            fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 8.dp))
    }
    if (all.isEmpty()) item { Text("No new mail screened yet. Jarvis checks every 15 minutes.", fontSize = 14.sp) }
}

@Composable
fun MailRow(m: JSONObject, color: Color, actions: @Composable () -> Unit) {
    val c = MaterialTheme.colorScheme
    val sender = m.optString("sender").let { s -> Regex("^(.*?)\\s*<(.+)>$").find(s)?.let { it.groupValues[1].trim('"').ifEmpty { it.groupValues[2] } } ?: s }
    val bill = m.optJSONObject("bill")
    Row(Modifier.fillMaxWidth().background(c.surface, RoundedCornerShape(12.dp)).border(1.dp, c.outline, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp).background(color, CircleShape))
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            if (bill != null) {
                val amt = if (bill.isNull("amount")) "" else " · ${bill.optString("currency").takeIf { it != "null" } ?: ""} " +
                    bill.optDouble("amount").let { a -> if (a % 1.0 == 0.0) "%,.0f".format(a) else "%,.2f".format(a) }
                val due = bill.optString("due_date").takeIf { it.isNotEmpty() && it != "null" }?.let { ", due $it" } ?: ""
                Text((bill.optString("payee").takeIf { it.isNotEmpty() && it != "null" } ?: sender) + amt + due, fontSize = 15.sp)
            } else Text(m.optString("summary").ifEmpty { m.optString("subject") }, fontSize = 15.sp)
            Text("$sender · ${m.optString("subject")}", fontSize = 12.sp, color = c.onSurfaceVariant, maxLines = 2)
        }
        actions()
    }
}

private fun JSONObject.str(key: String) = if (isNull(key)) "" else optString(key)

@Composable
private fun InfoRow(title: String, detail: String, color: Color, actions: @Composable () -> Unit) {
    val c = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth().background(c.surface, RoundedCornerShape(12.dp)).border(1.dp, c.outline, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp).background(color, CircleShape))
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 15.sp)
            if (detail.isNotEmpty()) Text(detail, fontSize = 12.sp, color = c.onSurfaceVariant, maxLines = 2)
        }
        actions()
    }
}

@Composable
fun PackageRow(p: JSONObject, actions: @Composable () -> Unit) {
    val c = MaterialTheme.colorScheme
    val status = p.str("status")
    val label = mapOf("ordered" to "Ordered", "shipped" to "Shipped", "out_for_delivery" to "Out for delivery",
        "delivered" to "Delivered", "delayed" to "Delayed").getOrDefault(status, "Update")
    val detail = listOf(p.str("sender"), if (p.str("expected").isNotEmpty() && status != "delivered") "expected ${p.str("expected")}" else "",
        listOf(p.str("carrier"), p.str("tracking")).filter { it.isNotEmpty() }.joinToString(" ")).filter { it.isNotEmpty() }.joinToString(" · ")
    InfoRow("${p.str("item").ifEmpty { "Package" }} · $label", detail,
        when (status) { "delivered" -> c.primary; "delayed" -> c.error; else -> c.tertiary }, actions)
}

@Composable
fun SubscriptionRow(s: JSONObject, actions: @Composable () -> Unit) {
    val amount = if (s.isNull("amount")) null else s.optDouble("amount")
    val money = amount?.let { a -> " · ${s.str("currency")} ${if (a % 1.0 == 0.0) "%,.0f".format(a) else "%,.2f".format(a)} ${s.str("cycle")}" } ?: " · ${s.str("cycle")}"
    val prev = if (s.isNull("previous_amount")) null else s.optDouble("previous_amount")
    val rise = if (prev != null && amount != null && amount > prev) "  ▲ was ${"%,.0f".format(prev)}" else ""
    InfoRow(s.str("service") + money + rise, s.str("renews").let { if (it.isEmpty()) "renewal date unknown" else "renews $it" },
        MaterialTheme.colorScheme.tertiary, actions)
}

private fun dayLabel(iso: String): String {
    if (iso == "Overdue") return iso
    return runCatching {
        val d = java.time.LocalDate.parse(iso)
        val today = java.time.LocalDate.now()
        when (d) {
            today -> "Today"
            today.plusDays(1) -> "Tomorrow"
            else -> d.format(java.time.format.DateTimeFormatter.ofPattern("EEEE d MMM"))
        }
    }.getOrDefault(iso)
}

@Composable
fun PlannerItem(vm: JarvisViewModel, it: JSONObject) {
    val c = MaterialTheme.colorScheme
    val kind = it.optString("kind")
    val color = when {
        it.optBoolean("overdue") -> c.error
        kind == "bill" -> c.tertiary
        kind == "reminder" -> Color(0xFF22C55E)
        else -> c.primary
    }
    Row(Modifier.fillMaxWidth().background(c.surface, RoundedCornerShape(12.dp)).border(1.dp, c.outline, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp).background(color, CircleShape))
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            val amount = if (it.isNull("amount")) "" else " · " + it.optDouble("amount").let { a -> if (a % 1.0 == 0.0) "%,.0f".format(a) else "%,.2f".format(a) }
            Text(it.optString("title") + amount, fontSize = 15.sp)
            val meta = listOfNotNull(kind.replaceFirstChar { ch -> ch.uppercase() }, it.optString("time").ifEmpty { null },
                it.optString("repeat").ifEmpty { null }?.let { r -> "repeats $r" }).joinToString(" · ")
            Text(meta, fontSize = 12.sp, color = c.onSurfaceVariant)
        }
        if (kind == "bill") TextButton({ vm.plannerAction("/api/planner/event/${it.optInt("id")}/paid") }) { Text("Paid") }
    }
}

// ---------------------------------------------------------------- activity

@Composable
fun ActivityScreen(vm: JarvisViewModel) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp)) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Activity", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text("Everything Jarvis changed, ran, or was blocked from doing", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                IconButton({ vm.loadActivity() }) { Icon(Icons.Filled.Refresh, "Reload") }
            }
        }
        if (vm.activity.isEmpty()) item { Text("Nothing yet.", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 12.dp)) }
        items(vm.activity) { line ->
            Column(Modifier.padding(vertical = 6.dp)) {
                Text(line.take(19), fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(line.drop(19).trim(), fontSize = 13.sp, fontFamily = FontFamily.Monospace, lineHeight = 17.sp)
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        }
    }
}

// ---------------------------------------------------------------- settings

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen(vm: JarvisViewModel) {
    val context = LocalContext.current
    var alerts by remember { mutableStateOf(vm.prefs.alerts) }
    var speak by remember { mutableStateOf(vm.prefs.speak) }
    var autoUpdate by remember { mutableStateOf(vm.prefs.autoUpdate) }
    val notifyPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    val c = MaterialTheme.colorScheme
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text("Settings", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
        Text("Connected to ${vm.prefs.server}", color = c.onSurfaceVariant)
        vm.me?.let { u -> Text("Signed in as ${u.optString("name")} (${if (vm.isFamily) "family" else "admin"})", color = c.onSurfaceVariant, fontSize = 13.sp) }

        SectionTitle("Theme")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            THEMES.forEach { t -> ThemeSwatch(t, selected = vm.theme == t.id) { vm.chooseTheme(t.id) } }
        }

        SectionTitle("Notifications and voice")
        SettingRow("Lab alerts", "Every 15 minutes, check Jarvis and notify me about new problems", alerts) {
            alerts = it; vm.prefs.alerts = it
            if (it) {
                AlertWorker.schedule(context)
                if (Build.VERSION.SDK_INT >= 33) notifyPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else AlertWorker.cancel(context)
        }
        SettingRow("Speak replies", "Read Jarvis's answer aloud when you asked by voice", speak) { speak = it; vm.prefs.speak = it }

        if (!vm.isFamily) {
            SectionTitle("Location")
            LocationRow(vm)
        }

        SectionTitle("App updates")
        SettingRow("Check automatically", "Look for a new version once a day", autoUpdate) { autoUpdate = it; vm.prefs.autoUpdate = it }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Version ${vm.version}", fontWeight = FontWeight.SemiBold)
                if (vm.updateStatus.isNotEmpty()) Text(vm.updateStatus, fontSize = 13.sp, color = c.onSurfaceVariant)
            }
            if (vm.update != null && vm.downloadProgress == null) Button({ vm.installUpdate() }) { Text("Update") }
            else OutlinedButton({ vm.checkForUpdate(manual = true) }, enabled = vm.downloadProgress == null && vm.updateStatus != "Checking...") { Text("Check now") }
        }
        vm.downloadProgress?.let { LinearProgressIndicator(progress = { it }, modifier = Modifier.fillMaxWidth()) }

        SectionTitle("Jarvis")
        OutlinedButton({ context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(vm.prefs.server))) }, Modifier.fillMaxWidth()) {
            Text("Open the full dashboard (Setup, Devices)")
        }
        OutlinedButton({ vm.logout() }, Modifier.fillMaxWidth()) { Text("Log out / change Jarvis") }
        Text("Destructive actions always ask, whatever the mode.", fontSize = 12.sp, color = c.onSurfaceVariant)
    }
}

@Composable
fun LocationRow(vm: JarvisViewModel) {
    val context = LocalContext.current
    var on by remember { mutableStateOf(vm.prefs.shareLocation) }
    var note by remember { mutableStateOf("") }
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    fun enable() {
        on = true; vm.prefs.shareLocation = true
        LocationShare.schedule(context)
        scope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val ok = LocationShare.sendNow(context)
            note = if (ok) "Sent your location just now. From now on, every 15 minutes." else "Turned on. Couldn't get a location yet (is location on?)"
        }
        if (!LocationShare.allowed(context, background = true))
            note = "For updates while the app is closed, choose \"Allow all the time\" in the location permission."
    }
    val background = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { enable() }
    val foreground = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted.values.any { it }) {
            if (Build.VERSION.SDK_INT >= 29 && !LocationShare.allowed(context, background = true)) background.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            else enable()
        } else note = "Jarvis needs location permission to share your location."
    }
    SettingRow("Share my location", "Every 15 minutes, send this phone's location to your Jarvis (and nowhere else). See your days on the Places page.", on) { want ->
        if (!want) { on = false; vm.prefs.shareLocation = false; LocationShare.cancel(context); note = "Stopped sharing." }
        else if (LocationShare.allowed(context)) {
            if (Build.VERSION.SDK_INT >= 29 && !LocationShare.allowed(context, background = true)) background.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION) else enable()
        } else foreground.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
    }
    if (note.isNotEmpty()) Text(note, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
fun SectionTitle(text: String) {
    Text(text.uppercase(), fontSize = 12.sp, letterSpacing = 0.6.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 6.dp))
}

@Composable
fun ThemeSwatch(t: AppTheme, selected: Boolean, onClick: () -> Unit) {
    val c = MaterialTheme.colorScheme
    Column(
        Modifier.width(96.dp).clip(RoundedCornerShape(12.dp))
            .border(2.dp, if (selected) c.primary else c.outline, RoundedCornerShape(12.dp))
            .clickable(onClick = onClick),
    ) {
        Box(Modifier.fillMaxWidth().height(48.dp).background(
            if (t.id == "system") Brush.linearGradient(0.5f to t.panel, 0.5f to t.bg) else Brush.linearGradient(listOf(t.bg, t.bg)))) {
            if (t.id != "system") Box(Modifier.align(Alignment.BottomStart).padding(start = 8.dp, bottom = 8.dp).width(52.dp).height(10.dp)
                .background(t.panel, RoundedCornerShape(4.dp)).border(1.dp, Color(0x22FFFFFF), RoundedCornerShape(4.dp)))
            Box(Modifier.align(Alignment.BottomEnd).padding(8.dp).size(12.dp).background(t.accent, CircleShape))
        }
        Text(t.name, fontSize = 12.sp, modifier = Modifier.fillMaxWidth().background(c.surface).padding(horizontal = 8.dp, vertical = 5.dp),
            maxLines = 1, color = c.onSurface)
    }
}

@Composable
fun UpdateDialog(vm: JarvisViewModel) {
    val u = vm.update ?: return
    AlertDialog(
        onDismissRequest = { vm.showUpdateDialog = false },
        title = { Text("Jarvis ${u.version} is available") },
        text = {
            Column(Modifier.heightIn(max = 320.dp).verticalScroll(rememberScrollState())) {
                Text("You have ${vm.version}." + if (u.size > 0) " Download size: ${"%.1f".format(u.size / 1_048_576.0)} MB." else "",
                    color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 14.sp)
                if (u.notes.isNotEmpty()) {
                    Spacer(Modifier.height(10.dp))
                    Text(u.notes, fontSize = 14.sp)
                }
            }
        },
        confirmButton = { Button({ vm.installUpdate() }) { Text("Update") } },
        dismissButton = {
            Row {
                TextButton({ vm.skipUpdate() }) { Text("Skip this version") }
                TextButton({ vm.showUpdateDialog = false }) { Text("Later") }
            }
        },
    )
}

@Composable
fun SettingRow(title: String, help: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, fontWeight = FontWeight.SemiBold)
            Text(help, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked, onChange)
    }
}

// ---------------------------------------------------------------- icons not in the core icon set

val MicIcon: ImageVector = ImageVector.Builder("Mic", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(Color.Black)) {
        moveTo(12f, 14f); curveTo(13.66f, 14f, 15f, 12.66f, 15f, 11f); verticalLineTo(5f)
        curveTo(15f, 3.34f, 13.66f, 2f, 12f, 2f); curveTo(10.34f, 2f, 9f, 3.34f, 9f, 5f); verticalLineTo(11f)
        curveTo(9f, 12.66f, 10.34f, 14f, 12f, 14f); close()
        moveTo(17.3f, 11f); curveTo(17.3f, 14f, 14.76f, 16.1f, 12f, 16.1f); curveTo(9.24f, 16.1f, 6.7f, 14f, 6.7f, 11f)
        horizontalLineTo(5f); curveTo(5f, 14.41f, 7.72f, 17.23f, 11f, 17.72f); verticalLineTo(21f); horizontalLineTo(13f)
        verticalLineTo(17.72f); curveTo(16.28f, 17.24f, 19f, 14.42f, 19f, 11f); horizontalLineTo(17.3f); close()
    }
}.build()

val HeadsetIcon: ImageVector = ImageVector.Builder("Headset", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(Color.Black)) {
        moveTo(12f, 3f); curveTo(7.03f, 3f, 3f, 7.03f, 3f, 12f); verticalLineTo(19f); curveTo(3f, 20.1f, 3.9f, 21f, 5f, 21f)
        horizontalLineTo(8f); verticalLineTo(13f); horizontalLineTo(5f); verticalLineTo(12f); curveTo(5f, 8.13f, 8.13f, 5f, 12f, 5f)
        reflectiveCurveTo(19f, 8.13f, 19f, 12f); verticalLineTo(13f); horizontalLineTo(16f); verticalLineTo(21f); horizontalLineTo(19f)
        curveTo(20.1f, 21f, 21f, 20.1f, 21f, 19f); verticalLineTo(12f); curveTo(21f, 7.03f, 16.97f, 3f, 12f, 3f); close()
    }
}.build()

val ChatIcon: ImageVector = ImageVector.Builder("Chat", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(Color.Black)) {
        moveTo(20f, 2f); horizontalLineTo(4f); curveTo(2.9f, 2f, 2f, 2.9f, 2f, 4f); verticalLineTo(22f)
        lineTo(6f, 18f); horizontalLineTo(20f); curveTo(21.1f, 18f, 22f, 17.1f, 22f, 16f); verticalLineTo(4f)
        curveTo(22f, 2.9f, 21.1f, 2f, 20f, 2f); close()
    }
}.build()
