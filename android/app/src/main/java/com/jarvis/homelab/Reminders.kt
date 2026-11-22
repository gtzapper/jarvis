package com.jarvis.homelab

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import org.json.JSONArray
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Reminders ring on the minute: the next day's reminders from Jarvis are set as alarms on the phone itself,
 * so they don't wait for Android's 15-minute background check. Refreshed whenever the app syncs.
 */
object ReminderAlarms {
    private const val MAX = 20

    fun schedule(context: Context, reminders: JSONArray) {
        val am = context.getSystemService(AlarmManager::class.java)
        val prefs = Prefs(context)
        // clear what we set last time, then set the current ones
        for (code in prefs.alarmCodes) am.cancel(pending(context, code, "", null))
        val now = System.currentTimeMillis()
        val codes = mutableSetOf<Int>()
        for (i in 0 until minOf(reminders.length(), 200)) {
            if (codes.size >= MAX) break
            val r = reminders.getJSONObject(i)
            val due = runCatching { LocalDateTime.parse(r.getString("due")) }.getOrNull() ?: continue
            val at = due.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
            if (at <= now || at > now + 26 * 3600_000L) continue
            val code = (r.getInt("id") * 31 + r.getString("due").hashCode()) and 0x7fffffff
            val pi = pending(context, code, r.getString("text"), r.getString("due"))
            if (Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
            else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
            codes += code
        }
        prefs.alarmCodes = codes
    }

    private fun pending(context: Context, code: Int, text: String, due: String?): PendingIntent =
        PendingIntent.getBroadcast(context, code,
            Intent(context, ReminderReceiver::class.java).putExtra("text", text).putExtra("due", due),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val text = intent.getStringExtra("text") ?: return
        val prefs = Prefs(context)
        if (!prefs.alerts) return
        // remember it, so the same reminder arriving later through Jarvis's notification feed isn't shown twice
        prefs.recentReminders = (prefs.recentReminders.toList() + "$text|${System.currentTimeMillis()}").takeLast(20).toSet()
        AlertWorker.notify(context, 2000 + (text.hashCode() and 0xfff), "Reminder", text, openStatus = false, channel = "reminders")
    }
}
