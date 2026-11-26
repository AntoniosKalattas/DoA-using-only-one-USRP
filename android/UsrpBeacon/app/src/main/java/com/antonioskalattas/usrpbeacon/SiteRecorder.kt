package com.antonioskalattas.usrpbeacon

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import org.json.JSONObject
import java.util.Locale
import java.util.UUID
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Records where the USRP is, using the phone:
 *  - position: the phone lies on top of the USRP; GPS fixes are averaged for [POS_SECONDS].
 *  - facing  : the phone is held flat with its top edge pointing where the antennas face (the
 *              direction the radar calls 0 deg); the true-north heading is averaged for [FACE_SECONDS].
 * Each result is checked (enough fixes, level phone, no magnetic disturbance), then handed to
 * [send] as a JSON message for the console ("usrp_site" / "usrp_facing").
 */
class SiteRecorder(
    private val truth: TruthSource,
    private val send: (String, (String?) -> Unit) -> Unit,
    private val onUpdate: () -> Unit,
) {
    companion object {
        const val POS_SECONDS = 30
        const val FACE_SECONDS = 4
        private const val MAX_FIX_ACC_M = 20.0
    }

    private val ui = Handler(Looper.getMainLooper())

    @Volatile var busy: String? = null; private set         // "position" / "facing" while recording
    @Volatile var progress = ""; private set
    @Volatile var positionText = "not recorded"; private set
    @Volatile var facingText = "not recorded"; private set
    @Volatile var lastError: String? = null; private set

    fun restore(pos: String?, face: String?) {
        pos?.let { positionText = it }; face?.let { facingText = it }
    }

    // ------------------------------------------------------------------ position

    fun recordPosition(done: (String) -> Unit) {
        if (busy != null) return
        busy = "position"; lastError = null
        val fixes = ArrayList<DoubleArray>()           // lat, lon, alt, acc, vacc
        var lastFixMs = 0L
        val t0 = SystemClock.elapsedRealtime()
        val tick = object : Runnable {
            override fun run() {
                val el = (SystemClock.elapsedRealtime() - t0) / 1000.0
                truth.location?.let { l ->
                    val age = truth.gpsAgeS() ?: 99.0
                    if (l.time != lastFixMs && age < 2.0 && l.accuracy <= MAX_FIX_ACC_M) {
                        lastFixMs = l.time
                        val vacc = if (android.os.Build.VERSION.SDK_INT >= 26 && l.hasVerticalAccuracy())
                            l.verticalAccuracyMeters.toDouble() else Double.NaN
                        fixes += doubleArrayOf(l.latitude, l.longitude, l.altitude, l.accuracy.toDouble(), vacc)
                    }
                }
                val acc = truth.location?.accuracy
                progress = String.format(Locale.US, "Keep it still on the USRP… %d s left · %d GPS fixes%s",
                    (POS_SECONDS - el).toInt().coerceAtLeast(0), fixes.size,
                    acc?.let { String.format(Locale.US, " · now ± %.1f m", it) } ?: " · no GPS yet")
                onUpdate()
                if (el < POS_SECONDS) { ui.postDelayed(this, 250); return }
                finishPosition(fixes, el, done)
            }
        }
        ui.post(tick)
    }

    private fun finishPosition(fixes: List<DoubleArray>, seconds: Double, done: (String) -> Unit) {
        busy = null; progress = ""
        if (fixes.size < 8) {
            fail("Only ${fixes.size} usable GPS fixes in $POS_SECONDS s (each must be within ${MAX_FIX_ACC_M.toInt()} m). " +
                "GPS needs open sky: do this outdoors and wait until the accuracy reads under 10 m.", done)
            return
        }
        // weighted mean (1/acc^2), then how far the fixes wandered around it
        var sw = 0.0; var la = 0.0; var lo = 0.0; var al = 0.0
        for (f in fixes) { val w = 1.0 / (f[3] * f[3]); sw += w; la += w * f[0]; lo += w * f[1]; al += w * f[2] }
        la /= sw; lo /= sw; al /= sw
        val mPerDegLat = 111_320.0
        val mPerDegLon = 111_320.0 * cos(Math.toRadians(la))
        val spread = sqrt(fixes.map {
            val dn = (it[0] - la) * mPerDegLat; val de = (it[1] - lo) * mPerDegLon; dn * dn + de * de
        }.average())
        val accs = fixes.map { it[3] }.sorted()
        val accMed = accs[accs.size / 2]
        // GPS error drifts slowly, so 30 s of averaging doesn't shrink it much: report it honestly
        val acc = maxOf(accMed / 2.0, spread)
        val vaccs = fixes.map { it[4] }.filter { !it.isNaN() }.sorted()
        val o = JSONObject().apply {
            put("type", "usrp_site"); put("id", UUID.randomUUID().toString()); put("t_ms", System.currentTimeMillis())
            put("lat", la); put("lon", lo); put("alt", al); put("acc_m", acc)
            if (vaccs.isNotEmpty()) put("vacc_m", vaccs[vaccs.size / 2])
            put("n_fixes", fixes.size); put("spread_m", spread); put("duration_s", seconds)
            truth.satsUsed?.let { put("sats_used", it) }
        }
        val text = String.format(Locale.US, "%.7f, %.7f  ± %.1f m  (%d fixes, wandered %.1f m)",
            la, lo, acc, fixes.size, spread)
        send(o.toString()) { err ->
            ui.post {
                if (err != null) fail("Measured ($text) but the laptop didn't answer ($err). It is saved on the phone and will be " +
                    "sent automatically once the laptop is reachable again.", done)
                else { positionText = text; onUpdate(); done("USRP position saved on the laptop.\n$text") }
            }
        }
    }

    // ------------------------------------------------------------------ facing

    fun recordFacing(done: (String) -> Unit) {
        if (busy != null) return
        busy = "facing"; lastError = null
        val heads = ArrayList<Double>(); val pitches = ArrayList<Double>(); val rolls = ArrayList<Double>()
        val fields = ArrayList<Double>()
        val t0 = SystemClock.elapsedRealtime()
        val tick = object : Runnable {
            override fun run() {
                val el = (SystemClock.elapsedRealtime() - t0) / 1000.0
                truth.headingTrueDeg?.let { heads += it }
                truth.pitchDeg?.let { pitches += it }
                truth.rollDeg?.let { rolls += it }
                truth.magFieldUt?.let { fields += it }
                progress = String.format(Locale.US, "Hold it flat and still, pointing where the antennas face… %d s",
                    (FACE_SECONDS - el).toInt().coerceAtLeast(0) + 1)
                onUpdate()
                if (el < FACE_SECONDS) { ui.postDelayed(this, 100); return }
                finishFacing(heads, pitches, rolls, fields, done)
            }
        }
        ui.post(tick)
    }

    private fun finishFacing(heads: List<Double>, pitches: List<Double>, rolls: List<Double>,
                             fields: List<Double>, done: (String) -> Unit) {
        busy = null; progress = ""
        if (heads.size < 10) { fail("The compass gave no readings. Does this phone have one?", done); return }
        val s = heads.sumOf { sin(Math.toRadians(it)) } / heads.size
        val c = heads.sumOf { cos(Math.toRadians(it)) } / heads.size
        val mean = (Math.toDegrees(atan2(s, c)) + 360.0) % 360.0
        val r = hypot(s, c).coerceIn(1e-9, 1.0)
        val spread = Math.toDegrees(sqrt(-2 * kotlin.math.ln(r)))
        val tilt = maxOf(pitches.map { abs(it) }.average(), rolls.map { abs(it) }.average())
        val field = if (fields.isEmpty()) Double.NaN else fields.sorted()[fields.size / 2]
        val status = truth.magStatus
        when {
            tilt > 20 -> { fail(String.format(Locale.US, "The phone was tilted %.0f°. Hold it flat (screen up), " +
                "like it is lying on a table.", tilt), done); return }
            spread > 6 -> { fail(String.format(Locale.US, "The compass moved %.0f° while measuring. Hold the phone " +
                "still and try again.", spread), done); return }
            !field.isNaN() && (field < 20 || field > 70) -> { fail(String.format(Locale.US, "The magnetic field " +
                "is %.0f µT (Earth's is 25–65): metal or electronics are bending the compass. Hold the phone at " +
                "least 30 cm from the USRP, the laptop and anything metal, and try again.", field), done); return }
            status == 0 -> { fail("The compass is uncalibrated. Wave the phone in a figure 8 a few times, then " +
                "try again.", done); return }
        }
        val o = JSONObject().apply {
            put("type", "usrp_facing"); put("id", UUID.randomUUID().toString()); put("t_ms", System.currentTimeMillis())
            put("facing_deg", mean); put("facing_spread_deg", spread); put("n_samples", heads.size)
            if (!field.isNaN()) put("mag_field_ut", field)
            status?.let { put("mag_status", it) }
            truth.declinationDeg?.let { put("declination_deg", it) }
        }
        val warn = if (status == 1) "  (compass accuracy LOW: a figure 8 wave first helps)" else ""
        val text = String.format(Locale.US, "%.1f° true north  ± %.1f°%s", mean, spread, warn)
        send(o.toString()) { err ->
            ui.post {
                if (err != null) fail("Measured ($text) but the laptop didn't answer ($err). It is saved on the phone and will be " +
                    "sent automatically once the laptop is reachable again.", done)
                else { facingText = text; onUpdate(); done("Antenna facing saved on the laptop.\n$text") }
            }
        }
    }

    private fun fail(msg: String, done: (String) -> Unit) {
        lastError = msg; onUpdate(); done(msg)
    }
}
