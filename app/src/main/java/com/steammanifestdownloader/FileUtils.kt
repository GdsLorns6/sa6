package com.steammanifestdownloader

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.DocumentsContract
import androidx.core.content.FileProvider
import java.io.File

object FileUtils {

    fun getDefaultDownloadDirectory(): String {
        val downloadDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        val steamDir = File(downloadDir, "SteamDownloads")
        if (!steamDir.exists()) {
            steamDir.mkdirs()
        }
        return steamDir.absolutePath
    }

    fun createOpenFolderIntent(context: Context, folderPath: String): Intent {
        val folder = File(folderPath)
        val intent = Intent(Intent.ACTION_VIEW)

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                // Try DocumentsContract folder URI
                val uri = DocumentsContract.buildDocumentUri(
                    "${context.packageName}.provider",
                    folder.absolutePath
                )
                intent.setDataAndType(uri, DocumentsContract.Document.MIME_TYPE_DIR)
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION)
            } else {
                val uri = FileProvider.getUriForFile(
                    context,
                    "${context.packageName}.provider",
                    folder
                )
                intent.setDataAndType(uri, "resource/folder")
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        } catch (_: Exception) {
            // Fallback to standard generic file viewer
            intent.setDataAndType(Uri.fromFile(folder), "*/*")
        }

        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return intent
    }

    fun openFolder(context: Context, folderPath: String): Boolean {
        return try {
            val intent = createOpenFolderIntent(context, folderPath)
            context.startActivity(intent)
            true
        } catch (e: Exception) {
            try {
                // Secondary fallback: open system Downloads
                val fallbackIntent = Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(
                        Uri.parse(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).path),
                        "*/*"
                    )
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                context.startActivity(fallbackIntent)
                true
            } catch (e2: Exception) {
                false
            }
        }
    }
}
