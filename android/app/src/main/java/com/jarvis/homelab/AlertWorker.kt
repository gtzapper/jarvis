package com.jarvis.homelab

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * Every 15 minutes (Android's shortest background interval), ask Jarvis for its status and
 * notify about problems that weren't there last time. Talks only to your Jarvis: no push service.
 */
class AlertWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        if (!prefs.alerts || prefs.server.isEmpty()) return Result.success()
        syncPlanner(applicationContext)
        if (prefs.role == "family") return Result.success()  // lab alerts are for the owner; family get their reminders via syncPlanner
        val status = try {
            Api(prefs).get("/api/status")
        } catch (e: AuthNeeded) {
            if (!prefs.loginNagged) {
                notify(applicationContext, 1, "Jarvis needs you to log in again", "Alerts are paused until you do.", false)
                prefs.loginNagged = true
            }
            return Result.success()
        } catch (e: Exception) {
            return Result.success()  // away from home or Jarvis is down: try again next time
        }

        val current = linkedMapOf<String, Pair<String, Boolean>>()  // key -> (text, critical)
        val reports = status.optJSONArray("reports")
        for (i in 0 until (reports?.length() ?: 0)) {
            val r = reports!!.getJSONObject(i)
            val name = r.optString("name")
            if (!r.optBoolean("reachable", true)) current["$name|down"] = "$name is not responding" to true
            val problems = r.optJSONArray("problems")
            for (k in 0 until (problems?.length() ?: 0)) {
                val p = problems!!.getJSONObject(k)
                current["$name|${p.optString("key")}"] = p.optString("message") to (p.optString("severity") == "critical")
            }
        }
        val known = prefs.knownProblems
        if (known != null) {  // the very first check only learns what's already wrong
            val fresh = current.filterKeys { it !in known }
            fresh.entries.take(4).forEachIndexed { n, (key, v) ->
                notify(applicationContext, 100 + (key.hashCode() and 0xffff), if (v.second) "Jarvis: critical" else "Jarvis: warning", v.first, true)
            }
            if (fresh.size > 4) notify(applicationContext, 99, "Jarvis", "${fresh.size - 4} more new problems. Open Jarvis to see them.", true)
        }
        prefs.knownProblems = current.keys
        return Result.success()
    }

    companion object {
        private const val CHANNEL = "alerts"

        /** Reminders, bill notices and the daily report from Jarvis's notification feed, and exact alarms for upcoming reminders. */
        fun syncPlanner(context: Context) {
            val prefs = Prefs(context)
            val api = Api(prefs)
            try {
                val box = api.get("/api/outbox" + if (prefs.outboxLast >= 0) "?after=${prefs.outboxLast}" else "")
                val items = box.optJSONArray("items")
                val fired = prefs.recentReminders.mapNotNull { e -> e.substringBeforeLast("|").takeIf {
                    System.currentTimeMillis() - (e.substringAfterLast("|").toLongOrNull() ?: 0) < 3600_000 } }.toSet()
                for (i in 0 until (items?.length() ?: 0)) {
                    val it = items!!.getJSONObject(i)
                    val kind = it.optString("kind")
                    if (kind == "reminder" && it.optString("text") in fired) continue  // the phone's own alarm already showed it
                    notify(context, 3000 + it.optInt("id") % 1000, it.optString("title"), it.optString("text"), openStatus = false,
                        channel = if (kind == "report") "reports" else "reminders")
                }
                prefs.outboxLast = box.optInt("last", prefs.outboxLast)
                val planner = api.get("/api/planner?days=${PhoneCalendar.WINDOW_DAYS}")
                planner.optJSONArray("reminders")?.let { ReminderAlarms.schedule(context, it) }
                if (prefs.phoneCalendar) planner.optJSONArray("upcoming")?.let { PhoneCalendar.sync(context, it) }
            } catch (_: Exception) {
                // away from home or Jarvis is down: try again next time
            }
        }

        fun schedule(context: Context) {
            val req = PeriodicWorkRequestBuilder<AlertWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork("jarvis-alerts", ExistingPeriodicWorkPolicy.KEEP, req)
        }

        fun cancel(context: Context) = WorkManager.getInstance(context).cancelUniqueWork("jarvis-alerts")

        fun notify(context: Context, id: Int, title: String, text: String, openStatus: Boolean, channel: String = CHANNEL) {
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
            val nm = context.getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(channel) == null) {
                val (name, desc, importance) = when (channel) {
                    "reminders" -> Triple("Reminders and bills", "Your reminders, bill notices and events", NotificationManager.IMPORTANCE_HIGH)
                    "reports" -> Triple("Daily report", "Jarvis's morning briefing", NotificationManager.IMPORTANCE_DEFAULT)
                    else -> Triple("Lab alerts", "New problems Jarvis finds in your homelab", NotificationManager.IMPORTANCE_DEFAULT)
                }
                nm.createNotificationChannel(NotificationChannel(channel, name, importance).apply { description = desc })
            }
            val open = Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .putExtra("tab", if (openStatus) "status" else if (channel == CHANNEL) "chat" else "planner")
            val pi = PendingIntent.getActivity(context, id, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val n = NotificationCompat.Builder(context, channel)
                .setSmallIcon(R.drawable.ic_stat_jarvis)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build()
            try { NotificationManagerCompat.from(context).notify(id, n) } catch (_: SecurityException) {}
        }
    }
}
