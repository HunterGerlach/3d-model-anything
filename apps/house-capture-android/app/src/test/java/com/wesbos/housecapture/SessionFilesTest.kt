package com.wesbos.housecapture

import com.wesbos.housecapture.bundle.ScanBundle
import com.wesbos.housecapture.bundle.SessionFiles
import com.wesbos.housecapture.bundle.ZipShare
import com.wesbos.housecapture.bundle.deleteTreeChecked
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.UUID
import java.util.zip.ZipFile

class SessionFilesTest {
    @get:Rule
    val temp = TemporaryFolder()

    private class Fixture(val root: File, remove: (File) -> Boolean = File::delete) {
        val sessions = File(root, "sessions")
        val share = File(root, "cache/share")
        val revoked = ArrayList<File>()
        var revokeFails = false
        val files = SessionFiles(sessions, share, revoke = { file ->
            assertTrue("revoke before unlinking the export", file.exists())
            if (revokeFails) throw SecurityException("Simulated revocation failure")
            revoked.add(file)
        }, remove = remove)

        fun room(id: String): File = files.session(id).also { dir ->
            assertTrue(File(dir, "photos").mkdirs())
            File(dir, "photos/0000.jpg").writeText("Private test photo for $id")
            File(dir, "manifest.json").writeText("{\"session\":\"$id\"}")
        }

        fun export(id: String): File = ZipShare.zip(files.session(id), files.newExport(id), "Same room name")
    }

    @Test
    fun multipleExportsAreCompleteAndIsolatedBySession() {
        val f = Fixture(temp.newFolder())
        val roomA = f.room("room-a")
        val roomB = f.room("room-b")
        val first = f.export("room-a")
        val second = f.export("room-a")
        val other = f.export("room-b")
        assertNotEquals(first.parentFile, second.parentFile)
        assertEquals("room-a", first.parentFile.parentFile.name)
        assertEquals("room-b", other.parentFile.parentFile.name)
        ZipFile(first).use { zip ->
            assertEquals(setOf("manifest.json", "photos/0000.jpg"), zip.entries().asSequence().map { it.name }.toSet())
            assertEquals("Private test photo for room-a", zip.getInputStream(zip.getEntry("photos/0000.jpg")).bufferedReader().use { it.readText() })
        }

        f.files.delete("room-a")

        assertFalse(roomA.exists())
        assertFalse(first.exists())
        assertFalse(second.exists())
        assertEquals(setOf(first, second), f.revoked.toSet())
        assertTrue(roomB.isDirectory)
        assertTrue(other.isFile)
        // Deletion is safe to retry after the session and its exports are already gone.
        f.files.delete("room-a")
        assertTrue(other.isFile)
    }

    @Test
    fun cancellingTheChooserCannotLeaveAnExportAfterRoomDeletion() {
        val f = Fixture(temp.newFolder())
        f.room("room-a")
        // Preparing the ZIP creates a copy even when no chooser target ever receives a grant.
        val neverShared = f.export("room-a")
        f.files.delete("room-a")
        assertFalse(neverShared.exists())
        assertEquals(listOf(neverShared), f.revoked)
    }

    @Test
    fun openingTheLibraryCleansLegacyCopiesWithoutTouchingCurrentOrUnrelatedFiles() {
        val f = Fixture(temp.newFolder())
        val room = f.room("room-a")
        val current = f.export("room-a")
        val legacy = File(f.share, "${UUID.randomUUID()}/Old room.zip")
        assertTrue(legacy.parentFile.mkdirs())
        legacy.writeText("old private export")
        val unrelated = File(f.root, "cache/unrelated.txt").apply { writeText("keep") }

        f.files.cleanup()

        assertFalse(legacy.parentFile.exists())
        assertEquals(listOf(legacy), f.revoked)
        assertTrue(current.isFile)
        assertTrue(room.isDirectory)
        assertTrue(unrelated.isFile)
    }

    @Test
    fun deletionAlsoRemovesUnassociatedLegacyExports() {
        val f = Fixture(temp.newFolder())
        f.room("room-a")
        val legacy = File(f.share, "${UUID.randomUUID()}/Old room.zip")
        assertTrue(legacy.parentFile.mkdirs())
        legacy.writeText("old private export")

        f.files.delete("room-a")

        assertFalse(legacy.exists())
        assertEquals(listOf(legacy), f.revoked)
    }

    @Test
    fun oldExportsAreRevokedDuringLibraryCleanupWithoutAnotherShare() {
        val f = Fixture(temp.newFolder())
        val room = f.room("room-a")
        val old = f.export("room-a")
        val fresh = f.export("room-a")
        val now = System.currentTimeMillis()
        assertTrue(old.parentFile.setLastModified(now - 2 * 86_400_000L))

        f.files.cleanup(now)

        assertFalse(old.exists())
        assertEquals(listOf(old), f.revoked)
        assertTrue(fresh.isFile)
        assertTrue(room.isDirectory)
    }

    @Test
    fun failedExportDeletionReportsFailureAndKeepsTheOriginalScan() {
        val f = Fixture(temp.newFolder(), remove = { file ->
            if (file.extension == "zip") false else file.delete()
        })
        val room = f.room("room-a")
        val export = f.export("room-a")

        assertThrows(IOException::class.java) { f.files.delete("room-a") }

        assertTrue(export.isFile)
        assertTrue(File(room, "manifest.json").isFile)
        assertTrue(File(room, "photos/0000.jpg").isFile)
        assertEquals(listOf(export), f.revoked)
    }

    @Test
    fun failedRevocationStopsDeletionAndKeepsTheScanAvailableForRetry() {
        val f = Fixture(temp.newFolder())
        val room = f.room("room-a")
        val export = f.export("room-a")
        f.revokeFails = true

        assertThrows(SecurityException::class.java) { f.files.delete("room-a") }

        assertTrue(export.isFile)
        assertTrue(File(room, "manifest.json").isFile)
        f.revokeFails = false
        f.files.delete("room-a")
        assertFalse(room.exists())
        assertFalse(export.exists())
    }

    @Test
    fun failedPhotoDeletionKeepsTheManifestSoTheRoomCanBeDeletedAgain() {
        val f = Fixture(temp.newFolder(), remove = { file ->
            if (file.name == "0000.jpg") false else file.delete()
        })
        val room = f.room("room-a")
        val export = f.export("room-a")

        assertThrows(IOException::class.java) { f.files.delete("room-a") }

        assertFalse(export.exists())
        assertTrue(File(room, "photos/0000.jpg").isFile)
        assertTrue(File(room, "manifest.json").isFile)
        SessionFiles(f.sessions, f.share, revoke = {}).delete("room-a")
        assertFalse(room.exists())
    }

    @Test
    fun traversalAndAliasesCannotSelectAnotherSession() {
        val f = Fixture(temp.newFolder())
        val room = f.room("room-a")
        for (id in listOf("", " ", ".", "..", "../room-a", "/room-a", "x/../room-a", "x\\room-a", "bad\u0000name")) {
            assertThrows("Rejected session id", IllegalArgumentException::class.java) { f.files.delete(id) }
        }
        Files.createSymbolicLink(File(f.sessions, "alias").toPath(), room.toPath())
        assertThrows(IllegalArgumentException::class.java) { f.files.delete("alias") }
        assertTrue(File(room, "photos/0000.jpg").isFile)
    }

    @Test
    fun legacyCleanupUnlinksSymlinksWithoutFollowingThem() {
        val f = Fixture(temp.newFolder())
        f.room("room-a")
        val outside = File(f.root, "outside").apply { assertTrue(mkdir()) }
        val keep = File(outside, "keep.txt").apply { writeText("outside the export cache") }
        val legacy = File(f.share, UUID.randomUUID().toString()).apply { assertTrue(mkdirs()) }
        Files.createSymbolicLink(File(legacy, "linked").toPath(), outside.toPath())

        f.files.cleanup()

        assertFalse(legacy.exists())
        assertTrue(keep.isFile)
        assertTrue(f.revoked.isEmpty())
    }

    @Test
    fun queuedCheckpointsCannotRecreateADeletedRoom() {
        val bundle = ScanBundle.create(temp.newFolder())
        bundle.writeText("photos/photos.json", "[]")
        deleteTreeChecked(bundle.dir)

        assertThrows(IOException::class.java) { bundle.writeText("photos/photos.json", "[]") }
        assertFalse(bundle.dir.exists())
    }
}
