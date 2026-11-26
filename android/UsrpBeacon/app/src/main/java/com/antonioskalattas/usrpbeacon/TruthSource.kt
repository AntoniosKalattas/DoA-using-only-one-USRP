package com.antonioskalattas.usrpbeacon

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.GeomagneticField
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.GnssStatus
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import org.json.JSONObject
import kotlin.math.sqrt

/**
 * Everything the phone knows about where it is and which way it points:
 *  - GPS fix: position, altitude, accuracies, speed, course, fix time, satellites
 *  - orientation: true-north heading (+ its accuracy), magnetic heading, pitch, roll
 *  - magnetometer: field strength (Earth = 25-65 uT; outside that = metal/electronics nearby)
 *  - barometer (if the phone has one)
 */
@SuppressLint("MissingPermission")
class TruthSource(ctx: Context) : SensorEventListener, LocationListener {
    private val lm = ctx.getSystemService(LocationManager::class.java)
    private val sm = ctx.getSystemService(SensorManager::class.java)
    private val main = Handler(Looper.getMainLooper())

    @Volatile var location: Location? = null; private set
    @Volatile var headingTrueDeg: Double? = null; private set
    @Volatile var headingMagDeg: Double? = null; private set
    @Volatile var headingAccDeg: Double? = null; private set     // from the rotation-vector sensor, if it says
    @Volatile var declinationDeg: Double? = null; private set
    @Volatile var pitchDeg: Double? = null; private set           // 0 = lying flat
    @Volatile var rollDeg: Double? = null; private set
    @Volatile var magFieldUt: Double? = null; private set
    @Volatile var magStatus: Int? = null; private set             // 0 unreliable .. 3 high
    @Volatile var pressureHpa: Double? = null; private set
    @Volatile var satsSeen: Int? = null; private set
    @Volatile var satsUsed: Int? = null; private set

    private val rot = FloatArray(9)
    private val ori = FloatArray(3)

    private val gnss = object : GnssStatus.Callback() {
        override fun onSatelliteStatusChanged(s: GnssStatus) {
            satsSeen = s.satelliteCount
            satsUsed = (0 until s.satelliteCount).count { s.usedInFix(it) }
        }
    }

    fun start() {
        sm.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)?.let { sm.registerListener(this, it, SensorManager.SENSOR_DELAY_UI) }
        sm.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)?.let { sm.registerListener(this, it, SensorManager.SENSOR_DELAY_UI) }
        sm.getDefaultSensor(Sensor.TYPE_PRESSURE)?.let { sm.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
        runCatching {
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 500L, 0f, this)
            location = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER)
        }
        runCatching { lm.registerGnssStatusCallback(gnss, main) }
    }

    fun stop() {
        sm.unregisterListener(this)
        runCatching { lm.removeUpdates(this) }
        runCatching { lm.unregisterGnssStatusCallback(gnss) }
    }

    /** Seconds since the last GPS fix, or null. */
    fun gpsAgeS(): Double? = location?.let {
        (SystemClock.elapsedRealtimeNanos() - it.elapsedRealtimeNanos) / 1e9
    }

    /** All of it, as telemetry fields (missing = the phone doesn't know / has no such sensor). */
    fun putInto(o: JSONObject) {
        location?.let {
            o.put("lat", it.latitude); o.put("lon", it.longitude); o.put("alt", it.altitude)
            o.put("acc_m", it.accuracy.toDouble())
            o.put("gps_age_s", gpsAgeS())
            o.put("fix_ms", it.time)                     // UTC time of the fix (GPS time)
            o.put("provider", it.provider)
            if (it.hasSpeed()) o.put("speed_mps", it.speed.toDouble())
            if (it.hasBearing()) o.put("course_deg", it.bearing.toDouble())   // direction of travel
            if (Build.VERSION.SDK_INT >= 26) {
                if (it.hasVerticalAccuracy()) o.put("vacc_m", it.verticalAccuracyMeters.toDouble())
                if (it.hasSpeedAccuracy()) o.put("speed_acc_mps", it.speedAccuracyMetersPerSecond.toDouble())
                if (it.hasBearingAccuracy()) o.put("course_acc_deg", it.bearingAccuracyDegrees.toDouble())
            }
            @Suppress("DEPRECATION")
            val mock = if (Build.VERSION.SDK_INT >= 31) it.isMock else it.isFromMockProvider
            if (mock) o.put("mock_location", true)
        }
        satsSeen?.let { o.put("sats_seen", it) }
        satsUsed?.let { o.put("sats_used", it) }
        headingTrueDeg?.let { o.put("heading_deg", it) }
        headingMagDeg?.let { o.put("heading_mag_deg", it) }
        headingAccDeg?.let { o.put("heading_acc_deg", it) }
        declinationDeg?.let { o.put("declination_deg", it) }
        pitchDeg?.let { o.put("pitch_deg", it) }
        rollDeg?.let { o.put("roll_deg", it) }
        magFieldUt?.let { o.put("mag_field_ut", it) }
        magStatus?.let { o.put("mag_status", it) }
        pressureHpa?.let { o.put("pressure_hpa", it) }
    }

    override fun onSensorChanged(e: SensorEvent) {
        when (e.sensor.type) {
            Sensor.TYPE_ROTATION_VECTOR -> {
                SensorManager.getRotationMatrixFromVector(rot, e.values)
                SensorManager.getOrientation(rot, ori)
                val mag = (Math.toDegrees(ori[0].toDouble()) + 360.0) % 360.0   // top edge, magnetic north
                headingMagDeg = mag
                pitchDeg = Math.toDegrees(ori[1].toDouble())
                rollDeg = Math.toDegrees(ori[2].toDouble())
                if (e.values.size > 4 && e.values[4] >= 0f) headingAccDeg = Math.toDegrees(e.values[4].toDouble())
                val decl = location?.let {
                    GeomagneticField(it.latitude.toFloat(), it.longitude.toFloat(),
                        it.altitude.toFloat(), System.currentTimeMillis()).declination.toDouble()
                }
                declinationDeg = decl
                headingTrueDeg = (mag + (decl ?: 0.0) + 360.0) % 360.0
            }
            Sensor.TYPE_MAGNETIC_FIELD -> {
                val x = e.values[0].toDouble(); val y = e.values[1].toDouble(); val z = e.values[2].toDouble()
                magFieldUt = sqrt(x * x + y * y + z * z)
                magStatus = e.accuracy
            }
            Sensor.TYPE_PRESSURE -> pressureHpa = e.values[0].toDouble()
        }
    }

    override fun onAccuracyChanged(s: Sensor?, a: Int) {
        if (s?.type == Sensor.TYPE_MAGNETIC_FIELD) magStatus = a
    }
    override fun onLocationChanged(l: Location) { location = l }
    @Deprecated("") override fun onStatusChanged(p: String?, s: Int, b: Bundle?) {}
    override fun onProviderEnabled(p: String) {}
    override fun onProviderDisabled(p: String) {}
}
