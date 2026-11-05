package com.steammanifestdownloader

import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject

class SteamDownloaderService : Service() {

    companion object {
        const val ACTION_START_SERVICE = "com.steammanifestdownloader.action.START"
        const val ACTION_STOP_SERVICE = "com.steammanifestdownloader.action.STOP"
        const val ACTION_CANCEL_DOWNLOAD = "com.steammanifestdownloader.action.CANCEL_DOWNLOAD"
        const val EXTRA_JOB_ID = "job_id"
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var isForeground = false

    private var currentGameName: String = "Steam Game"
    private var currentJobId: String? = null
    private var currentDownloadDir: String? = null

    override fun onCreate() {
        super.onCreate()
        AndroidBridgeManager.activeService = this
        acquireLocks()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CANCEL_DOWNLOAD -> {
                val jobId = intent.getStringExtra(EXTRA_JOB_ID)
                if (!jobId.isNullOrEmpty()) {
                    cancelDownloadJob(jobId)
                }
            }
            ACTION_STOP_SERVICE -> {
                stopForegroundService()
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun acquireLocks() {
        try {
            if (wakeLock == null) {
                val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
                wakeLock = powerManager.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    "SteamDownloader::DownloadWakeLock"
                ).apply {
                    setReferenceCounted(false)
                    acquire(12 * 60 * 60 * 1000L) // Max 12 hours safety timeout
                }
            }

            if (wifiLock == null) {
                val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                val lockType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                } else {
                    @Suppress("DEPRECATION")
                    WifiManager.WIFI_MODE_FULL_HIGH_PERF
                }
                wifiLock = wifiManager.createWifiLock(lockType, "SteamDownloader::WifiLock").apply {
                    setReferenceCounted(false)
                    acquire()
                }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun releaseLocks() {
        try {
            wakeLock?.let {
                if (it.isHeld) it.release()
            }
            wakeLock = null

            wifiLock?.let {
                if (it.isHeld) it.release()
            }
            wifiLock = null
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun handleDownloadEvent(eventName: String, payload: JSONObject) {
        if (eventName != "download-progress") return

        val type = payload.optString("type")
        val jobId = payload.optString("jobId")
        if (jobId.isNotEmpty()) currentJobId = jobId

        when (type) {
            "start" -> {
                currentGameName = payload.optString("gameName", "Steam Game")
                currentDownloadDir = payload.optString("downloadDir", null)
                startForegroundDownload(currentGameName, 0, "", "Preparing download…", null, jobId)
            }

            "progress", "output" -> {
                val percent = payload.optDouble("percent", -1.0).let {
                    if (it >= 0) it.toInt() else -1
                }
                var speed = payload.optString("speed", "")
                val completedBytes = payload.optLong("completedBytes", 0L)
                val totalBytes = payload.optLong("totalBytes", 0L)
                val line = payload.optString("line", "")

                if (speed.isEmpty() && line.contains("@")) {
                    speed = line.substringAfter("@").trim()
                }

                val sizeInfo = when {
                    totalBytes > 0 -> {
                        val downloadedMb = completedBytes.toDouble() / (1024.0 * 1024.0)
                        val totalMb = totalBytes.toDouble() / (1024.0 * 1024.0)
                        String.format("%.1f MB / %.1f MB", downloadedMb, totalMb)
                    }
                    line.isNotEmpty() -> line
                    else -> "Downloading chunks…"
                }

                val depotStr = if (payload.has("depotIndex") && payload.has("depotsTotal")) {
                    "Depot ${payload.optInt("depotIndex")}/${payload.optInt("depotsTotal")}"
                } else null

                if (percent >= 0 || speed.isNotEmpty() || totalBytes > 0) {
                    updateForegroundNotification(currentGameName, percent, speed, sizeInfo, depotStr, jobId)
                }
            }

            "depot_complete" -> {
                val current = payload.optInt("current", 0)
                val total = payload.optInt("total", 0)
                val depotInfo = "Depots completed: $current/$total"
                updateForegroundNotification(currentGameName, -1, "", depotInfo, null, jobId)
            }

            "complete" -> {
                vibrateFeedback(success = true)
                NotificationHelper.showCompleteNotification(this, currentGameName, currentDownloadDir)
                stopForegroundService()
            }

            "error" -> {
                val errorMsg = payload.optString("message", "Unknown download error")
                vibrateFeedback(success = false)
                NotificationHelper.showErrorNotification(this, currentGameName, errorMsg)
                stopForegroundService()
            }
        }
    }

    private fun startForegroundDownload(
        gameName: String,
        progress: Int,
        speed: String,
        sizeInfo: String,
        depotInfo: String?,
        jobId: String?
    ) {
        acquireLocks()
        val builder = NotificationHelper.buildDownloadNotification(
            this, gameName, progress, speed, sizeInfo, depotInfo, jobId
        )
        startForeground(NotificationHelper.NOTIFICATION_DOWNLOAD_ID, builder.build())
        isForeground = true
    }

    private fun updateForegroundNotification(
        gameName: String,
        progress: Int,
        speed: String,
        sizeInfo: String,
        depotInfo: String?,
        jobId: String?
    ) {
        if (!isForeground) {
            startForegroundDownload(gameName, progress, speed, sizeInfo, depotInfo, jobId)
            return
        }
        val builder = NotificationHelper.buildDownloadNotification(
            this, gameName, progress, speed, sizeInfo, depotInfo, jobId
        )
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NotificationHelper.NOTIFICATION_DOWNLOAD_ID, builder.build())
    }

    private fun stopForegroundService() {
        releaseLocks()
        if (isForeground) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            isForeground = false
        }
        stopSelf()
    }

    private fun cancelDownloadJob(jobId: String) {
        // Trigger cancel via Python invoke handler
        Thread {
            try {
                val py = com.chaquo.python.Python.getInstance()
                val appModule = py.getModule("app")
                appModule.callAttr("handle_invoke", "cancel_download", com.chaquo.python.PyObject.fromJava(mapOf("jobId" to jobId)))
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }.start()
        stopForegroundService()
    }

    private fun vibrateFeedback(success: Boolean) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vibratorManager = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                val vibrator = vibratorManager.defaultVibrator
                val effect = if (success) {
                    VibrationEffect.createPredefined(VibrationEffect.EFFECT_HEAVY_CLICK)
                } else {
                    VibrationEffect.createPredefined(VibrationEffect.EFFECT_DOUBLE_CLICK)
                }
                vibrator.vibrate(effect)
            } else {
                @Suppress("DEPRECATION")
                val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                val pattern = if (success) longArrayOf(0, 150, 100, 150) else longArrayOf(0, 300, 150, 300)
                @Suppress("DEPRECATION")
                vibrator.vibrate(pattern, -1)
            }
        } catch (_: Exception) {}
    }

    override fun onDestroy() {
        releaseLocks()
        AndroidBridgeManager.activeService = null
        super.onDestroy()
    }
}
