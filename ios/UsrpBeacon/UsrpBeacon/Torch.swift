import AVFoundation

/// Flashlight indicator: on while the beacon is radiating, off during the blink's quiet part.
/// (iPhones have no notification LED, so this replaces the Android LED/torch modes.)
/// The camera is shared with AR, so the torch can't be used during the AR walk.
final class Torch {
    private let dev = AVCaptureDevice.default(for: .video)
    private var lit = false
    var enabled = false { didSet { if !enabled { set(false) } } }
    private(set) var lastError: String?

    var available: Bool { dev?.hasTorch == true }

    /// Call often (the app's 4 Hz tick): follows the beacon's on/off state.
    func follow(_ on: Bool) { set(enabled && on) }

    private func set(_ on: Bool) {
        guard let d = dev, d.hasTorch, on != lit else { return }
        do {
            try d.lockForConfiguration()
            if on { try d.setTorchModeOn(level: 1.0) } else { d.torchMode = .off }
            d.unlockForConfiguration()
            lit = on; lastError = nil
        } catch { lastError = error.localizedDescription }
    }
}
