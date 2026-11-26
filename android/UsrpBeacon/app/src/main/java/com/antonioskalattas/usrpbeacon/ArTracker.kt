package com.antonioskalattas.usrpbeacon

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.SystemClock
import android.view.Surface
import android.view.WindowManager
import com.google.ar.core.ArCoreApk
import com.google.ar.core.Config
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/**
 * Where is this phone? ARCore (camera + motion sensors) tracks the phone's position in the room
 * to a few centimetres. The "AR walk" calibration sends these positions to the Mac, which then
 * knows the true direction and distance of every Bluetooth packet the USRP received.
 *
 * Coordinates: metres, y = up, origin = wherever the phone was when tracking started.
 * [view] shows the camera picture; ARCore needs it drawn to keep tracking.
 */
class ArTracker(private val act: Activity) : GLSurfaceView.Renderer {

    /** One tracked position: phone clock (ms since epoch) + x, y, z in metres. */
    class Sample(val tMs: Long, val x: Float, val y: Float, val z: Float)

    companion object {
        /** null = still checking (ask again shortly), true/false = final answer. */
        fun supported(ctx: Context): Boolean? {
            val a = runCatching { ArCoreApk.getInstance().checkAvailability(ctx) }.getOrNull() ?: return false
            return if (a.isTransient) null else a.isSupported
        }
    }

    val view = GLSurfaceView(act).apply {
        preserveEGLContextOnPause = true
        setEGLContextClientVersion(2)
        setRenderer(this@ArTracker)
        renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
    }

    @Volatile var running = false; private set
    @Volatile var tracking = false; private set
    @Volatile var problem: String? = null; private set     // why it is not tracking, in plain words
    @Volatile var last: Sample? = null; private set

    private var session: Session? = null
    private var installAsked = false
    private val pending = ArrayList<Sample>()
    private var lastKeptMs = 0L

    // ------------------------------------------------------------------ control

    val hasCamera: Boolean get() = act.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    /** Returns null when tracking started, else what the user has to do first. */
    fun start(): String? {
        if (running) return null
        if (!hasCamera) {
            act.requestPermissions(arrayOf(Manifest.permission.CAMERA), 2)
            return "Allow the camera, then tap START AR again. AR finds the phone's position from the camera picture."
        }
        try {
            if (session == null) {
                val st = ArCoreApk.getInstance().requestInstall(act, !installAsked)
                installAsked = true
                if (st == ArCoreApk.InstallStatus.INSTALL_REQUESTED)
                    return "Install \"Google Play Services for AR\" from the page that just opened, then tap START AR again."
                val s = Session(act)
                s.configure(Config(s).apply {
                    updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
                    planeFindingMode = Config.PlaneFindingMode.DISABLED
                    lightEstimationMode = Config.LightEstimationMode.DISABLED
                    focusMode = Config.FocusMode.AUTO
                })
                session = s
                geometryDirty = true
            }
            session!!.resume()
            view.onResume()
            running = true; problem = "Starting: move the phone slowly so it can see the room."
            return null
        } catch (t: Throwable) {
            return "AR could not start on this phone (${t.javaClass.simpleName}). Use the sweep method instead."
        }
    }

    fun pause() {
        if (!running) return
        running = false; tracking = false
        view.onPause()
        runCatching { session?.pause() }
    }

    fun release() {
        pause()
        val s = session; session = null
        if (s != null) Thread { runCatching { s.close() } }.start()     // close() can block for seconds
    }

    /** Positions collected since the last call (about 15 per second while tracking). */
    fun drain(): List<Sample> = synchronized(pending) { ArrayList(pending).also { pending.clear() } }

    // ------------------------------------------------------------------ GL: camera picture + pose

    private var tex = 0
    private var prog = 0
    private var aPos = 0
    private var aUv = 0
    private var geometryDirty = true
    private var uvReady = false
    private var vw = 1
    private var vh = 1
    private val quad = floats(floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f))
    private val uv = floats(FloatArray(8))

    private fun floats(a: FloatArray): FloatBuffer =
        ByteBuffer.allocateDirect(a.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(a); position(0) }

    private fun shader(type: Int, src: String) = GLES20.glCreateShader(type).also {
        GLES20.glShaderSource(it, src); GLES20.glCompileShader(it)
    }

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        val t = IntArray(1); GLES20.glGenTextures(1, t, 0); tex = t[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, tex)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        prog = GLES20.glCreateProgram()
        GLES20.glAttachShader(prog, shader(GLES20.GL_VERTEX_SHADER,
            "attribute vec4 aPos; attribute vec2 aUv; varying vec2 vUv; void main(){ gl_Position = aPos; vUv = aUv; }"))
        GLES20.glAttachShader(prog, shader(GLES20.GL_FRAGMENT_SHADER,
            "#extension GL_OES_EGL_image_external : require\nprecision mediump float; varying vec2 vUv; " +
                "uniform samplerExternalOES uTex; void main(){ gl_FragColor = texture2D(uTex, vUv); }"))
        GLES20.glLinkProgram(prog)
        aPos = GLES20.glGetAttribLocation(prog, "aPos"); aUv = GLES20.glGetAttribLocation(prog, "aUv")
        geometryDirty = true; uvReady = false
    }

    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) {
        GLES20.glViewport(0, 0, w, h); vw = w; vh = h; geometryDirty = true
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClearColor(0.06f, 0.07f, 0.08f, 1f)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        val s = session ?: return
        if (!running) return
        try {
            if (geometryDirty) {
                @Suppress("DEPRECATION")
                val rot = (act.getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay?.rotation ?: Surface.ROTATION_0
                s.setDisplayGeometry(rot, vw, vh); geometryDirty = false
            }
            s.setCameraTextureName(tex)
            val frame = s.update()
            if (frame.hasDisplayGeometryChanged() || !uvReady) {
                quad.position(0); uv.position(0)
                frame.transformCoordinates2d(Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES, quad,
                    Coordinates2d.TEXTURE_NORMALIZED, uv)
                uvReady = true
            }
            quad.position(0); uv.position(0)
            GLES20.glUseProgram(prog)
            GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, tex)
            GLES20.glEnableVertexAttribArray(aPos); GLES20.glVertexAttribPointer(aPos, 2, GLES20.GL_FLOAT, false, 0, quad)
            GLES20.glEnableVertexAttribArray(aUv); GLES20.glVertexAttribPointer(aUv, 2, GLES20.GL_FLOAT, false, 0, uv)
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

            val cam = frame.camera
            tracking = cam.trackingState == TrackingState.TRACKING
            if (!tracking) {
                problem = when (cam.trackingFailureReason.name) {
                    "INSUFFICIENT_LIGHT" -> "Too dark for the camera. Turn on more light."
                    "EXCESSIVE_MOTION" -> "Moving too fast. Slow down."
                    "INSUFFICIENT_FEATURES" -> "The camera sees a blank surface. Point it at something with detail."
                    "CAMERA_UNAVAILABLE" -> "Another app is using the camera."
                    else -> "Finding the phone's position: move it slowly from side to side."
                }
                return
            }
            problem = null
            // Wall-clock time of this camera picture (it is a few tens of ms old when we get it).
            val now = System.currentTimeMillis()
            val ageMs = (SystemClock.elapsedRealtimeNanos() - frame.timestamp) / 1_000_000
            val p = cam.pose
            val smp = Sample(if (ageMs in 0..500) now - ageMs else now, p.tx(), p.ty(), p.tz())
            last = smp
            if (smp.tMs - lastKeptMs >= 60) {
                lastKeptMs = smp.tMs
                synchronized(pending) { if (pending.size < 3000) pending.add(smp) }
            }
        } catch (t: Throwable) {
            tracking = false; problem = "AR stopped: ${t.javaClass.simpleName}"
        }
    }
}
