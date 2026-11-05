package com.steammanifestdownloader

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

class SteamDownloaderApp : Application() {

    override fun onCreate() {
        super.onCreate()
        AndroidBridgeManager.appContext = this

        // Create notification channels on startup
        NotificationHelper.createNotificationChannels(this)

        // Initialize Chaquopy CPython 3.11 runtime
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        // Configure environment in Python for direct in-memory engine
        initPythonEnvironment()
    }

    private fun initPythonEnvironment() {
        try {
            val py = Python.getInstance()
            val osModule = py.getModule("os")
            val environ = osModule.get("environ")
            val dataDir = File(filesDir, "smd_data").apply { mkdirs() }

            environ?.callAttr("__setitem__", "SMD_DATA_DIR", dataDir.absolutePath)
            environ?.callAttr("__setitem__", "SMD_DOWNLOAD_DIR", FileUtils.getDefaultDownloadDirectory())
            environ?.callAttr("__setitem__", "IS_ANDROID_APP", "1")
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }
}
