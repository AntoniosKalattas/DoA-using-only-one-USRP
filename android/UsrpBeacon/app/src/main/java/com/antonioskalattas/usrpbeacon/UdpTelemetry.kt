package com.antonioskalattas.usrpbeacon

import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Sends one JSON message per tick to the laptop. Two transports, picked from the "Laptop IP" field:
 *  - plain IP / hostname  -> UDP datagram to host:port (same Wi-Fi, fastest)
 *  - http(s)://… link     -> HTTP POST to <link>/phone (e.g. an ngrok tunnel to the console's
 *                            phone-only ingest on port 5080; works across networks / firewalls)
 *
 * Every message is first saved on the phone ([store]). Whatever the laptop missed (no internet on
 * either side, laptop asleep, tunnel down) is uploaded later to <link>/phone/backlog — over UDP to
 * http://<ip>:[BACKLOG_PORT]/phone/backlog — as soon as the laptop answers again.
 */
class UdpTelemetry(private val build: () -> String, private val store: TelemetryStore) {
    companion object {
        const val BACKLOG_PORT = 5080              // the console's phone-only HTTP ingest
        private const val UPLOAD_EVERY_MS = 3000L
        private const val RETRY_AFTER_FAIL_MS = 10_000L
    }

    private val exec = Executors.newSingleThreadScheduledExecutor()
    private var task: ScheduledFuture<*>? = null
    private var socket: DatagramSocket? = null
    @Volatile var sent = 0L; private set
    /** Periodic updates are only sent while this is true (the beacon is on). MARK / site messages always go. */
    @Volatile var active: () -> Boolean = { true }
    @Volatile var paused = false; private set
    private var wasActive = false
    @Volatile var lastError: String? = null; private set
    @Volatile var backlogError: String? = null; private set
    private var nextUpload = 0L

    val pending get() = store.pending
    val lastUpload get() = store.lastUpload

    private fun isHttp(host: String) = host.startsWith("http://") || host.startsWith("https://")

    private fun backlogUrl(host: String) =
        if (isHttp(host)) host.trimEnd('/') + "/phone/backlog" else "http://$host:$BACKLOG_PORT/phone/backlog"

    /** Save, then send live. Throws if the live send failed (the message stays saved for later). */
    private fun sendOne(host: String, port: Int) {
        val (line, idx) = store.append(build())
        sendBody(host, port, (line + "\n").toByteArray())
        if (isHttp(host)) store.confirmLive(idx)    // HTTP 200 = the laptop has it
    }

    /** Upload stored messages the laptop doesn't have yet (a few batches per call, never blocks long). */
    private fun drainBacklog(host: String) {
        val now = System.currentTimeMillis()
        if (now < nextUpload || store.pending == 0L) return
        try {
            var batches = 0
            while (batches < 5 && store.uploadOne { body -> postHttp(backlogUrl(host), body.toByteArray(), 20_000) }) batches++
            backlogError = null
            nextUpload = now + UPLOAD_EVERY_MS
        } catch (t: Throwable) {
            backlogError = t.message ?: t.javaClass.simpleName
            nextUpload = now + RETRY_AFTER_FAIL_MS
        }
    }

    /**
     * One message that matters (e.g. the USRP position). HTTP: the laptop's answer is reported.
     * UDP has no answer, so it is sent 3 times (the console ignores repeats by the message id).
     * Either way it is saved on the phone first, so if it fails now it is delivered later.
     * onDone(null) = delivered / sent, onDone(text) = what went wrong. Called on a background thread.
     */
    fun sendJson(host: String, port: Int, json: String, onDone: (String?) -> Unit) {
        exec.execute {
            val err = try {
                val (line, idx) = store.append(json)
                val body = (line + "\n").toByteArray()
                repeat(if (isHttp(host)) 1 else 3) { sendBody(host, port, body); if (!isHttp(host)) Thread.sleep(150) }
                if (isHttp(host)) store.confirmLive(idx)
                null
            } catch (t: Throwable) {
                t.message ?: t.javaClass.simpleName
            }
            onDone(err)
        }
    }

    private fun sendBody(host: String, port: Int, body: ByteArray) {
        if (isHttp(host)) {
            postHttp(host.trimEnd('/') + "/phone", body, 4000)
        } else {
            val s = socket ?: DatagramSocket().also { it.broadcast = true; socket = it }
            s.send(DatagramPacket(body, body.size, InetAddress.getByName(host), port))
        }
    }

    private fun postHttp(link: String, body: ByteArray, timeoutMs: Int) {
        val url = URL(link)
        val c = url.openConnection() as HttpURLConnection
        try {
            c.requestMethod = "POST"
            c.doOutput = true
            c.connectTimeout = 4000
            c.readTimeout = timeoutMs
            c.setRequestProperty("Content-Type", "application/json")
            c.setRequestProperty("ngrok-skip-browser-warning", "1")
            c.setFixedLengthStreamingMode(body.size)
            c.outputStream.use { it.write(body) }
            val code = c.responseCode
            (if (code in 200..299) c.inputStream else c.errorStream)?.use { it.readBytes() }
            if (code !in 200..299) throw IOException("HTTP $code from $url")
        } finally {
            c.disconnect()
        }
    }

    fun start(host: String, port: Int, rateHz: Double) {
        stop()
        // HTTP through a tunnel has ~100-300 ms round trips; 4 Hz keeps it from backing up.
        val hz = if (isHttp(host)) minOf(rateHz, 4.0) else rateHz
        val periodMs = (1000.0 / hz).toLong().coerceAtLeast(20)
        nextUpload = 0L
        wasActive = false
        task = exec.scheduleWithFixedDelay({
            val on = runCatching { active() }.getOrDefault(true)
            // beacon off: send nothing, except ONE last update right after it was switched off
            // (so the laptop sees adv_on = false and the blink timeline ends cleanly)
            if (on || wasActive) {
                try {
                    sendOne(host, port)
                    sent++; lastError = null
                } catch (t: Throwable) {
                    lastError = t.message ?: t.javaClass.simpleName
                }
            }
            wasActive = on; paused = !on
            // Over HTTP: only when the live link works again. Over UDP there's no answer, so try anyway.
            if (lastError == null) runCatching { drainBacklog(host) }
        }, 0, periodMs, TimeUnit.MILLISECONDS)
    }

    /** Fire one extra message right now (used for MARK events). */
    fun sendNow(host: String, port: Int) {
        exec.execute {
            try { sendOne(host, port); sent++ } catch (t: Throwable) { lastError = t.message }
        }
    }

    /** Clear stored (not yet uploaded) updates on the worker thread; [done] gets the count, on that thread. */
    fun clearStored(beforeMs: Long?, done: (Long) -> Unit) {
        exec.execute { done(runCatching { store.clear(beforeMs) }.getOrDefault(0L)) }
    }

    fun stop() {
        task?.cancel(false); task = null
        exec.execute { runCatching { store.flush() } }
    }

    fun close() {
        stop(); exec.execute { socket?.close(); socket = null }; exec.shutdown()
    }
}
