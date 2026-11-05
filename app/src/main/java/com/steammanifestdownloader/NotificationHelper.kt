package com.steammanifestdownloader

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object NotificationHelper {
    const val CHANNEL_DOWNLOAD_ID = "steam_download_channel"
    const val CHANNEL_ALERTS_ID = "steam_alerts_channel"

    const val NOTIFICATION_DOWNLOAD_ID = 1001
    const val NOTIFICATION_COMPLETE_ID = 2001
    const val NOTIFICATION_ERROR_ID = 3001

    fun createNotificationChannels(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val notificationManager =
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

            // Active download progress channel (low importance = no sound per progress tick)
            val downloadChannel = NotificationChannel(
                CHANNEL_DOWNLOAD_ID,
                context.getString(R.string.channel_download_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = context.getString(R.string.channel_download_desc)
                setShowBadge(false)
                enableVibration(false)
            }

            // Alerts channel (high importance for completion/error notifications)
            val alertsChannel = NotificationChannel(
                CHANNEL_ALERTS_ID,
                context.getString(R.string.channel_alerts_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = context.getString(R.string.channel_alerts_desc)
                setShowBadge(true)
                enableVibration(true)
            }

            notificationManager.createNotificationChannel(downloadChannel)
            notificationManager.createNotificationChannel(alertsChannel)
        }
    }

    fun buildDownloadNotification(
        context: Context,
        gameName: String,
        progressPercent: Int,
        speedStr: String,
        sizeInfo: String,
        depotInfo: String?,
        jobId: String?
    ): NotificationCompat.Builder {
        val openAppIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val contentPendingIntent = PendingIntent.getActivity(
            context,
            0,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val builder = NotificationCompat.Builder(context, CHANNEL_DOWNLOAD_ID)
            .setSmallIcon(R.drawable.ic_stat_download)
            .setContentTitle("Downloading: $gameName")
            .setContentText(if (speedStr.isNotEmpty()) "$speedStr • $sizeInfo" else sizeInfo)
            .setSubText(depotInfo)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(contentPendingIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)

        if (progressPercent >= 0) {
            builder.setProgress(100, progressPercent.coerceIn(0, 100), false)
        } else {
            builder.setProgress(0, 0, true)
        }

        // Add Cancel action button
        if (!jobId.isNullOrEmpty()) {
            val cancelIntent = Intent(context, SteamDownloaderService::class.java).apply {
                action = SteamDownloaderService.ACTION_CANCEL_DOWNLOAD
                putExtra(SteamDownloaderService.EXTRA_JOB_ID, jobId)
            }
            val cancelPendingIntent = PendingIntent.getService(
                context,
                jobId.hashCode(),
                cancelIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            builder.addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                context.getString(R.string.action_cancel),
                cancelPendingIntent
            )
        }

        return builder
    }

    fun showCompleteNotification(
        context: Context,
        gameName: String,
        downloadDir: String?
    ) {
        val openIntent = if (!downloadDir.isNullOrEmpty()) {
            FileUtils.createOpenFolderIntent(context, downloadDir)
        } else {
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            }
        }

        val pendingIntent = PendingIntent.getActivity(
            context,
            NOTIFICATION_COMPLETE_ID,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ALERTS_ID)
            .setSmallIcon(R.drawable.ic_stat_complete)
            .setContentTitle(context.getString(R.string.notification_complete_title))
            .setContentText("$gameName has finished downloading!")
            .setStyle(
                NotificationCompat.BigTextStyle()
                    .bigText("Download complete for $gameName.\nSaved to: ${downloadDir ?: "Downloads"}")
            )
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .build()

        NotificationManagerCompat.from(context).notify(NOTIFICATION_COMPLETE_ID, notification)
    }

    fun showErrorNotification(
        context: Context,
        gameName: String,
        errorMessage: String
    ) {
        val openAppIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            context,
            NOTIFICATION_ERROR_ID,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ALERTS_ID)
            .setSmallIcon(R.drawable.ic_stat_error)
            .setContentTitle(context.getString(R.string.notification_error_title))
            .setContentText("$gameName: $errorMessage")
            .setStyle(
                NotificationCompat.BigTextStyle()
                    .bigText("Error downloading $gameName:\n$errorMessage\nYour progress is saved - run again to resume.")
            )
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_ERROR)
            .build()

        NotificationManagerCompat.from(context).notify(NOTIFICATION_ERROR_ID, notification)
    }
}
