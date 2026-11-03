package com.wesbos.housecapture.bundle

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.UUID

/**
 * The lifetime of a scan and its local export copies. Callers serialize export creation/writing,
 * cleanup and deletion so a share cannot finish after the user has deleted its room.
 * Kept independent of Android so cleanup and failure handling can be tested on the JVM.
 */
internal class SessionFiles(
    sessionsRoot: File,
    shareRoot: File,
    private val revoke: (File) -> Unit,
    private val remove: (File) -> Boolean = File::delete,
) {
    private val sessionsRoot = checkedRoot(sessionsRoot)
    private val shareRoot = checkedRoot(shareRoot)

    /** Only a plain, non-symlink session directory immediately inside the sessions root. */
    fun session(sessionId: String): File = child(sessionsRoot, sessionId)

    fun newExport(sessionId: String, now: Long = System.currentTimeMillis()): File {
        val session = session(sessionId)
        check(session.isDirectory) { "This room is no longer on the phone." }
        cleanup(now)
        val group = child(child(shareRoot, LAYOUT), session.name)
        val dir = child(group, UUID.randomUUID().toString())
        if (!dir.mkdirs()) throw IOException("Could not prepare the room export.")
        return dir
    }

    /** Revoke and remove exports first; a failure leaves the scan available for a retry. */
    fun delete(sessionId: String) {
        val session = session(sessionId)
        removeLegacyExports()
        val exports = child(child(shareRoot, LAYOUT), session.name)
        deleteTreeChecked(exports, beforeFile = revoke, remove = remove)
        deleteTreeChecked(session, remove = remove)
    }

    /** Runs when the library opens and before an export; age cleanup is not a promised expiry. */
    fun cleanup(now: Long = System.currentTimeMillis()) {
        removeLegacyExports()
        val groups = child(shareRoot, LAYOUT)
        val cutoff = now - DAY_MS
        for (group in children(groups)) {
            if (Files.isSymbolicLink(group.toPath()) || !group.isDirectory) {
                deleteTreeChecked(group, beforeFile = revoke, remove = remove)
                continue
            }
            for (export in children(group)) {
                if (export.lastModified() < cutoff) {
                    deleteTreeChecked(export, beforeFile = revoke, remove = remove)
                }
            }
            if (children(group).isEmpty()) deleteTreeChecked(group, remove = remove)
        }
    }

    /** Old UUID-only exports cannot be associated with a room, so migrate by removing all of them. */
    private fun removeLegacyExports() {
        for (entry in children(shareRoot)) {
            if (entry.name != LAYOUT) deleteTreeChecked(entry, beforeFile = revoke, remove = remove)
        }
    }

    private companion object {
        const val LAYOUT = "by-session"
        const val DAY_MS = 86_400_000L

        fun checkedRoot(root: File): File {
            require(!Files.isSymbolicLink(root.toPath())) { "Unexpected linked storage directory." }
            return root.canonicalFile
        }

        fun child(parent: File, name: String): File {
            require(name.isNotBlank() && name != "." && name != ".." && name.none { it == '/' || it == '\\' || it == '\u0000' }) {
                "Unknown session."
            }
            val root = parent.canonicalFile
            val file = File(root, name)
            require(!Files.isSymbolicLink(file.toPath()) && file.canonicalFile.parentFile == root) { "Unknown session." }
            return file.canonicalFile
        }

        fun children(dir: File): List<File> {
            if (!dir.exists() && !Files.isSymbolicLink(dir.toPath())) return emptyList()
            if (Files.isSymbolicLink(dir.toPath())) throw IOException("Unexpected linked storage directory.")
            return dir.listFiles()?.toList() ?: throw IOException("Could not read saved files.")
        }
    }
}

/** Deletes without following links; failures throw, and a room's manifest is removed last. */
internal fun deleteTreeChecked(
    file: File,
    beforeFile: (File) -> Unit = {},
    remove: (File) -> Boolean = File::delete,
) {
    val linked = Files.isSymbolicLink(file.toPath())
    if (!linked && !file.exists()) return
    if (!linked && file.isDirectory) {
        val children = file.listFiles() ?: throw IOException("Could not read saved files for deletion.")
        children.sortedBy { it.name == "manifest.json" }.forEach { deleteTreeChecked(it, beforeFile, remove) }
    } else if (!linked) {
        beforeFile(file)
    }
    if (!remove(file) && (file.exists() || Files.isSymbolicLink(file.toPath()))) {
        throw IOException("Some saved files could not be deleted. Please try again.")
    }
}
