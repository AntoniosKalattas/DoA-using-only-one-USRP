import ARKit
import AVFoundation
import SceneKit
import SwiftUI

/// Where is this phone? ARKit (camera + motion sensors) tracks the phone's position in the room to a
/// few centimetres (port of ArTracker.kt, which used ARCore). The AR walk sends these positions to
/// the Mac, which then knows the true direction and distance of every packet the USRP received.
///
/// Coordinates: metres, y = up (gravity), origin = where the phone was when tracking started —
/// the same convention as ARCore, so the console needs no changes.
final class ArTracker: NSObject, ARSessionDelegate {

    struct Sample { let tMs: Double; let x: Float; let y: Float; let z: Float }

    static var supported: Bool { ARWorldTrackingConfiguration.isSupported }

    let session = ARSession()
    private(set) var running = false
    private(set) var tracking = false
    private(set) var problem: String?
    private(set) var last: Sample?
    private var pending: [Sample] = []
    private var lastKeptMs = 0.0

    override init() {
        super.init()
        session.delegate = self
        session.delegateQueue = .main
    }

    /// Returns nil when tracking started, else what the user has to do first.
    func start() -> String? {
        if running { return nil }
        guard Self.supported else { return "This iPhone can't do AR world tracking. Use the sweep method instead." }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .denied, .restricted:
            return "Camera access is off: Settings › USRP Beacon › Camera. AR finds the phone's position from the camera picture."
        default: break
        }
        let cfg = ARWorldTrackingConfiguration()
        cfg.planeDetection = []
        cfg.isLightEstimationEnabled = false
        cfg.worldAlignment = .gravity
        session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
        running = true
        problem = "Starting: move the phone slowly so it can see the room."
        return nil
    }

    func pause() {
        guard running else { return }
        running = false; tracking = false
        session.pause()
    }

    /// Positions collected since the last call (about 15 per second while tracking).
    func drain() -> [Sample] { defer { pending.removeAll() }; return pending }

    // MARK: ARSessionDelegate (main queue)

    func session(_ s: ARSession, didUpdate frame: ARFrame) {
        guard running else { return }
        let cam = frame.camera
        switch cam.trackingState {
        case .normal:
            tracking = true; problem = nil
        case .notAvailable:
            tracking = false; problem = "AR tracking is not available right now."; return
        case .limited(let why):
            tracking = false
            switch why {
            case .excessiveMotion: problem = "Moving too fast. Slow down."
            case .insufficientFeatures: problem = "The camera sees a blank surface or it is too dark. Point it at something with detail."
            case .relocalizing: problem = "Re-finding its position: hold still for a moment."
            default: problem = "Finding the phone's position: move it slowly from side to side."
            }
            return
        }
        // Wall-clock time of this camera picture (frame.timestamp is on the uptime clock).
        let ageMs = (ProcessInfo.processInfo.systemUptime - frame.timestamp) * 1000
        let now = Date().timeIntervalSince1970 * 1000
        let p = cam.transform.columns.3
        let smp = Sample(tMs: (0...500).contains(ageMs) ? now - ageMs : now, x: p.x, y: p.y, z: p.z)
        last = smp
        if smp.tMs - lastKeptMs >= 60 {
            lastKeptMs = smp.tMs
            if pending.count < 3000 { pending.append(smp) }
        }
    }

    func session(_ s: ARSession, didFailWithError error: Error) {
        tracking = false; running = false
        problem = "AR stopped: \(error.localizedDescription)"
    }

    func sessionWasInterrupted(_ s: ARSession) { tracking = false; problem = "AR paused (camera interrupted)." }
}

/// Camera preview for the AR walk (ARKit needs nothing drawn, but seeing the room helps aim).
struct ArPreview: UIViewRepresentable {
    let tracker: ArTracker
    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView(frame: .zero)
        v.session = tracker.session
        v.automaticallyUpdatesLighting = false
        v.scene = SCNScene()
        return v
    }
    func updateUIView(_ v: ARSCNView, context: Context) {}
}
