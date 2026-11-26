package com.antonioskalattas.usrpbeacon

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothAdapter
import android.bluetooth.le.AdvertisingSetParameters
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.*
import org.json.JSONObject
import java.util.Locale

class MainActivity : Activity() {

    private lateinit var beacon: Beacon
    private lateinit var truth: TruthSource
    private lateinit var udp: UdpTelemetry
    private lateinit var site: SiteRecorder
    private val ui = Handler(Looper.getMainLooper())

    // ---- palette
    private val bg = Color.rgb(15, 17, 21)
    private val card = Color.rgb(26, 29, 36)
    private val field = Color.rgb(36, 40, 49)
    private val textMain = Color.rgb(237, 239, 243)
    private val textSub = Color.rgb(154, 163, 178)
    private val textNote = Color.rgb(112, 120, 135)
    private val accent = Color.rgb(79, 140, 255)
    private val green = Color.rgb(34, 170, 90)
    private val amber = Color.rgb(214, 150, 30)
    private val red = Color.rgb(214, 70, 70)
    private val grey = Color.rgb(70, 76, 88)

    // ---- views
    private lateinit var statusDot: View
    private lateinit var statusTitle: TextView
    private lateinit var statusSub: TextView
    private lateinit var interval: Spinner
    private lateinit var txPower: Spinner
    private lateinit var tagField: EditText
    private lateinit var blink: Switch
    private lateinit var blinkPeriod: EditText
    private lateinit var indicator: Indicator
    private lateinit var mainView: View
    private lateinit var calScreen: CalibrationScreen
    private var onCal = false
    private lateinit var lightMode: Spinner
    private lateinit var startBtn: Button
    private lateinit var hostField: EditText
    private lateinit var portField: EditText
    private lateinit var udpSwitch: Switch
    private lateinit var bearingField: EditText
    private lateinit var rangeField: EditText
    private lateinit var liveGps: TextView
    private lateinit var liveAcc: TextView
    private lateinit var liveHeading: TextView
    private lateinit var livePackets: TextView
    private lateinit var liveMarks: TextView
    private lateinit var liveSats: TextView
    private lateinit var liveTilt: TextView
    private lateinit var liveMag: TextView
    private lateinit var sitePos: TextView
    private lateinit var siteFace: TextView
    private lateinit var siteStatus: TextView
    private lateinit var sitePosBtn: Button
    private lateinit var siteFaceBtn: Button

    private var markCount = 0
    private var markBearing: Double? = null
    private var markRange: Double? = null
    private var lastMsg = "Press START to begin transmitting."

    private val intervals = listOf(100, 250, 1000)
    private val txLevels = listOf(
        "HIGH  (≈ +1 dBm)" to AdvertisingSetParameters.TX_POWER_HIGH,
        "MEDIUM  (≈ −7 dBm)" to AdvertisingSetParameters.TX_POWER_MEDIUM,
        "LOW  (≈ −15 dBm)" to AdvertisingSetParameters.TX_POWER_LOW,
        "ULTRA LOW  (≈ −21 dBm)" to AdvertisingSetParameters.TX_POWER_ULTRA_LOW,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.statusBarColor = bg
        window.navigationBarColor = bg
        beacon = Beacon(this) { msg -> runOnUiThread { lastMsg = msg } }
        truth = TruthSource(this)
        udp = UdpTelemetry(::telemetryJson, TelemetryStore(filesDir))
        udp.active = { beacon.running }            // only stream while the beacon is on
        site = SiteRecorder(truth, send = { json, done ->
            val h = hostField.text.toString().trim()
            if (h.isEmpty()) done("No laptop address in section 2.") else udp.sendJson(h, port(), json, done)
        }, onUpdate = { runOnUiThread { refreshSite() } })
        site.restore(prefs.getString("site_pos", null), prefs.getString("site_face", null))
        indicator = Indicator(this, beacon)
        mainView = buildUi()
        calScreen = CalibrationScreen(this,
            link = { hostField.text.toString() },
            tag = { tagField.text.toString().toIntOrNull()?.coerceIn(0, 255) ?: 1 },
            prepareBeacon = ::beaconForCalibration,
            onBack = ::closeCalibration)
        setContentView(mainView)
        askPermissions()
    }

    // ---------------------------------------------------------------- telemetry

    private fun telemetryJson(): String {
        val o = JSONObject()
        o.put("type", "usrp_beacon")
        o.put("t_ms", System.currentTimeMillis())
        o.put("tag", beacon.tag)
        o.put("adv_running", beacon.running)
        o.put("adv_on", beacon.radiating)          // true while the phone is actually transmitting
        o.put("interval_ms", beacon.intervalMs)
        o.put("tx_dbm", beacon.actualTxDbm ?: JSONObject.NULL)
        truth.putInto(o)                           // GPS, compass, orientation, magnetometer, barometer
        o.put("mark", markCount)
        markBearing?.let { o.put("truth_bearing_deg", it) }
        markRange?.let { o.put("truth_range_m", it) }
        return o.toString()
    }

    // ---------------------------------------------------------------- actions

    private fun toggleBeacon() {
        if (beacon.running) { beacon.stop(); refresh(); return }
        if (!beacon.bluetoothOn) {
            runCatching { startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)) }
            return
        }
        val tag = tagField.text.toString().toIntOrNull()?.coerceIn(0, 255) ?: 1
        val blinkMs = if (blink.isChecked)
            ((blinkPeriod.text.toString().toDoubleOrNull() ?: 2.0) * 1000).toLong().coerceAtLeast(200)
        else 0L
        beacon.start(intervals[interval.selectedItemPosition],
            txLevels[txPower.selectedItemPosition].second, tag, blinkMs)
    }

    private var udpDest = ""

    // Defaults for the current setup: ngrok tunnel to the Capture Console's phone ingest.
    // The free ngrok link changes whenever ngrok restarts, so whatever you type is remembered.
    private val defaultHost = "https://c565-213-7-3-118.ngrok-free.app"
    /** Old ngrok links that no longer exist: replaced by [defaultHost] when the app starts. */
    private fun isDeadLink(h: String) = listOf("2711-62-228-144-38", "89d3-46-199-13-84", "exploitable-fashionably-jarred")
        .any { h.contains(it) }
    private val defaultPort = "5005"
    private val prefs by lazy { getSharedPreferences("usrp_beacon", MODE_PRIVATE) }

    /** Calibration wants lots of packets: non-stop (no blink), every 100 ms, high power. */
    private fun beaconForCalibration() {
        if (!beacon.bluetoothOn) {
            runCatching { startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)) }
            return
        }
        blink.isChecked = false
        interval.setSelection(0)
        txPower.setSelection(0)
        if (beacon.running && !beacon.blinking && beacon.intervalMs == intervals[0] &&
            beacon.txDbm == txLevels[0].second) return
        val tag = tagField.text.toString().toIntOrNull()?.coerceIn(0, 255) ?: 1
        beacon.start(intervals[0], txLevels[0].second, tag, 0L)    // start() restarts if already running
    }

    private fun openCalibration() {
        onCal = true
        setContentView(calScreen.view)
        calScreen.show()
    }

    private fun closeCalibration() {
        onCal = false
        calScreen.hide()
        setContentView(mainView)
    }

    @Deprecated("system back")
    override fun onBackPressed() {
        if (onCal) closeCalibration() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    private fun toggleUdp(on: Boolean) {
        if (on) {
            val h = hostField.text.toString().trim()
            udpDest = if (h.startsWith("http")) h.trimEnd('/') + "/phone" else "$h:${port()}"
            udp.start(hostField.text.toString().trim(), port(), 5.0)
        } else udp.stop()
    }

    /** Re-target the stream when the IP/port is edited while streaming (it used to keep the old address). */
    private val restartUdp = object : android.text.TextWatcher {
        override fun afterTextChanged(e: android.text.Editable?) {
            if (::portField.isInitialized) prefs.edit()
                .putString("host", hostField.text.toString().trim())
                .putString("port", portField.text.toString().trim()).apply()
            if (::udpSwitch.isInitialized && udpSwitch.isChecked) toggleUdp(true)
        }
        override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
        override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
    }

    private fun port() = portField.text.toString().toIntOrNull() ?: 5005

    private fun mark() {
        markCount++
        markBearing = bearingField.text.toString().toDoubleOrNull()
        markRange = rangeField.text.toString().toDoubleOrNull()
        if (udpSwitch.isChecked) udp.sendNow(hostField.text.toString().trim(), port())
        val note = if (udpSwitch.isChecked) "sent to laptop" else "NOT sent — turn on 'Send info to laptop'"
        Toast.makeText(this, "Mark #$markCount $note", Toast.LENGTH_SHORT).show()
    }

    // ---------------------------------------------------------------- USRP position

    private fun recordSitePosition() {
        siteStatus.text = ""
        site.recordPosition { msg -> showSiteResult(msg, site.lastError == null) }
        refreshSite()
    }

    private fun recordSiteFacing() {
        siteStatus.text = ""
        site.recordFacing { msg -> showSiteResult(msg, site.lastError == null) }
        refreshSite()
    }

    private fun showSiteResult(msg: String, ok: Boolean) {
        siteStatus.text = msg
        siteStatus.setTextColor(if (ok) green else amber)
        if (ok) prefs.edit().putString("site_pos", site.positionText).putString("site_face", site.facingText).apply()
        refreshSite()
    }

    private fun refreshSite() {
        if (!::sitePos.isInitialized) return
        sitePos.text = site.positionText
        siteFace.text = site.facingText
        val busy = site.busy
        if (busy != null) { siteStatus.text = site.progress; siteStatus.setTextColor(textSub) }
        sitePosBtn.isEnabled = busy == null; siteFaceBtn.isEnabled = busy == null
        sitePosBtn.alpha = if (busy == null) 1f else 0.5f; siteFaceBtn.alpha = if (busy == null) 1f else 0.5f
    }

    private val ticker = object : Runnable {
        override fun run() { refresh(); ui.postDelayed(this, 250) }
    }

    private fun refresh() {
        val (title, color) = when {
            !beacon.running -> "IDLE" to grey
            beacon.radiating -> "TRANSMITTING" to green
            else -> "PAUSED (blink)" to amber
        }
        statusTitle.text = title
        (statusDot.background as GradientDrawable).setColor(color)
        statusSub.text = if (!beacon.running) lastMsg else when {
            beacon.radiating -> "Sending Bluetooth packets now\nevery ${beacon.intervalMs} ms · ${beacon.actualTxDbm ?: "?"} dBm"
            else -> "Quiet part of the blink\nno signal right now"
        }

        startBtn.text = if (beacon.running) "STOP BEACON" else "START BEACON"
        (startBtn.background as GradientDrawable).setColor(if (beacon.running) red else green)

        val loc = truth.location
        liveGps.text = loc?.let { String.format(Locale.US, "%.6f, %.6f", it.latitude, it.longitude) }
            ?: "waiting… (needs sky view)"
        liveAcc.text = loc?.let {
            String.format(Locale.US, "± %.1f m  ·  %.0f s old", it.accuracy, truth.gpsAgeS() ?: 0.0)
        } ?: "—"
        liveHeading.text = truth.headingTrueDeg?.let { h ->
            String.format(Locale.US, "%.0f°  %s", h, compass(h)) +
                (truth.headingAccDeg?.let { String.format(Locale.US, "  ± %.0f°", it) } ?: "")
        } ?: "—"
        liveSats.text = truth.satsUsed?.let { "$it used of ${truth.satsSeen ?: "?"} seen" } ?: "—"
        liveTilt.text = if (truth.pitchDeg == null) "—" else
            String.format(Locale.US, "pitch %.0f°  roll %.0f°", truth.pitchDeg, truth.rollDeg ?: 0.0)
        liveMag.text = truth.magFieldUt?.let { f ->
            val ok = f in 25.0..65.0
            String.format(Locale.US, "%.0f µT  %s", f, if (ok) "normal" else "DISTURBED") +
                (when (truth.magStatus) { 0 -> " · uncalibrated"; 1 -> " · low accuracy"; else -> "" })
        } ?: "—"
        livePackets.text = if (!udpSwitch.isChecked) "off" else
            "${udp.sent} → $udpDest" + (if (!beacon.running) "\n⏸ paused: beacon is off (MARK / site still send)" else "") +
                (udp.lastError?.let { "\n⚠ $it" } ?: "") + backlogText()
        liveMarks.text = if (markCount == 0) "none yet" else
            "$markCount  (last: ${markBearing ?: "?"}° at ${markRange ?: "?"} m)"
        refreshSite()
    }

    /** Drop stored updates that haven't reached the laptop: only older days, or everything. */
    private fun confirmClearStored() {
        val n = udp.pending
        if (n == 0L) { Toast.makeText(this, "Nothing stored: the laptop has everything.", Toast.LENGTH_SHORT).show(); return }
        val midnight = java.util.Calendar.getInstance().apply {
            set(java.util.Calendar.HOUR_OF_DAY, 0); set(java.util.Calendar.MINUTE, 0)
            set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
        }.timeInMillis
        val run: (Long?) -> Unit = { before ->
            udp.clearStored(before) { dropped ->
                ui.post {
                    Toast.makeText(this, "Cleared $dropped stored updates. ${udp.pending} still waiting.", Toast.LENGTH_LONG).show()
                    refresh()
                }
            }
        }
        AlertDialog.Builder(this)
            .setTitle("Clear stored packets?")
            .setMessage("$n updates are saved on the phone and NOT on the laptop yet.\n\n" +
                "• Older than today: drops only updates from before today; today's keep uploading.\n" +
                "• Delete all: drops everything, including today's. They are gone for good.")
            .setPositiveButton("Older than today") { _, _ -> run(midnight) }
            .setNegativeButton("Delete all") { _, _ ->
                AlertDialog.Builder(this).setTitle("Delete all $n?")
                    .setMessage("This can't be undone. Today's GPS / MARK data that hasn't reached the laptop is lost too.")
                    .setPositiveButton("Delete all") { _, _ -> run(null) }
                    .setNegativeButton("Cancel", null).show()
            }
            .setNeutralButton("Cancel", null)
            .show()
    }

    /** Saved on the phone but not on the laptop yet (no internet on either side): uploaded later. */
    private fun backlogText(): String {
        val n = udp.pending
        return when {
            n > 0 -> "\n📦 $n saved on the phone, waiting for the laptop" +
                (udp.backlogError?.let { " (retrying: $it)" } ?: "")
            udp.lastUpload != null -> "\n✓ all caught up (${udp.lastUpload})"
            else -> ""
        }
    }

    private fun compass(deg: Double) =
        listOf("N", "NE", "E", "SE", "S", "SW", "W", "NW")[(((deg + 22.5) % 360) / 45).toInt()]

    // ---------------------------------------------------------------- lifecycle

    override fun onResume() { super.onResume(); truth.start(); ui.post(ticker) }
    override fun onPause() { super.onPause(); ui.removeCallbacks(ticker); calScreen.onPause() }   // beacon keeps running; AR camera stops
    override fun onDestroy() { calScreen.release(); indicator.release(); beacon.stop(); truth.stop(); udp.close(); super.onDestroy() }

    private fun askPermissions() {
        val perms = mutableListOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
        if (Build.VERSION.SDK_INT >= 31) {
            perms += Manifest.permission.BLUETOOTH_ADVERTISE
            perms += Manifest.permission.BLUETOOTH_CONNECT
        }
        if (Build.VERSION.SDK_INT >= 33) perms += Manifest.permission.POST_NOTIFICATIONS
        val missing = perms.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(rc: Int, p: Array<out String>, r: IntArray) {
        super.onRequestPermissionsResult(rc, p, r)
        truth.stop(); truth.start()
    }

    // ---------------------------------------------------------------- UI building blocks

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun rounded(color: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radiusDp).toFloat()
    }

    private fun matchWrap(topDp: Int = 0) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
    ).apply { topMargin = dp(topDp) }

    private fun text(t: String, sizeSp: Float, color: Int, bold: Boolean = false) = TextView(this).apply {
        text = t; textSize = sizeSp; setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setLineSpacing(0f, 1.15f)
    }

    /** A rounded card with a title and a one-line explanation of what the section is for. */
    private fun card(parent: LinearLayout, title: String, subtitle: String): LinearLayout {
        val c = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(card, 16)
            setPadding(dp(18), dp(16), dp(18), dp(18))
        }
        c.addView(text(title, 17f, textMain, bold = true))
        c.addView(text(subtitle, 13f, textSub), matchWrap(4))
        parent.addView(c, matchWrap(14))
        return c
    }

    /** Label, input control, and a plain-English note underneath. */
    private fun setting(parent: LinearLayout, label: String, control: View, note: String) {
        parent.addView(text(label, 14f, textMain, bold = true), matchWrap(18))
        parent.addView(control, matchWrap(6))
        parent.addView(text(note, 12f, textNote), matchWrap(6))
    }

    private fun input(def: String, type: Int) = EditText(this).apply {
        setText(def); inputType = type; textSize = 16f
        setTextColor(textMain); setHintTextColor(textNote)
        background = rounded(field, 10)
        setPadding(dp(14), dp(12), dp(14), dp(12))
        setSingleLine()
    }

    private fun number(def: String, decimal: Boolean = true, signed: Boolean = false) = input(def,
        InputType.TYPE_CLASS_NUMBER or
            (if (decimal) InputType.TYPE_NUMBER_FLAG_DECIMAL else 0) or
            (if (signed) InputType.TYPE_NUMBER_FLAG_SIGNED else 0))

    private fun spinner(items: List<String>) = Spinner(this).apply {
        adapter = object : ArrayAdapter<String>(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, items) {
            override fun getView(pos: Int, cv: View?, p: ViewGroup): View =
                (super.getView(pos, cv, p) as TextView).apply { setTextColor(textMain); textSize = 16f }
        }
        background = rounded(field, 10)
        setPadding(dp(4), dp(4), dp(4), dp(4))
    }

    private fun toggle(label: String) = Switch(this).apply {
        text = label; textSize = 15f; setTextColor(textMain)
        thumbTintList = ColorStateList(
            arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
            intArrayOf(accent, Color.rgb(180, 185, 195)))
        trackTintList = ColorStateList(
            arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
            intArrayOf(Color.argb(140, 79, 140, 255), grey))
    }

    private fun bigButton(label: String, color: Int, onClick: () -> Unit) = Button(this).apply {
        text = label; textSize = 16f; setTextColor(Color.WHITE)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        background = rounded(color, 12)
        stateListAnimator = null
        setPadding(0, dp(14), 0, dp(14))
        setOnClickListener { onClick() }
    }

    /** One live readout: name + value on a line, explanation below. */
    private fun readout(parent: LinearLayout, name: String, note: String): TextView {
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        row.addView(text(name, 14f, textSub), LinearLayout.LayoutParams(dp(110), ViewGroup.LayoutParams.WRAP_CONTENT))
        val v = text("—", 15f, textMain, bold = true).apply { typeface = Typeface.MONOSPACE }
        row.addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        parent.addView(row, matchWrap(14))
        parent.addView(text(note, 12f, textNote), matchWrap(2))
        return v
    }

    // ---------------------------------------------------------------- screen

    private fun buildUi(): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(20), dp(16), dp(40))
        }

        // Header
        col.addView(text("USRP Beacon", 26f, textMain, bold = true))
        col.addView(text("Turns this phone into a known Bluetooth transmitter so you can test " +
            "and calibrate the USRP B210 detector.", 13f, textSub), matchWrap(4))

        // Status
        val status = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = rounded(card, 16)
            setPadding(dp(18), dp(18), dp(18), dp(18))
        }
        statusDot = View(this).apply { background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(grey) } }
        status.addView(statusDot, LinearLayout.LayoutParams(dp(18), dp(18)))
        val st = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(14), 0, 0, 0) }
        statusTitle = text("IDLE", 22f, textMain, bold = true)
        statusSub = text(lastMsg, 13f, textSub).apply {
            setLines(2)                                   // fixed height: no jumping when the state flips
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        statusTitle.setSingleLine()
        st.addView(statusTitle); st.addView(statusSub)
        status.addView(st, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        col.addView(status, matchWrap(18))
        col.addView(bigButton("CALIBRATE THE USRP WITH THIS PHONE  ›", Color.rgb(48, 56, 72)) { openCalibration() },
            matchWrap(12))

        // 1. Beacon
        val b = card(col, "1 · Bluetooth beacon",
            "What the phone transmits. The USRP should pick this up at 2.426 GHz (Bluetooth channel 38).")
        interval = spinner(intervals.map { "Every $it ms" })
        setting(b, "How often to transmit", interval,
            "100 ms = 10 signals per second, which is easiest for the USRP to catch. " +
            "Slower settings save battery but give fewer detections.")
        txPower = spinner(txLevels.map { it.first })
        setting(b, "Transmit power", txPower,
            "How strong the signal is. Use HIGH for long distances. Use LOW to find the " +
            "weakest signal the USRP can still detect.")
        tagField = number("1", decimal = false)
        setting(b, "Tag number (0–255)", tagField,
            "An ID hidden inside every signal so you can tell phones apart if you use more than one. " +
            "Any number is fine.")
        blink = toggle("Blink on / off").apply { isChecked = true }
        setting(b, "Blink mode", blink,
            "Switches the signal on and off in a steady rhythm. Lots of devices use Bluetooth, but " +
            "only your phone will turn on and off on this beat, so it's easy to spot on the USRP.")
        blinkPeriod = number("2")
        setting(b, "Blink time (seconds)", blinkPeriod,
            "How long each on and off part lasts. 2 means on for 2 s, then off for 2 s, and repeat.")
        val modes = Indicator.Mode.values()
        lightMode = spinner(listOf("Off", "Notification LED (screen off)",
            if (indicator.hasTorch) "Flashlight (pulses with packets)" else "Flashlight (not available)"))
        lightMode.setSelection(prefs.getInt("light", Indicator.Mode.LED.ordinal).coerceIn(0, modes.size - 1))
        lightMode.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                indicator.mode = modes[pos]
                prefs.edit().putInt("light", pos).apply()
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        setting(b, "Light while transmitting", lightMode,
            "Notification LED: the small light on top flashes in short pulses while the phone is sending " +
            "and stays dark during the blink's off part. Android only lights it with the SCREEN OFF, so press " +
            "the power button after starting. Android limits how fast an app can do this, so the LED pulses " +
            "at most about 2–3 times a second (once per packet at 500 ms or 1000 ms intervals). " +
            "Flashlight: flashes once per packet interval and works with the screen on. Both follow the " +
            "app's timing, because Android doesn't tell apps the exact moment each packet goes out.")
        startBtn = bigButton("START BEACON", green) { toggleBeacon() }
        b.addView(startBtn, matchWrap(20))

        // 2. Laptop link
        val l = card(col, "2 · Send info to laptop",
            "Sends the phone's location, compass direction and on/off state over Wi-Fi so the laptop " +
            "can match it against what the USRP recorded. Run phone_truth_listener.py on the laptop.")
        hostField = input(prefs.getString("host", null)?.takeIf { it.isNotBlank() && !isDeadLink(it) } ?: defaultHost, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        setting(l, "Laptop IP address or link", hostField,
            "Same Wi-Fi: type the Mac's IP (run 'ipconfig getifaddr en0' on the Mac). " +
            "If the Wi-Fi blocks that, paste the https:// ngrok link instead and the info goes " +
            "through the internet. Works on mobile data too.")
        hostField.addTextChangedListener(restartUdp)
        portField = number(prefs.getString("port", null)?.takeIf { it.isNotBlank() } ?: defaultPort, decimal = false)
        portField.addTextChangedListener(restartUdp)
        setting(l, "Port", portField,
            "A channel number that has to match the laptop listener. Leave it at 5005. It's ignored when you use a link.")
        udpSwitch = toggle("Send info to laptop").apply { setOnCheckedChangeListener { _, on -> toggleUdp(on) } }
        setting(l, "Streaming", udpSwitch,
            "Turn this on while testing. Sends 5 small updates per second, so it barely uses any data.")

        // 3. Calibration
        val c = card(col, "3 · Calibration point",
            "Put the phone at a measured spot, enter where it is, then tap MARK. The laptop then knows " +
            "the exact right answer and can check or correct the USRP's direction estimate.")
        bearingField = number("0", signed = true)
        setting(c, "Angle from antennas (°)", bearingField,
            "Direction of the phone as seen from the USRP antennas. 0 = straight in front. Use the " +
            "same left/right sign (+ or −) as your MUSIC output.")
        rangeField = number("3")
        setting(c, "Distance (m)", rangeField,
            "Distance from the antennas to the phone in meters. Measure it with a tape, because GPS " +
            "is too rough at short range.")
        c.addView(bigButton("MARK · phone is here now", accent) { mark() }, matchWrap(20))
        c.addView(text("Tap MARK again each time you move the phone to a new spot.", 12f, textNote), matchWrap(8))

        // 4. USRP position
        val sp = card(col, "4 · USRP position",
            "Tell the laptop exactly where the USRP is and which way its antennas face. Then every direction " +
            "the USRP measures can be checked against where GPS says this phone was (doa_compare.py on the laptop). " +
            "Do it outdoors: GPS is poor indoors. 'Send info to laptop' must have an address.")
        sp.addView(text("Step 1 · Lay the phone flat on top of the USRP, then tap. Don't touch it for " +
            "${SiteRecorder.POS_SECONDS} s while GPS fixes are averaged.", 13f, textMain), matchWrap(14))
        sitePosBtn = bigButton("RECORD POSITION · phone is on the USRP", accent) { recordSitePosition() }
        sp.addView(sitePosBtn, matchWrap(10))
        sitePos = readout(sp, "Saved", "Latitude, longitude and how far off it may be.")
        sp.addView(text("Step 2 · Hold the phone flat, about 30 cm in front of the USRP (away from the laptop and " +
            "metal), with its TOP edge pointing straight out the front of the antennas, the radar's 0°. Tap and " +
            "hold still for ${SiteRecorder.FACE_SECONDS} s.", 13f, textMain), matchWrap(18))
        siteFaceBtn = bigButton("RECORD FACING · top edge = antenna front", accent) { recordSiteFacing() }
        sp.addView(siteFaceBtn, matchWrap(10))
        siteFace = readout(sp, "Saved", "Compass direction the antennas face (0° = North, 90° = East). If it is a " +
            "little off, doa_compare.py measures the real value from the walk.")
        siteStatus = text("", 13f, textSub)
        sp.addView(siteStatus, matchWrap(14))
        sp.addView(text("Then start live tracking on the laptop (it records automatically), walk around 15–60 m away " +
            "with the beacon on, and run: python3 capture_console/doa_compare.py", 12f, textNote), matchWrap(10))

        // 5. Live
        val v = card(col, "5 · Live readings", "What the phone knows right now. Updates 4 times a second.")
        liveGps = readout(v, "GPS position", "Where satellites say the phone is. Usually doesn't work well indoors.")
        liveAcc = readout(v, "GPS accuracy", "How far off the GPS position could be. Under 5 m is good.")
        liveHeading = readout(v, "Facing", "Direction the top of the phone points. 0° = North, 90° = East.")
        liveSats = readout(v, "Satellites", "GPS satellites used for the position. 6 or more is good.")
        liveTilt = readout(v, "Tilt", "0° / 0° = lying flat. The compass is only trustworthy when flat.")
        liveMag = readout(v, "Magnetic field", "Earth's field is 25–65 µT. Outside that, metal or electronics nearby are " +
            "bending the compass.")
        livePackets = readout(v, "Sent to laptop", "Updates sent so far. If this keeps going up, the link is working.")
        v.addView(bigButton("CLEAR STORED PACKETS", Color.rgb(120, 48, 48)) { confirmClearStored() }, matchWrap(8))
        liveMarks = readout(v, "Marks", "How many calibration points you've sent.")

        return ScrollView(this).apply {
            setBackgroundColor(bg)
            isFillViewport = true
            addView(col)
        }
    }
}
