package com.antonioskalattas.usrpbeacon

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertisingSet
import android.bluetooth.le.AdvertisingSetCallback
import android.bluetooth.le.AdvertisingSetParameters
import android.content.Context
import android.os.Handler
import android.os.Looper

/**
 * Legacy (BT4-style) non-connectable, non-scannable BLE advertiser = ADV_NONCONN_IND.
 * Each advertising event is one short packet on ch 37 (2402), 38 (2426) and 39 (2480 MHz),
 * repeated every [intervalMs] (+0-10 ms random advDelay added by the controller).
 *
 * "Blink" mode keys the whole beacon on/off with a known period so you can pick your
 * phone out of all the other BLE traffic the B210 sees (correlate with adv_on in the UDP stream).
 */
@SuppressLint("MissingPermission")
class Beacon(ctx: Context, private val onState: (String) -> Unit) {

    private val adapter = ctx.getSystemService(BluetoothManager::class.java)?.adapter
    private val main = Handler(Looper.getMainLooper())
    private var set: AdvertisingSet? = null
    private var callback: AdvertisingSetCallback? = null
    private var blinkPeriodMs = 0L

    @Volatile var running = false; private set      // advertising set exists
    @Volatile var radiating = false; private set    // actually transmitting right now
    @Volatile var intervalMs = 0; private set
    @Volatile var txDbm = 0; private set
    @Volatile var tag = 0; private set
    @Volatile var actualTxDbm: Int? = null; private set

    val bluetoothOn: Boolean get() = adapter?.isEnabled == true
    val blinking: Boolean get() = blinkPeriodMs > 0

    fun start(intervalMs: Int, txPowerLevel: Int, tag: Int, blinkPeriodMs: Long) {
        stop()
        val adv = adapter?.bluetoothLeAdvertiser
        if (adv == null) { onState("No BLE advertiser (Bluetooth off?)"); return }
        this.intervalMs = intervalMs; this.txDbm = txPowerLevel; this.tag = tag
        this.blinkPeriodMs = blinkPeriodMs

        val params = AdvertisingSetParameters.Builder()
            .setLegacyMode(true)
            .setConnectable(false)
            .setScannable(false)
            .setInterval(intervalMs * 1000 / 625)  // units of 0.625 ms; 160 = 100 ms minimum
            .setTxPowerLevel(txPowerLevel)
            .build()

        // Manufacturer data with company ID 0xFFFF (reserved for testing). Payload:
        // "USRP" magic + tag byte, so a scanner / your BleScanner app can find it by data.
        val payload = byteArrayOf(0x55, 0x53, 0x52, 0x50, tag.toByte())
        val data = AdvertiseData.Builder()
            .setIncludeDeviceName(false)
            .setIncludeTxPowerLevel(false)
            .addManufacturerData(0xFFFF, payload)
            .build()

        val cb = object : AdvertisingSetCallback() {
            override fun onAdvertisingSetStarted(s: AdvertisingSet?, txPower: Int, status: Int) {
                if (status != ADVERTISE_SUCCESS || s == null) {
                    running = false; radiating = false
                    onState("Advertise failed, status=$status"); return
                }
                set = s; running = true; radiating = true; actualTxDbm = txPower
                onState("Advertising: ${intervalMs} ms, controller TX ${txPower} dBm")
                if (blinkPeriodMs > 0) main.postDelayed(blinkTick, blinkPeriodMs)
            }
            override fun onAdvertisingEnabled(s: AdvertisingSet?, enable: Boolean, status: Int) {
                if (status == ADVERTISE_SUCCESS) radiating = enable
            }
            override fun onAdvertisingSetStopped(s: AdvertisingSet?) {
                running = false; radiating = false
            }
        }
        callback = cb
        adv.startAdvertisingSet(params, data, null, null, null, cb)
    }

    private val blinkTick = object : Runnable {
        override fun run() {
            val s = set ?: return
            if (!running) return
            s.enableAdvertising(!radiating, 0, 0)
            radiating = !radiating  // optimistic; callback confirms
            main.postDelayed(this, blinkPeriodMs)
        }
    }

    fun stop() {
        main.removeCallbacks(blinkTick)
        callback?.let { adapter?.bluetoothLeAdvertiser?.stopAdvertisingSet(it) }
        callback = null; set = null
        if (running) onState("Stopped")
        running = false; radiating = false
    }
}
