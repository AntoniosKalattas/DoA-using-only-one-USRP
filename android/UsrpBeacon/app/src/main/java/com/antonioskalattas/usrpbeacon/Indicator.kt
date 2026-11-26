package com.antonioskalattas.usrpbeacon

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.graphics.Color
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.os.Handler
import android.os.HandlerThread

/**
 * Lights up while the beacon is transmitting.
 *
 * Android never tells an app the exact moment each BLE packet leaves the radio, so the
 * light follows the app's own state/timing, not the radio itself:
 *  - LED:   the notification LED flashes in short pulses (one per packet interval, at most
 *           ~2.5/s) during the ON part of the blink cycle and stays dark during the OFF part.
 *           Android only lights it while the SCREEN IS OFF.
 *  - TORCH: the camera flashlight pulses once per advertising interval while transmitting
 *           (software-timed, so roughly in step with the packets, not exactly).
 */
class Indicator(private val ctx: Context, private val beacon: Beacon) {

    enum class Mode { OFF, LED, TORCH }

    private val thread = HandlerThread("indicator").apply { start() }
    private val h = Handler(thread.looper)
    private val nm = ctx.getSystemService(NotificationManager::class.java)
    private val cm = ctx.getSystemService(CameraManager::class.java)
    private val torchId: String? = runCatching {
        cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        }
    }.getOrNull()

    @Volatile var mode = Mode.OFF
        set(v) { field = v; h.post { allOff() } }
    @Volatile var lastError: String? = null; private set

    private var ledShown = false
    private var torchOn = false

    val hasTorch get() = torchId != null

    init {
        val ch = NotificationChannel(CHANNEL, "Transmit light", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Lights the notification LED while the beacon transmits"
            enableLights(true)
            lightColor = Color.BLUE
            setSound(null, null)
            enableVibration(false)
            setShowBadge(false)
        }
        nm.createNotificationChannel(ch)
    }

    private val tick = object : Runnable {
        override fun run() {
            val on = beacon.running && beacon.radiating
            var next = 100L
            when (mode) {
                Mode.LED -> if (on) {
                    // short pulse: light, then dark. Capped at ~2.5 Hz because Android throttles
                    // apps that post/cancel notifications faster than ~5 times a second.
                    val period = beacon.intervalMs.toLong().coerceAtLeast(LED_MIN_PERIOD_MS)
                    showLed()
                    h.postDelayed({ hideLed() }, LED_ON_MS)
                    next = period
                } else if (ledShown) hideLed()
                Mode.TORCH -> if (on) {
                    val interval = beacon.intervalMs.toLong().coerceAtLeast(100)
                    torch(true)
                    h.postDelayed({ torch(false) }, minOf(60L, interval / 2))
                    next = interval
                } else if (torchOn) torch(false)
                Mode.OFF -> {}
            }
            h.postDelayed(this, next)
        }
    }

    init { h.post(tick) }

    private fun showLed() {
        val n = Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("USRP Beacon is transmitting")
            .setContentText("The LED is on while Bluetooth packets are going out")
            .setCategory(Notification.CATEGORY_STATUS)
            .setTimeoutAfter(LED_ON_MS + 300)       // safety: never leave the LED stuck on
            .build()
        runCatching { nm.notify(NOTIF_ID, n) }.onFailure { lastError = it.message }
        ledShown = true
    }

    private fun hideLed() {
        nm.cancel(NOTIF_ID)
        ledShown = false
    }

    private fun torch(on: Boolean) {
        val id = torchId ?: return
        runCatching { cm.setTorchMode(id, on); torchOn = on }.onFailure { lastError = it.message }
    }

    private fun allOff() {
        h.removeCallbacksAndMessages(null)
        if (ledShown) hideLed()
        if (torchOn) torch(false)
        h.post(tick)
    }

    fun release() {
        h.removeCallbacksAndMessages(null)
        h.post { if (ledShown) hideLed(); if (torchOn) torch(false); thread.quitSafely() }
    }

    companion object {
        private const val CHANNEL = "tx_light"
        private const val NOTIF_ID = 42
        private const val LED_ON_MS = 150L
        private const val LED_MIN_PERIOD_MS = 400L
    }
}
