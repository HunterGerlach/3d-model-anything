package com.wesbos.housecapture.bundle

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import java.io.File
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Zips a session into an export directory allocated by [SessionFiles], with manifest.json at the root. */
object ZipShare {
    /** already-compressed files are stored, everything else deflated */
    private val STORED = setOf("jpg", "f16z")

    fun zip(folder: File, dir: File, name: String): File {
        val safe = name.replace('/', '-').replace(':', '-').ifBlank { "Room" }
        val dest = File(dir, "$safe.zip")
        try {
            ZipOutputStream(dest.outputStream().buffered()).use { zip ->
                folder.walkTopDown().filter { it.isFile && !it.name.endsWith(".tmp") }.sortedBy { it.path }.forEach { file ->
                    val entry = ZipEntry(file.relativeTo(folder).invariantSeparatorsPath)
                    if (file.extension.lowercase() in STORED) {
                        entry.method = ZipEntry.STORED
                        entry.size = file.length()
                        entry.compressedSize = file.length()
                        entry.crc = crc32(file)
                    }
                    zip.putNextEntry(entry)
                    file.inputStream().use { it.copyTo(zip) }
                    zip.closeEntry()
                }
            }
        } catch (e: Exception) {
            try {
                deleteTreeChecked(dir)
            } catch (cleanup: Exception) {
                e.addSuppressed(cleanup)
            }
            throw e
        }
        return dest
    }

    fun uri(context: Context, file: File): Uri =
        FileProvider.getUriForFile(context, "${context.packageName}.files", file)

    /** ACTION_SEND for a zip Uri from [uri]; wrap it in Intent.createChooser to show the share sheet. */
    fun sendIntent(uri: Uri): Intent = Intent(Intent.ACTION_SEND)
        .setType("application/zip")
        .putExtra(Intent.EXTRA_STREAM, uri)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

    private fun crc32(file: File): Long {
        val crc = CRC32()
        val buffer = ByteArray(64 * 1024)
        file.inputStream().use { input ->
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                crc.update(buffer, 0, n)
            }
        }
        return crc.value
    }
}
