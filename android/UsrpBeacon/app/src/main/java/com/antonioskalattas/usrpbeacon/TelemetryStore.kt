package com.antonioskalattas.usrpbeacon

import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * Every message the app sends to the laptop is first written here, on the phone
 * (files/telemetry/<start>_<sid>.jsonl, one JSON per line). Nothing is lost when the phone or the
 * laptop drops off the internet: whatever the laptop has not confirmed is uploaded later, in
 * batches, to <laptop>/phone/backlog. The laptop skips anything it already has (by sid + seq).
 *
 * "Confirmed" moves forward in one line order per file (a cursor kept in <file>.sent):
 *  - a live HTTP send that the laptop answered 200,
 *  - a backlog batch that the laptop answered 200.
 * Live UDP sends get no answer, so over UDP everything is also uploaded through the backlog
 * (the laptop drops the copies it already got by UDP).
 *
 * Not thread-safe on purpose: UdpTelemetry calls it only from its single worker thread.
 * [pending] / [stored] are read by the UI.
 */
class TelemetryStore(baseDir: File) {
    companion object {
        const val BATCH = 300                      // lines per backlog upload (~300 kB)
        private const val KEEP_DAYS = 30           // fully uploaded files older than this are deleted
    }

    private val dir = File(baseDir, "telemetry").apply { mkdirs() }
    val sid: String = UUID.randomUUID().toString().substring(0, 8)
    private val file = File(dir, SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date()) + "_" + sid + ".jsonl")

    // current session: byte offset where each line starts, plus confirmations ahead of the cursor
    private val offsets = ArrayList<Long>()
    private var size = 0L
    private var seq = 0L
    private var acked = 0                          // lines 0 until acked-1 are on the laptop
    private val ahead = HashSet<Int>()             // line indexes confirmed out of order (live sends)
    private var lastSave = 0L

    // older sessions (from earlier app runs) that still have lines to upload
    private data class Old(val f: File, var acked: Int, var offset: Long, val total: Int)
    private val old = ArrayList<Old>()

    @Volatile var pending = 0L; private set        // lines on the phone that the laptop doesn't have yet
    @Volatile var stored = 0L; private set         // lines written this session
    @Volatile var lastUpload: String? = null; private set

    init {
        val cutoff = System.currentTimeMillis() - KEEP_DAYS * 86_400_000L
        dir.listFiles { f -> f.name.endsWith(".jsonl") && f != file }?.sortedBy { it.name }?.forEach { f ->
            val total = f.useLines { it.count() }
            val (a, off) = readCursor(f)
            when {
                a < total -> old += Old(f, a, off, total)
                f.lastModified() < cutoff -> { f.delete(); cursorFile(f).delete() }
            }
        }
        recount()
    }

    /** Stamp the message with sid/seq, write it to the phone, return (stamped json, line index). */
    fun append(json: String): Pair<String, Int> {
        val o = JSONObject(json)
        o.put("sid", sid); o.put("seq", ++seq)
        val line = o.toString()
        val bytes = (line + "\n").toByteArray()
        file.appendBytes(bytes)
        offsets += size; size += bytes.size
        stored = offsets.size.toLong(); recount()
        return line to offsets.size - 1
    }

    /** The laptop answered this live send. */
    fun confirmLive(index: Int) {
        if (index < acked) return
        ahead += index
        while (ahead.remove(acked)) acked++
        saveCursor(false); recount()
    }

    /**
     * Upload one batch of unconfirmed lines (older sessions first). [post] sends the body and
     * throws on failure. Returns true when something was uploaded (call again for the next batch).
     */
    fun uploadOne(post: (String) -> Unit): Boolean {
        val o = old.firstOrNull()
        if (o != null) {
            val (lines, newOff) = readLines(o.f, o.offset, BATCH)
            if (lines.isEmpty()) { old.remove(o); recount(); return true }
            post(body(lines))
            o.acked += lines.size; o.offset = newOff
            writeCursor(o.f, o.acked, o.offset)
            if (o.acked >= o.total) old.remove(o)
            lastUpload = "${lines.size} older updates uploaded"
            recount(); return true
        }
        // skip lines that the live link already delivered (they are ahead of the cursor)
        while (acked < offsets.size && ahead.remove(acked)) acked++
        if (acked >= offsets.size) return false
        var end = acked
        while (end < offsets.size && end - acked < BATCH && end !in ahead) end++
        val from = offsets[acked]
        val to = if (end < offsets.size) offsets[end] else size
        val text = RandomAccessFile(file, "r").use { r ->
            val b = ByteArray((to - from).toInt()); r.seek(from); r.readFully(b); String(b)
        }
        val lines = text.split('\n').filter { it.isNotBlank() }
        post(body(lines))
        acked = end
        while (ahead.remove(acked)) acked++
        saveCursor(true)
        lastUpload = "${lines.size} stored updates uploaded"
        recount(); return true
    }

    fun flush() = saveCursor(true)

    /**
     * "Clear stored packets" button.
     * [beforeMs] = null: forget EVERYTHING not yet uploaded (older sessions' files are deleted).
     * [beforeMs] = a time: only skip what was measured before it (by each line's t_ms); newer stays queued.
     * Returns how many lines were dropped.
     */
    fun clear(beforeMs: Long?): Long {
        var dropped = 0L
        for (o in ArrayList(old)) {
            if (beforeMs == null) {
                dropped += o.total - o.acked
                o.f.delete(); cursorFile(o.f).delete(); old.remove(o); continue
            }
            RandomAccessFile(o.f, "r").use { r ->
                r.seek(o.offset)
                while (o.acked < o.total) {
                    val start = r.filePointer
                    val raw = r.readLine() ?: break
                    if (raw.isNotBlank() && tMs(raw) >= beforeMs) { r.seek(start); break }
                    o.acked++; o.offset = r.filePointer; dropped++
                }
            }
            writeCursor(o.f, o.acked, o.offset)
            if (o.acked >= o.total) old.remove(o)
        }
        // this session: move the cursor forward over the dropped lines
        while (acked < offsets.size) {
            if (acked !in ahead) {
                if (beforeMs != null) {
                    val to = if (acked + 1 < offsets.size) offsets[acked + 1] else size
                    val line = RandomAccessFile(file, "r").use { r ->
                        val b = ByteArray((to - offsets[acked]).toInt()); r.seek(offsets[acked]); r.readFully(b); String(b)
                    }
                    if (tMs(line) >= beforeMs) break
                }
                dropped++
            }
            ahead.remove(acked); acked++
        }
        saveCursor(true); recount()
        lastUpload = "cleared $dropped stored updates"
        return dropped
    }

    /** t_ms of one stored line (0 if missing, so it counts as old). */
    private fun tMs(line: String): Long = runCatching { JSONObject(line.trim()).optLong("t_ms", 0L) }.getOrDefault(0L)

    private fun body(lines: List<String>) = "{\"items\":[" + lines.joinToString(",") + "]}"

    private fun recount() {
        pending = (offsets.size - acked).toLong() - ahead.size + old.sumOf { (it.total - it.acked).toLong() }
    }

    private fun saveCursor(force: Boolean) {
        val now = System.currentTimeMillis()
        if (!force && now - lastSave < 2000) return
        lastSave = now
        writeCursor(file, acked, if (acked < offsets.size) offsets[acked] else size)
    }

    private fun cursorFile(f: File) = File(f.path + ".sent")

    private fun writeCursor(f: File, lines: Int, offset: Long) {
        runCatching {
            val tmp = File(f.path + ".sent.tmp")
            tmp.writeText("$lines $offset")
            tmp.renameTo(cursorFile(f))
        }
    }

    private fun readCursor(f: File): Pair<Int, Long> = runCatching {
        val p = cursorFile(f).readText().trim().split(" ")
        p[0].toInt() to p[1].toLong()
    }.getOrDefault(0 to 0L)

    /** Up to [max] lines starting at byte [offset]; returns them and the offset after the last one. */
    private fun readLines(f: File, offset: Long, max: Int): Pair<List<String>, Long> {
        RandomAccessFile(f, "r").use { r ->
            r.seek(offset)
            val out = ArrayList<String>()
            var pos = offset
            while (out.size < max) {
                val start = r.filePointer
                val raw = r.readLine() ?: break        // ISO-8859-1 bytes: re-decode as UTF-8
                pos = r.filePointer
                if (raw.isNotBlank()) out += String(raw.toByteArray(Charsets.ISO_8859_1), Charsets.UTF_8)
                if (pos == start) break
            }
            return out to pos
        }
    }
}
