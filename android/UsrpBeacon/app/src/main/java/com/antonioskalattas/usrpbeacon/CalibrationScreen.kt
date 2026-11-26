package com.antonioskalattas.usrpbeacon

import android.app.Activity
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.widget.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * "Calibrate the USRP from this phone" wizard. Talks to the Capture Console's /cal endpoint
 * (through the same https:// ngrok link the telemetry uses). The console runs the B210
 * while this phone is the known transmitter. Three ways, the app picks the best one:
 *   AR walk : the phone tracks its own position (ARCore) while you walk around the antennas.
 *             The Mac then knows the true direction + distance of every packet and fits
 *             offset, antenna spacing, left/right and the distance model. Needs an AR phone.
 *   Sweep   : carry the phone once in an arc from the left end to the right end. Gives
 *             offset, antenna spacing and left/right. Works on any phone.
 *   Manual  : phone 1 m straight ahead, then ~45° to the right (the original two steps).
 * Then save: writes the preset and makes it active, optional "start live tracking".
 */
class CalibrationScreen(
    private val act: Activity,
    private val link: () -> String,
    private val tag: () -> Int,
    private val prepareBeacon: () -> Unit,     // continuous beacon, 100 ms, high power
    private val onBack: () -> Unit,
) {
    private val ui = Ui(act)
    private val main = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()
    private var visible = false
    private var prefilled = false
    private var last: JSONObject? = null

    // views
    private lateinit var linkStatus: TextView
    private lateinit var usbStatus: TextView
    private lateinit var busyStatus: TextView
    private lateinit var errorText: TextView
    private lateinit var nameField: EditText
    private lateinit var channel: Spinner
    private lateinit var spacing: EditText
    private lateinit var seconds: EditText
    private lateinit var leftAnt: Spinner
    private lateinit var frontBtn: Button
    private lateinit var frontBar: ProgressBar
    private lateinit var frontText: TextView
    private lateinit var sideBtn: Button
    private lateinit var skipBtn: Button
    private lateinit var sideBar: ProgressBar
    private lateinit var sideText: TextView
    private lateinit var summary: TextView
    private lateinit var saveBtn: Button
    private lateinit var liveBtn: Button
    private lateinit var cancelBtn: Button

    private val channels = listOf(39, 38, 37)

    // ---- automatic calibration
    private lateinit var methodSpin: Spinner
    private lateinit var methodNote: TextView
    private lateinit var sweepCard: View
    private lateinit var arCard: View
    private lateinit var manualCard1: View
    private lateinit var manualCard2: View
    private lateinit var sweepBtn: Button
    private lateinit var sweepText: TextView
    private lateinit var arHolder: FrameLayout
    private lateinit var arStatus: TextView
    private lateinit var arStartBtn: Button
    private lateinit var originBtn: Button
    private lateinit var walkBtn: Button
    private lateinit var arText: TextView
    private var arSupported: Boolean? = null            // null = ARCore is still answering
    private var ar: ArTracker? = null
    private var origin: FloatArray? = null
    private var posesSent = 0
    private var beginSent = false
    private var stage = "idle"
    private var autoBegun = false
    private var phonePkts = 0
    // console clock - phone clock (ms), from the request with the shortest round trip
    private var clockOffsetMs = 0.0
    private var bestRttMs = Double.MAX_VALUE
    private var bestAtMs = 0L
    @Volatile private var reqT0 = 0L
    @Volatile private var reqT1 = 0L

    /** "ar", "sweep" or "manual": the user's pick, or the app's when left on Automatic. */
    private fun method(): String = when (methodSpin.selectedItemPosition) {
        1 -> "ar"; 2 -> "sweep"; 3 -> "manual"
        else -> if (arSupported == true) "ar" else "sweep"
    }

    val view: View by lazy { build() }

    // ------------------------------------------------------------------ lifecycle

    fun show() { visible = true; bestRttMs = Double.MAX_VALUE; checkAr(); applyMethod(); poll(); tick() }
    fun hide() { visible = false; main.removeCallbacksAndMessages(null); stopAr() }
    fun onPause() { stopAr() }

    /** AR may restart with a different coordinate frame, so a stored origin is no longer valid. */
    private fun stopAr() { if (ar?.running == true) { ar?.pause(); origin = null } }

    /** Ask ARCore whether this phone can do AR (the answer can take a moment the first time). */
    private fun checkAr() {
        arSupported = ArTracker.supported(act)
        if (arSupported == null && visible) main.postDelayed({ checkAr() }, 300) else applyMethod()
    }

    private fun applyMethod() {
        if (!::methodNote.isInitialized) return
        val m = method()
        val auto = methodSpin.selectedItemPosition == 0
        methodNote.text = when {
            auto && arSupported == null -> "Checking whether this phone supports AR…"
            auto && m == "ar" -> "✓ This phone supports AR, so the app chose the AR walk (most accurate: it also calibrates distance)."
            auto -> "This phone has no AR support, so the app chose the sweep."
            m == "ar" && arSupported == false -> "⚠ This phone does not support AR. Pick the sweep instead."
            m == "ar" -> "AR walk: the phone tracks its own position with the camera while you walk around."
            m == "sweep" -> "Sweep: carry the phone once in an arc from the left end of the antennas to the right end."
            else -> "Manual: place the phone at two measured spots."
        }
        methodNote.setTextColor(if (m == "ar" && arSupported == false) ui.amber else ui.textSub)
        sweepCard.visibility = if (m == "sweep") View.VISIBLE else View.GONE
        arCard.visibility = if (m == "ar") View.VISIBLE else View.GONE
        manualCard1.visibility = if (m == "manual") View.VISIBLE else View.GONE
        manualCard2.visibility = if (m == "manual") View.VISIBLE else View.GONE
        if (m != "ar") ar?.pause()
    }

    /** Local 4 Hz refresh of the AR readout (does not wait for the network). */
    private fun tick() {
        if (!visible) return
        renderAr()
        main.postDelayed({ tick() }, 250)
    }

    private fun baseUrl(): String? = link().trim().trimEnd('/').takeIf { it.startsWith("http") }

    private fun poll() {
        if (!visible) return
        val base = baseUrl()
        if (base == null) {
            linkStatus.text = "✗ Calibration needs the https:// link. Put it in \"Laptop IP address or link\" on the main screen."
            linkStatus.setTextColor(ui.red)
            main.postDelayed({ poll() }, 1500)
            return
        }
        request("GET", "$base/cal", null) { code, body, err ->
            if (body != null && code == 200) {
                val s = JSONObject(body)
                // clock alignment: the console's "now" belongs to the middle of this request
                val rtt = (reqT1 - reqT0).toDouble()
                if (s.has("now") && rtt > 0 && (rtt <= bestRttMs || reqT1 - bestAtMs > 30_000)) {
                    bestRttMs = rtt; bestAtMs = reqT1
                    clockOffsetMs = s.getDouble("now") * 1000.0 - (reqT0 + reqT1) / 2.0
                }
                render(s)
                uploadPoses()
            } else offline(err ?: "HTTP $code")
            main.postDelayed({ poll() }, 700)
        }
    }

    /** HTTP on a background thread, callback on the main thread. */
    private fun request(method: String, url: String, json: JSONObject?, cb: (Int, String?, String?) -> Unit) {
        net.execute {
            var code = -1; var body: String? = null; var err: String? = null
            val t0 = System.currentTimeMillis()
            try {
                val c = URL(url).openConnection() as HttpURLConnection
                c.requestMethod = method; c.connectTimeout = 5000; c.readTimeout = 8000
                c.setRequestProperty("ngrok-skip-browser-warning", "1")
                if (json != null) {
                    val b = json.toString().toByteArray()
                    c.doOutput = true
                    c.setRequestProperty("Content-Type", "application/json")
                    c.setFixedLengthStreamingMode(b.size)
                    c.outputStream.use { it.write(b) }
                }
                code = c.responseCode
                body = (if (code in 200..299) c.inputStream else c.errorStream)?.bufferedReader()?.readText()
                c.disconnect()
            } catch (t: Throwable) { err = t.message ?: t.javaClass.simpleName }
            val t1 = System.currentTimeMillis()
            main.post { reqT0 = t0; reqT1 = t1; cb(code, body, err) }
        }
    }

    private fun command(action: String, extra: JSONObject.() -> Unit = {}) {
        val base = baseUrl() ?: return
        val j = JSONObject().put("action", action).apply(extra)
        request("POST", "$base/cal", j) { _, body, err ->
            val r = body?.let { runCatching { JSONObject(it) }.getOrNull() }
            if (r == null) { showError(err ?: "No answer from the console"); return@request }
            r.optJSONObject("state")?.let { render(it) }
            if (!r.optBoolean("ok", false) && r.has("msg")) showError(r.getString("msg"))
        }
    }

    private fun showError(msg: String) {
        errorText.text = "⚠ $msg"; errorText.visibility = View.VISIBLE
    }

    private fun offline(err: String) {
        linkStatus.text = "✗ Can't reach the console: $err\nIs app.py running on the Mac, and ngrok open?"
        linkStatus.setTextColor(ui.red)
        listOf(frontBtn, sideBtn, skipBtn, saveBtn, liveBtn, cancelBtn, sweepBtn, walkBtn).forEach { ui.enable(it, false) }
    }

    // ------------------------------------------------------------------ actions

    private fun measureFront() {
        prepareBeacon()
        val ch = channels[channel.selectedItemPosition]
        command("front") {
            put("name", nameField.text.toString().trim()); put("channel", ch)
            put("spacing_cm", spacing.text.toString().toDoubleOrNull() ?: 6.0)
            put("seconds", seconds.text.toString().toDoubleOrNull() ?: 10.0)
            put("rfa_side", if (leftAnt.selectedItemPosition == 0) "left" else "right")
            put("tag", tag()); put("stop_other", true)
        }
    }

    private fun measureSide() { prepareBeacon(); command("side") { put("stop_other", true) } }

    // ------------------------------------------------------------------ automatic: sweep + AR walk

    private fun autoStart(m: String) {
        prepareBeacon()
        beginSent = false; posesSent = 0
        ar?.drain()
        val ch = channels[channel.selectedItemPosition]
        command("auto_start") {
            put("method", m)
            put("name", nameField.text.toString().trim()); put("channel", ch)
            put("spacing_cm", spacing.text.toString().toDoubleOrNull() ?: 6.0)
            put("rfa_side", if (leftAnt.selectedItemPosition == 0) "left" else "right")
            put("tag", tag()); put("stop_other", true)
        }
    }

    private fun originJson() = origin?.let { org.json.JSONArray().put(it[0].toDouble()).put(it[1].toDouble()).put(it[2].toDouble()) }

    /** One button, three jobs: start the radio -> "go" when you start walking -> "done" at the end. */
    private fun sweepPressed() = when {
        stage == "sweep_running" && !autoBegun -> command("auto_begin")
        stage == "sweep_running" -> command("auto_stop")
        else -> autoStart("sweep")
    }

    private fun arStartPressed() {
        val t = ar ?: ArTracker(act).also {
            ar = it
            arHolder.addView(it.view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        }
        t.start()?.let { showError(it) }
        renderAr()
    }

    private fun setOrigin() {
        val p = ar?.last ?: return
        origin = floatArrayOf(p.x, p.y, p.z)
        Toast.makeText(act, "Origin set: this spot is now the antennas", Toast.LENGTH_SHORT).show()
        renderAr()
    }

    private fun walkPressed() {
        if (stage == "ar_running") { uploadPoses(); command("auto_stop") { originJson()?.let { put("origin", it) } } }
        else autoStart("ar")
    }

    /** Send the positions collected since last time, stamped on the console's clock. */
    private fun uploadPoses() {
        val t = ar ?: return
        val batch = t.drain()
        if (stage != "ar_running" || batch.isEmpty()) return
        if (!beginSent) { beginSent = true; command("auto_begin") }
        val arr = org.json.JSONArray()
        for (p in batch) arr.put(org.json.JSONArray()
            .put((p.tMs + clockOffsetMs) / 1000.0).put(p.x.toDouble()).put(p.y.toDouble()).put(p.z.toDouble()))
        posesSent += batch.size
        val base = baseUrl() ?: return
        val j = JSONObject().put("action", "auto_poses").put("poses", arr)
        originJson()?.let { j.put("origin", it) }
        request("POST", "$base/cal", j) { _, _, _ -> }
    }

    private fun renderAr() {
        if (!::arStatus.isInitialized || arCard.visibility != View.VISIBLE) return
        val t = ar
        val running = t?.running == true
        val busy = stage == "ar_running" || stage == "fitting"
        ui.enable(arStartBtn, !running)
        arStartBtn.text = if (running) "AR IS RUNNING" else "START AR (uses the camera)"
        ui.enable(originBtn, running && t?.tracking == true && !busy)
        originBtn.text = if (origin == null) "SET ORIGIN · phone is between the antennas" else "SET ORIGIN AGAIN"
        val p = t?.last
        val o = origin
        val dist = if (p != null && o != null) Math.sqrt(((p.x - o[0]) * (p.x - o[0]) + (p.y - o[1]) * (p.y - o[1]) +
            (p.z - o[2]) * (p.z - o[2])).toDouble()) else null
        arStatus.text = when {
            !running -> "AR is off. Tap START AR."
            t?.tracking != true -> "● " + (t?.problem ?: "Finding the phone's position…")
            o == null -> "● Tracking. Now hold the phone right between the two antennas and tap SET ORIGIN."
            stage == "ar_running" -> "● Recording · ${fmt(dist ?: 0.0, 2)} m from the antennas · $phonePkts packets · $posesSent positions" +
                (if ((dist ?: 1.0) < 0.4) "\nToo close: packets nearer than 0.4 m are not used." else "") +
                (if (bestRttMs > 400) "\nSlow link (${fmt(bestRttMs, 0)} ms): walk slowly so timing errors stay small." else "")
            else -> "● Tracking · ${fmt(dist ?: 0.0, 2)} m from the antennas. Ready to walk."
        }
        arStatus.setTextColor(if (running && t?.tracking == true) ui.green else if (running) ui.amber else ui.textSub)
    }

    private fun autoResult(front: JSONObject): String {
        val sb = StringBuilder("✓ ${front.optString("quality").uppercase()}: offset ${signed(front.optDouble("phase_deg"))}° " +
            "(spread ±${fmt(front.optDouble("phase_spread_deg"), 0)}°)\n")
        sb.append("antenna spacing fitted ${fmt(front.optDouble("spacing_m") * 100, 1)} cm" +
            (if (front.has("ruler_spacing_m")) " (you entered ${fmt(front.optDouble("ruler_spacing_m") * 100, 1)})" else "") + "\n")
        sb.append(if (front.optBoolean("flip")) "left/right were mirrored: fixed\n" else "left/right correct\n")
        sb.append("power at 1 m ${fmt(front.optDouble("ref_power_db"))} dB · ${front.optInt("n_bursts")} packets used")
        if (front.has("path_loss_exp")) sb.append("\ndistance model: path-loss exponent ${fmt(front.optDouble("path_loss_exp"), 2)}")
        if (front.has("bearing_err_median_deg")) sb.append("\ntypical direction error of one packet: ${fmt(front.optDouble("bearing_err_median_deg"), 0)}° " +
            "(walk covered ${fmt(front.optDouble("span_deg"), 0)}°)")
        return sb.toString()
    }

    // ------------------------------------------------------------------ render

    private fun fmt(v: Double, d: Int = 1) = String.format(Locale.US, "%.${d}f", v)
    private fun signed(v: Double, d: Int = 1) = (if (v >= 0) "+" else "") + fmt(v, d)

    private fun render(s: JSONObject) {
        last = s
        linkStatus.text = "✓ Connected to the Capture Console"
        linkStatus.setTextColor(ui.green)

        // USRP link
        val usb = s.optJSONObject("usb")
        when {
            usb == null || usb.isNull("connected") -> { usbStatus.text = "USRP: checking…"; usbStatus.setTextColor(ui.textSub) }
            !usb.optBoolean("connected") -> { usbStatus.text = "✗ USRP not connected to the Mac"; usbStatus.setTextColor(ui.red) }
            else -> {
                val fast = usb.optBoolean("fast_enough", false)
                usbStatus.text = "USRP ${usb.optString("product").removePrefix("USRP ")} on ${usb.optString("usb")} (${usb.optString("rate")})" +
                    if (!fast) "\nUSB 2 can drop samples: use a USB 3 (blue) port if you can." else ""
                usbStatus.setTextColor(if (fast) ui.green else ui.amber)
            }
        }
        val busy = s.optBoolean("busy") && s.optString("measuring").isEmpty()
        busyStatus.visibility = if (busy) View.VISIBLE else View.GONE
        busyStatus.text = "The B210 is running ${s.optString("busy_kind")}. Measuring will stop it."

        // first time: fill settings from the active calibration
        if (!prefilled) {
            s.optJSONObject("defaults")?.let { d ->
                spacing.setText(fmt(d.optDouble("spacing_m", 0.06) * 100, 1))
                val f = d.optDouble("freq", 2.480e9)
                channel.setSelection(when { f < 2.41e9 -> 2; f < 2.45e9 -> 1; else -> 0 })
                leftAnt.setSelection(if (d.optString("rfa_side", "left") == "right") 1 else 0)
            }
            prefilled = true
        }

        val err = s.optString("error", "")
        if (err.isNotEmpty() && err != "null") showError(err) else errorText.visibility = View.GONE

        val measuring = s.optString("measuring", "").let { if (it == "null") "" else it }
        stage = s.optString("stage", "idle")
        val auto = s.optJSONObject("auto")
        autoBegun = auto?.optBoolean("begun") == true
        phonePkts = s.optJSONObject("progress")?.optInt("phone_pkts") ?: 0
        val front = s.optJSONObject("front")
        val side = s.optJSONObject("side")
        val saved = s.optJSONObject("saved")
        val prog = s.optJSONObject("progress")

        // automatic methods
        val autoFront = front?.takeIf { it.has("method") }
        val idle = measuring.isEmpty() && stage != "fitting"
        when {
            stage == "sweep_running" && !autoBegun -> {
                sweepBtn.text = "GO · I'M STARTING TO WALK NOW"; ui.enable(sweepBtn, phonePkts > 0)
                sweepText.text = if (phonePkts > 0) "Radio ready ($phonePkts packets from your phone). Stand at the LEFT end, tap GO, then walk the arc."
                    else "Starting the B210 (about 5 s)… stay at the left end."
                sweepText.setTextColor(ui.textMain)
            }
            stage == "sweep_running" -> {
                sweepBtn.text = "DONE · I'M AT THE RIGHT END"; ui.enable(sweepBtn, true)
                sweepText.text = "Walking… ${fmt(auto?.optDouble("elapsed") ?: 0.0, 0)} s · $phonePkts packets. Aim for 10–15 s."
                sweepText.setTextColor(ui.textMain)
            }
            stage == "fitting" -> { ui.enable(sweepBtn, false); sweepText.text = "Working it out…" }
            else -> {
                sweepBtn.text = if (autoFront?.optString("method") == "sweep") "SWEEP AGAIN" else "START SWEEP"
                ui.enable(sweepBtn, idle)
                if (autoFront?.optString("method") == "sweep") { sweepText.text = autoResult(autoFront); sweepText.setTextColor(ui.green) }
                else { sweepText.text = "Not measured yet."; sweepText.setTextColor(ui.textSub) }
            }
        }
        when {
            stage == "ar_running" -> { walkBtn.text = "DONE WALKING"; ui.enable(walkBtn, origin != null) }
            stage == "fitting" -> { ui.enable(walkBtn, false); arText.text = "Working it out…"; arText.setTextColor(ui.textMain) }
            else -> {
                walkBtn.text = if (autoFront?.optString("method") == "ar") "WALK AGAIN" else "START WALK"
                ui.enable(walkBtn, idle && ar?.tracking == true && origin != null)
                if (autoFront?.optString("method") == "ar") { arText.text = autoResult(autoFront); arText.setTextColor(ui.green) }
                else { arText.text = "Not measured yet."; arText.setTextColor(ui.textSub) }
            }
        }
        if (stage == "ar_running") { arText.text = "Recording: walk a wide arc in front of the antennas."; arText.setTextColor(ui.textMain) }
        renderAr()

        // step 1
        ui.enable(frontBtn, measuring.isEmpty())
        frontBtn.text = if (front != null) "MEASURE FRONT AGAIN" else "MEASURE FRONT"
        if (measuring == "front") {
            showProgress(frontBar, frontText, prog)
        } else {
            frontBar.progress = if (front != null) 1000 else 0
            frontText.text = if (front == null) "Not measured yet." else {
                val q = front.optString("quality")
                frontText.setTextColor(when (q) { "good" -> ui.green; "ok" -> ui.amber; else -> ui.red })
                "✓ ${q.uppercase()}: offset ${signed(front.optDouble("phase_deg"))}° " +
                    "(spread ±${fmt(front.optDouble("phase_spread_deg"), 0)}°)\n" +
                    "power at 1 m ${fmt(front.optDouble("ref_power_db"))} dB · ${front.optInt("n_bursts")} packets from your phone" +
                    if (q == "poor") "\nPoor = the packets disagree a lot. Move away from walls/metal and measure again." else ""
            }
            if (front == null) frontText.setTextColor(ui.textSub)
        }

        // step 2
        ui.enable(sideBtn, measuring.isEmpty() && front != null)
        ui.enable(skipBtn, measuring.isEmpty() && front != null && side == null)
        if (measuring == "side") {
            showProgress(sideBar, sideText, prog)
        } else {
            sideBar.progress = if (side != null) 1000 else 0
            sideText.text = when {
                side == null -> if (front == null) "Do step 1 first." else "Not measured yet."
                side.has("angle") && !side.optBoolean("flip") -> "✓ Left/right correct: your phone read ${fmt(side.optDouble("angle"), 0)}° to the right."
                side.optBoolean("auto") -> "Left/right: ${side.optString("check")}"
                side.has("angle") -> "↔ Mirrored: your phone read ${fmt(-side.optDouble("angle"), 0)}° to the LEFT. The saved calibration will flip it, so that's fixed."
                else -> "Skipped: left and right are not verified."
            }
            sideText.setTextColor(if (side == null) ui.textSub else if (side.has("angle") || side.optBoolean("auto")) ui.green else ui.amber)
        }

        // step 3
        val cfg = s.optJSONObject("cfg")
        summary.text = if (front == null) "Nothing to save yet." else
            "name      ${cfg?.optString("name")}\n" +
            "channel   ${cfg?.optInt("channel")} (${fmt((cfg?.optDouble("freq") ?: 0.0) / 1e9, 3)} GHz) · gain ${fmt(cfg?.optDouble("gain") ?: 0.0, 0)} dB\n" +
            "spacing   ${fmt((cfg?.optDouble("spacing_m") ?: 0.0) * 100, 1)} cm\n" +
            "offset    ${signed(front.optDouble("phase_deg"))}° · ${front.optString("quality")}\n" +
            (if (front.has("method")) "method    ${if (front.optString("method") == "ar") "AR walk" else "sweep"}\n" else "") +
            (if (front.has("path_loss_exp")) "distance  path-loss exponent ${fmt(front.optDouble("path_loss_exp"), 2)}\n" else "") +
            "sides     ${side?.optString("check") ?: "not checked yet"}" +
            (saved?.let { "\n\n✓ Saved as \"${it.optString("name")}\" and set active." } ?: "")
        ui.enable(saveBtn, measuring.isEmpty() && front != null && side != null && saved == null)
        ui.enable(liveBtn, saved != null && measuring.isEmpty())
        cancelBtn.visibility = if (measuring.isNotEmpty()) View.VISIBLE else View.GONE
    }

    private fun showProgress(bar: ProgressBar, label: TextView, p: JSONObject?) {
        label.setTextColor(ui.textMain)
        if (p == null) { bar.progress = 0; label.text = "Starting the B210 (about 4 s)…"; return }
        val t = p.optDouble("t"); val dur = p.optDouble("dur", 10.0)
        bar.progress = (1000 * t / dur).toInt().coerceIn(0, 1000)
        val n = p.optInt("phone_pkts")
        val sb = StringBuilder("Measuring… ${fmt(t, 0)} / ${fmt(dur, 0)} s · $n packets from your phone")
        if (!p.isNull("phase")) sb.append("\noffset so far ${signed(p.optDouble("phase"))}° (spread ±${fmt(p.optDouble("spread"), 0)}°)")
        if (n == 0 && t > 3) sb.append("\nNo packets from your phone yet: is the beacon on and the phone close?")
        label.text = sb.toString()
    }

    // ------------------------------------------------------------------ layout

    private fun diagram(s: String) = ui.mono(s, 12f, ui.textSub).apply {
        background = ui.rounded(ui.field, 10); setPadding(ui.dp(12), ui.dp(10), ui.dp(12), ui.dp(10))
    }

    private fun build(): View {
        val col = LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(ui.dp(16), ui.dp(16), ui.dp(16), ui.dp(40))
        }
        val back = TextView(act).apply {
            text = "‹ Back"; textSize = 16f; setTextColor(ui.accent)
            setPadding(0, ui.dp(4), 0, ui.dp(8)); setOnClickListener { onBack() }
        }
        col.addView(back)
        col.addView(ui.text("Calibrate the USRP", 26f, ui.textMain, bold = true))
        col.addView(ui.text("This phone becomes the known transmitter. Put it where the steps say and " +
            "tap Measure. The Mac does the math and saves the calibration for the radar.", 13f, ui.textSub), ui.matchWrap(4))

        // connection
        val c0 = ui.card(col, "Connection", null)
        linkStatus = ui.text("Connecting…", 14f, ui.textSub, bold = true); c0.addView(linkStatus, ui.matchWrap(8))
        usbStatus = ui.text("USRP: checking…", 13f, ui.textSub); c0.addView(usbStatus, ui.matchWrap(6))
        busyStatus = ui.text("", 13f, ui.amber).apply { visibility = View.GONE }; c0.addView(busyStatus, ui.matchWrap(6))
        errorText = ui.text("", 13f, ui.red).apply { visibility = View.GONE }; c0.addView(errorText, ui.matchWrap(8))

        // settings
        val c1 = ui.card(col, "Settings", "Usually fine as they are. They're filled in from your current calibration.")
        nameField = ui.input("phone-" + SimpleDateFormat("MMdd-HHmm", Locale.US).format(Date()), InputType.TYPE_CLASS_TEXT)
        ui.setting(c1, "Calibration name", nameField, "What it's saved as on the Mac. Pick it later on the radar page.")
        channel = ui.spinner(listOf("Channel 39 · 2.480 GHz (recommended)", "Channel 38 · 2.426 GHz", "Channel 37 · 2.402 GHz"))
        ui.setting(c1, "Bluetooth channel to listen on", channel,
            "The USRP listens to one of the 3 Bluetooth advertising channels. Channel 39 sits above most Wi-Fi, " +
            "so it's the cleanest. Live tracking must then use the same channel.")
        spacing = ui.number("6.0")
        ui.setting(c1, "Antenna spacing (cm)", spacing,
            "Distance between the centres of the two antennas. Measure it with a ruler. 6 cm or less avoids " +
            "mirror images at 2.4 GHz.")
        seconds = ui.number("10")
        ui.setting(c1, "Measure time (seconds)", seconds, "Longer = steadier result. 10 s is plenty.")
        leftAnt = ui.spinner(listOf("RF A is on my left", "RF B is on my left"))
        ui.setting(c1, "Which antenna is on your left?", leftAnt,
            "Stand behind the USRP looking out toward the phone. Only used to label the radar's left/right sides.")

        // method
        val cm = ui.card(col, "How to calibrate", "Leave it on Automatic: the app uses AR if this phone can, otherwise the sweep.")
        methodSpin = ui.spinner(listOf("Automatic (the app chooses)", "AR walk · most accurate", "Sweep · one arc", "Manual · two positions"))
        methodSpin.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) { applyMethod() }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }
        cm.addView(methodSpin, ui.matchWrap(12))
        methodNote = ui.text("", 13f, ui.textSub); cm.addView(methodNote, ui.matchWrap(8))

        // AR walk
        val ca = ui.card(col, "AR walk", "The phone works out where it is from the camera. You only show it where the antennas are, then walk.")
        arCard = ca
        ca.addView(ui.text("1. Tap START AR and move the phone a little until it says Tracking.\n" +
            "2. Hold the phone right between the two antennas and tap SET ORIGIN.\n" +
            "3. Tap START WALK, step back and walk slowly for 20–30 s: a wide arc from one side of the antennas " +
            "to the other, 1–3 m away, also changing distance. Stay in front of the antennas and keep the " +
            "camera uncovered.\n4. Tap DONE WALKING.", 13f, ui.textSub), ui.matchWrap(10))
        arHolder = FrameLayout(act).apply { background = ui.rounded(ui.field, 10) }
        ca.addView(arHolder, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, ui.dp(220)).apply { topMargin = ui.dp(12) })
        arStatus = ui.text("AR is off. Tap START AR.", 13f, ui.textSub, bold = true); ca.addView(arStatus, ui.matchWrap(8))
        arStartBtn = ui.button("START AR (uses the camera)", ui.grey) { arStartPressed() }
        ca.addView(arStartBtn, ui.matchWrap(12))
        originBtn = ui.button("SET ORIGIN · phone is between the antennas", ui.grey) { setOrigin() }
        ca.addView(originBtn, ui.matchWrap(10))
        walkBtn = ui.button("START WALK", ui.accent) { walkPressed() }
        ca.addView(walkBtn, ui.matchWrap(10))
        arText = ui.text("Not measured yet.", 13f, ui.textSub); ca.addView(arText, ui.matchWrap(8))

        // sweep
        val cs = ui.card(col, "Sweep", "One slow walk around the front of the antennas, from the left end to the right end.")
        sweepCard = cs
        cs.addView(diagram(
            "            ·  ·  📱 ·  ·\n" +
            "        ·                  ·\n" +
            "      ·        ~1 m          ·\n" +
            "  START                      END\n" +
            "   📱 ──── [A]──────[B] ──── 📱\n" +
            "   left        🧍 you        right"), ui.matchWrap(10))
        cs.addView(ui.text("Stand about 1 m to the LEFT of the antennas, exactly in line with the two of them " +
            "(left as seen from behind the antennas). Tap START SWEEP and wait for the radio. Tap GO, walk a " +
            "half circle around the front at arm's length from your body, keeping about 1 m from the antennas, " +
            "and stop exactly in line with them on the RIGHT. Take 10–15 s, don't go back, then tap DONE.", 13f, ui.textSub), ui.matchWrap(10))
        sweepBtn = ui.button("START SWEEP", ui.accent) { sweepPressed() }
        cs.addView(sweepBtn, ui.matchWrap(14))
        sweepText = ui.text("Not measured yet.", 13f, ui.textSub); cs.addView(sweepText, ui.matchWrap(8))

        // step 1
        val c2 = ui.card(col, "Step 1 · Phone straight in front", null)
        manualCard1 = c2
        c2.addView(diagram(
            "            📱   phone, screen up\n" +
            "            │\n" +
            "            │  1 m\n" +
            "            │\n" +
            "     [A]────┴────[B]   antennas\n" +
            "            🧍  you stand behind"), ui.matchWrap(10))
        c2.addView(ui.text("Put the phone 1 m straight out from the middle of the two antennas, at about " +
            "the same height. Keep it still and don't stand between the phone and the antennas. " +
            "The beacon is switched to non-stop, fast and loud for this.", 13f, ui.textSub), ui.matchWrap(10))
        frontBtn = ui.button("MEASURE FRONT", ui.accent) { measureFront() }
        c2.addView(frontBtn, ui.matchWrap(14))
        frontBar = ui.progress(); c2.addView(frontBar, ui.matchWrap(10))
        frontText = ui.text("Not measured yet.", 13f, ui.textSub); c2.addView(frontText, ui.matchWrap(6))

        // step 2
        val c3 = ui.card(col, "Step 2 · Phone to the right", null)
        manualCard2 = c3
        c3.addView(diagram(
            "                    📱  phone, ~45° right\n" +
            "                  ╱\n" +
            "                ╱  ~1 m\n" +
            "              ╱\n" +
            "     [A]────●────[B]   antennas\n" +
            "            🧍  you stand behind"), ui.matchWrap(10))
        c3.addView(ui.text("Move the phone about 45° to YOUR right (seen from behind the antennas), still about " +
            "1 m away. This checks the radar doesn't show left and right swapped.", 13f, ui.textSub), ui.matchWrap(10))
        sideBtn = ui.button("MEASURE RIGHT SIDE", ui.accent) { measureSide() }
        skipBtn = ui.button("SKIP", ui.grey) { command("skip_side") }
        c3.addView(ui.row(sideBtn, skipBtn).apply {
            (getChildAt(0).layoutParams as LinearLayout.LayoutParams).weight = 2f
        }, ui.matchWrap(14))
        sideBar = ui.progress(); c3.addView(sideBar, ui.matchWrap(10))
        sideText = ui.text("Do step 1 first.", 13f, ui.textSub); c3.addView(sideText, ui.matchWrap(6))

        // step 3
        val c4 = ui.card(col, "Save", "Saves the calibration on the Mac and makes it the active one.")
        summary = ui.mono("Nothing to save yet.", 12f, ui.textMain).apply {
            background = ui.rounded(ui.field, 10); setPadding(ui.dp(12), ui.dp(10), ui.dp(12), ui.dp(10))
        }
        c4.addView(summary, ui.matchWrap(10))
        saveBtn = ui.button("SAVE & USE THIS CALIBRATION", ui.green) { command("save") }
        c4.addView(saveBtn, ui.matchWrap(14))
        liveBtn = ui.button("START LIVE TRACKING", ui.accent) {
            command("live")
            Toast.makeText(act, "Live tracking started: watch the radar on the Mac", Toast.LENGTH_LONG).show()
        }
        c4.addView(liveBtn, ui.matchWrap(10))
        c4.addView(ui.text("After saving you can put the phone anywhere and use the main screen's MARK " +
            "button to check how accurate the radar is.", 12f, ui.textNote), ui.matchWrap(8))

        cancelBtn = ui.button("CANCEL MEASUREMENT", ui.red) { command("cancel") }.apply { visibility = View.GONE }
        col.addView(cancelBtn, ui.matchWrap(14))

        listOf(frontBtn, sideBtn, skipBtn, saveBtn, liveBtn, sweepBtn, walkBtn, originBtn).forEach { ui.enable(it, false) }
        applyMethod()
        return ScrollView(act).apply { setBackgroundColor(ui.bg); isFillViewport = true; addView(col) }
    }

    fun release() { hide(); ar?.release(); net.shutdown() }
}
