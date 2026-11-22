package com.jarvis.homelab

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import org.json.JSONArray
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset

/**
 * "Show in phone calendar": a local calendar called Jarvis on this phone, filled with the next 90 days of events, bills
 * and reminders. It lives only on the phone (no Google account involved) and is rewritten on each sync.
 */
object PhoneCalendar {
    private const val ACCOUNT = "Jarvis"
    private const val DAYS = 90

    fun allowed(context: Context) = listOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR).all {
        ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
    }

    private fun asAdapter(uri: Uri): Uri = uri.buildUpon()
        .appendQueryParameter(CalendarContract.CALLER_IS_SYNCADAPTER, "true")
        .appendQueryParameter(CalendarContract.Calendars.ACCOUNT_NAME, ACCOUNT)
        .appendQueryParameter(CalendarContract.Calendars.ACCOUNT_TYPE, CalendarContract.ACCOUNT_TYPE_LOCAL)
        .build()

    private fun calendarId(context: Context, create: Boolean): Long? {
        val cr = context.contentResolver
        cr.query(CalendarContract.Calendars.CONTENT_URI, arrayOf(CalendarContract.Calendars._ID),
            "${CalendarContract.Calendars.ACCOUNT_NAME} = ? AND ${CalendarContract.Calendars.ACCOUNT_TYPE} = ?",
            arrayOf(ACCOUNT, CalendarContract.ACCOUNT_TYPE_LOCAL), null)?.use { if (it.moveToFirst()) return it.getLong(0) }
        if (!create) return null
        val v = ContentValues().apply {
            put(CalendarContract.Calendars.ACCOUNT_NAME, ACCOUNT)
            put(CalendarContract.Calendars.ACCOUNT_TYPE, CalendarContract.ACCOUNT_TYPE_LOCAL)
            put(CalendarContract.Calendars.NAME, "Jarvis")
            put(CalendarContract.Calendars.CALENDAR_DISPLAY_NAME, "Jarvis")
            put(CalendarContract.Calendars.CALENDAR_COLOR, Color.parseColor("#3B82F6"))
            put(CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL, CalendarContract.Calendars.CAL_ACCESS_READ)
            put(CalendarContract.Calendars.OWNER_ACCOUNT, ACCOUNT)
            put(CalendarContract.Calendars.VISIBLE, 1)
            put(CalendarContract.Calendars.SYNC_EVENTS, 1)
            put(CalendarContract.Calendars.CALENDAR_TIME_ZONE, ZoneId.systemDefault().id)
        }
        return cr.insert(asAdapter(CalendarContract.Calendars.CONTENT_URI), v)?.lastPathSegment?.toLongOrNull()
    }

    /** Rewrite the Jarvis calendar from Jarvis's upcoming list (repeats already expanded by the server). */
    fun sync(context: Context, upcoming: JSONArray): Int {
        if (!allowed(context)) return 0
        val cal = calendarId(context, create = true) ?: return 0
        val cr = context.contentResolver
        cr.delete(asAdapter(CalendarContract.Events.CONTENT_URI), "${CalendarContract.Events.CALENDAR_ID} = ?", arrayOf(cal.toString()))
        val zone = ZoneId.systemDefault()
        var n = 0
        for (i in 0 until upcoming.length()) {
            val it = upcoming.getJSONObject(i)
            if (it.optBoolean("overdue")) continue
            val day = runCatching { LocalDate.parse(it.getString("date")) }.getOrNull() ?: continue
            val kind = it.optString("kind")
            val amount = if (it.isNull("amount")) "" else " (" + it.optDouble("amount").let { a -> if (a % 1.0 == 0.0) "%,.0f".format(a) else "%,.2f".format(a) } + ")"
            val title = when (kind) { "bill" -> "Bill: ${it.optString("title")}$amount"; "reminder" -> "Reminder: ${it.optString("title")}"; else -> it.optString("title") }
            val time = it.optString("time")
            val v = ContentValues().apply {
                put(CalendarContract.Events.CALENDAR_ID, cal)
                put(CalendarContract.Events.TITLE, title)
                put(CalendarContract.Events.DESCRIPTION, "From Jarvis")
                put(CalendarContract.Events.HAS_ALARM, 0)  // Jarvis already reminds you; no second alert from the calendar
                if (time.isNotEmpty()) {
                    val start = LocalDateTime.of(day, java.time.LocalTime.parse(time)).atZone(zone)
                    put(CalendarContract.Events.DTSTART, start.toInstant().toEpochMilli())
                    put(CalendarContract.Events.DTEND, start.plusMinutes(if (kind == "reminder") 15 else 30).toInstant().toEpochMilli())
                    put(CalendarContract.Events.EVENT_TIMEZONE, zone.id)
                } else {
                    put(CalendarContract.Events.ALL_DAY, 1)
                    put(CalendarContract.Events.DTSTART, day.atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli())
                    put(CalendarContract.Events.DTEND, day.plusDays(1).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli())
                    put(CalendarContract.Events.EVENT_TIMEZONE, "UTC")
                }
            }
            if (cr.insert(asAdapter(CalendarContract.Events.CONTENT_URI), v) != null) n++
        }
        return n
    }

    fun remove(context: Context) {
        if (!allowed(context)) return
        val cal = calendarId(context, create = false) ?: return
        context.contentResolver.delete(asAdapter(CalendarContract.Calendars.CONTENT_URI), "${CalendarContract.Calendars._ID} = ?", arrayOf(cal.toString()))
    }

    const val WINDOW_DAYS = DAYS
}
