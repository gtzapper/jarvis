package com.jarvis.homelab

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.BatteryManager
import android.os.Build
import android.os.CancellationSignal
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * "Share my location": every 15 minutes, send this phone's location to your Jarvis (and nowhere else).
 * Uses Android's own location service, no Google Play services needed.
 */
class LocationWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val prefs = Prefs(applicationContext)
        if (!prefs.shareLocation || prefs.server.isEmpty()) return Result.success()
        LocationShare.sendNow(applicationContext)
        return Result.success()
    }
}

object LocationShare {
    fun allowed(context: Context, background: Boolean = false): Boolean {
        val fine = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val bg = Build.VERSION.SDK_INT < 29 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED
        return fine && (!background || bg)
    }

    fun schedule(context: Context) {
        val req = PeriodicWorkRequestBuilder<LocationWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("jarvis-location", ExistingPeriodicWorkPolicy.KEEP, req)
    }

    fun cancel(context: Context) = WorkManager.getInstance(context).cancelUniqueWork("jarvis-location")

    @Suppress("MissingPermission")
    private suspend fun current(context: Context): Location? {
        if (!allowed(context)) return null
        val lm = context.getSystemService(LocationManager::class.java) ?: return null
        val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER, LocationManager.FUSED_PROVIDER)
            .filter { runCatching { lm.isProviderEnabled(it) }.getOrDefault(false) }
        if (Build.VERSION.SDK_INT >= 30) {
            for (p in providers.sortedBy { if (it == LocationManager.FUSED_PROVIDER) 0 else if (it == LocationManager.NETWORK_PROVIDER) 1 else 2 }) {
                val loc = withTimeoutOrNull(30_000) {
                    suspendCancellableCoroutine<Location?> { cont ->
                        val cancel = CancellationSignal()
                        cont.invokeOnCancellation { cancel.cancel() }
                        lm.getCurrentLocation(p, cancel, ContextCompat.getMainExecutor(context)) { cont.resume(it) }
                    }
                }
                if (loc != null) return loc
            }
        }
        // older phones, or nothing fresh: the most recent fix, if it's from the last 10 minutes
        return providers.mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
            .filter { System.currentTimeMillis() - it.time < 10 * 60_000 }.maxByOrNull { it.time }
    }

    suspend fun sendNow(context: Context): Boolean {
        val loc = current(context) ?: return false
        val batt = context.getSystemService(BatteryManager::class.java)?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val body = JSONObject().put("lat", loc.latitude).put("lon", loc.longitude).put("t", loc.time / 1000.0)
            .put("acc", if (loc.hasAccuracy()) loc.accuracy.toDouble() else JSONObject.NULL)
            .put("speed", if (loc.hasSpeed()) loc.speed * 3.6 else JSONObject.NULL)
            .put("batt", batt ?: JSONObject.NULL).put("device", Build.MODEL)
        return runCatching { Api(Prefs(context)).post("/api/location/app", body); true }.getOrDefault(false)
    }
}
