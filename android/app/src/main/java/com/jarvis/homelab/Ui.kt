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
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
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
                    Triple("chat", "Chat", ChatIcon), Triple("status", "Status", Icons.Filled.Home),
                    Triple("activity", "Activity", Icons.AutoMirrored.Filled.List), Triple("settings", "Settings", Icons.Filled.Settings),
                ).forEach { (key, label, icon) ->
                    NavigationBarItem(
                        selected = tab.value == key,
                        onClick = { tab.value = key; if (key == "activity") vm.loadActivity(); if (key == "status") vm.loadStatus() },
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
            when (tab.value) {
                "status" -> StatusScreen(vm)
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
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(password, { password = it }, label = { Text("Dashboard password") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Go),
            keyboardActions = KeyboardActions(onGo = { vm.connect(server, password) }))
        Spacer(Modifier.height(16.dp))
        Button({ vm.connect(server, password) }, enabled = server.isNotBlank() && !vm.connecting, modifier = Modifier.fillMaxWidth().height(48.dp)) {
            if (vm.connecting) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
            else Text("Connect")
        }
        vm.connectError?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = MaterialTheme.colorScheme.error, fontSize = 14.sp)
        }
        Spacer(Modifier.height(20.dp))
        Text("Use the address you open the dashboard with. Your phone needs to be on your home network, or connected to it by VPN. " +
            "The password isn't stored; the app keeps the login like a browser does.",
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

    Column(Modifier.fillMaxSize().imePadding()) {
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = list, contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            itemsIndexed(vm.messages) { i, m -> MessageBubble(m) { value -> vm.answer(i, value) } }
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
        Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(input, { input = it }, placeholder = { Text("Tell Jarvis what to do...") },
                modifier = Modifier.weight(1f), maxLines = 4, shape = RoundedCornerShape(24.dp),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { vm.send(input); input = "" }))
            IconButton(onClick = {
                vm.stopSpeaking()
                val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_PROMPT, "Tell Jarvis what to do")
                try { voice.launch(intent) } catch (e: ActivityNotFoundException) {
                    android.widget.Toast.makeText(context, "No voice input app on this phone", android.widget.Toast.LENGTH_SHORT).show()
                }
            }, enabled = !vm.busy) { Icon(MicIcon, "Speak") }
            IconButton(onClick = { vm.send(input); input = "" }, enabled = input.isNotBlank() && !vm.busy) {
                Icon(Icons.AutoMirrored.Filled.Send, "Send", tint = if (input.isNotBlank()) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
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
            Text(p.question, fontSize = 15.sp, lineHeight = 21.sp, color = c.onSurface)
            Row(Modifier.padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button({ onAnswer(true) }, colors = if (p.destructive) ButtonDefaults.buttonColors(containerColor = c.error) else ButtonDefaults.buttonColors()) {
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

val ChatIcon: ImageVector = ImageVector.Builder("Chat", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(Color.Black)) {
        moveTo(20f, 2f); horizontalLineTo(4f); curveTo(2.9f, 2f, 2f, 2.9f, 2f, 4f); verticalLineTo(22f)
        lineTo(6f, 18f); horizontalLineTo(20f); curveTo(21.1f, 18f, 22f, 17.1f, 22f, 16f); verticalLineTo(4f)
        curveTo(22f, 2.9f, 21.1f, 2f, 20f, 2f); close()
    }
}.build()
