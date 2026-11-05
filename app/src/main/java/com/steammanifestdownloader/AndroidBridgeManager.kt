package com.steammanifestdownloader

import android.content.Context
import org.json.JSONObject

object AndroidBridgeManager {
    @Volatile
    var appContext: Context? = null

    @Volatile
    var activeService: SteamDownloaderService? = null

    @Volatile
    var activeActivity: MainActivity? = null

    @JvmStatic
    fun onDownloadEvent(eventName: String, payloadJson: String) {
        try {
            val payload = JSONObject(payloadJson)
            // Dispatch to Foreground Service for persistent notification updates
            activeService?.handleDownloadEvent(eventName, payload)

            // Dispatch to Activity UI if active
            activeActivity?.runOnUiThread {
                activeActivity?.handleBridgeEvent(eventName, payload)
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    @JvmStatic
    fun openFolder(folderPath: String): Boolean {
        appContext?.let {
            return FileUtils.openFolder(it, folderPath)
        }
        return false
    }

    @JvmStatic
    fun getDownloadDirectory(): String {
        return FileUtils.getDefaultDownloadDirectory()
    }
}
