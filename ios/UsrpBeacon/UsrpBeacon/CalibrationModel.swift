import Combine
import Foundation
import SwiftUI

typealias JSON = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func has(_ k: String) -> Bool { if let v = self[k] { return !(v is NSNull) } else { return false } }
    func obj(_ k: String) -> JSON? { self[k] as? JSON }
    func str(_ k: String) -> String {
        switch self[k] { case let s as String: return s; case let n as NSNumber: return n.stringValue; default: return "" }
    }
    func dbl(_ k: String, _ def: Double = .nan) -> Double { (self[k] as? NSNumber)?.doubleValue ?? def }
    func int(_ k: String) -> Int { (self[k] as? NSNumber)?.intValue ?? 0 }
    func bool(_ k: String) -> Bool { (self[k] as? NSNumber)?.boolValue ?? false }
}

/// "Calibrate the USRP from this phone" wizard (port of CalibrationScreen.kt). Talks to the
/// Capture Console's /cal endpoint through the same https:// link the telemetry uses.
/// Three methods: AR walk (ARKit), sweep (one arc), manual (two positions).
final class CalibrationModel: ObservableObject {
    private let app: AppModel
    private var visible = false
    private var prefilled = false
    private var pollTimer: Timer?
    private var ticker: AnyCancellable?

    // settings
    @Published var name: String
    @Published var channelIdx = 0             // 39, 38, 37
    @Published var spacingText = "6.0"
    @Published var secondsText = "10"
    @Published var leftIdx = 0                // 0 = RF A on my left
    @Published var methodIdx = 0 { didSet { applyMethod() } }

    // console state
    @Published private(set) var s: JSON?
    @Published private(set) var linkText = "Connecting…"
    @Published private(set) var linkOk: Bool? = nil
    @Published var errorText: String?

    let channels = [39, 38, 37]
    let ar = ArTracker()
    @Published private(set) var arVisible = false
    private(set) var origin: [Float]?
    private var posesSent = 0
    private var beginSent = false
    private(set) var stage = "idle"
    private(set) var autoBegun = false
    private(set) var phonePkts = 0
    private var clockOffsetMs = 0.0
    private(set) var bestRttMs = Double.greatestFiniteMagnitude
    private var bestAt = Date.distantPast

    init(app: AppModel) {
        self.app = app
        let df = DateFormatter(); df.locale = Locale(identifier: "en_US_POSIX"); df.dateFormat = "MMdd-HHmm"
        name = "phone-" + df.string(from: Date())
    }

    var arSupported: Bool { ArTracker.supported }

    /// "ar", "sweep" or "manual": the user's pick, or the app's when left on Automatic.
    var method: String {
        switch methodIdx { case 1: return "ar"; case 2: return "sweep"; case 3: return "manual"
        default: return arSupported ? "ar" : "sweep" }
    }

    var methodNote: (String, Color) {
        let auto = methodIdx == 0
        switch (auto, method) {
        case (true, "ar"): return ("✓ This phone supports AR, so the app chose the AR walk (most accurate: it also calibrates distance).", Theme.textSub)
        case (true, _): return ("This phone has no AR support, so the app chose the sweep.", Theme.textSub)
        case (false, "ar") where !arSupported: return ("⚠ This phone does not support AR. Pick the sweep instead.", Theme.amber)
        case (false, "ar"): return ("AR walk: the phone tracks its own position with the camera while you walk around.", Theme.textSub)
        case (false, "sweep"): return ("Sweep: carry the phone once in an arc from the left end of the antennas to the right end.", Theme.textSub)
        default: return ("Manual: place the phone at two measured spots.", Theme.textSub)
        }
    }

    // MARK: lifecycle

    func show() {
        visible = true; bestRttMs = .greatestFiniteMagnitude
        applyMethod(); poll()
        ticker = Timer.publish(every: 0.25, on: .main, in: .common).autoconnect().sink { [weak self] _ in self?.objectWillChange.send() }
    }

    func hide() {
        visible = false
        pollTimer?.invalidate(); pollTimer = nil; ticker = nil
        stopAr()
    }

    /// AR may restart with a different coordinate frame, so a stored origin is no longer valid.
    func stopAr() { if ar.running { ar.pause(); origin = nil } }

    private func applyMethod() { if method != "ar" { ar.pause() } }

    private var baseUrl: String? {
        let b = trimSlash(app.host)
        return b.hasPrefix("http") ? b : nil
    }

    private func schedulePoll(_ dt: TimeInterval) {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: dt, repeats: false) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        guard visible else { return }
        guard let base = baseUrl else {
            linkText = "✗ Calibration needs the https:// link. Put it in \"Laptop IP address or link\" on the main screen."
            linkOk = false
            schedulePoll(1.5); return
        }
        request("GET", base + "/cal", nil) { [weak self] code, body, err, t0, t1 in
            guard let self else { return }
            if code == 200, let s = body {
                let rtt = (t1 - t0) * 1000
                if s.has("now"), rtt > 0, rtt <= self.bestRttMs || Date().timeIntervalSince(self.bestAt) > 30 {
                    self.bestRttMs = rtt; self.bestAt = Date()
                    // the console's "now" belongs to the middle of this request
                    self.clockOffsetMs = s.dbl("now") * 1000 - (t0 + t1) / 2 * 1000
                }
                self.render(s)
                self.uploadPoses()
            } else {
                self.offline(err ?? "HTTP \(code)")
            }
            self.schedulePoll(0.7)
        }
    }

    /// HTTP request; callback on the main thread with (code, json, error, t0, t1) (t in epoch seconds).
    private func request(_ method: String, _ url: String, _ json: JSON?,
                         _ cb: @escaping (Int, JSON?, String?, Double, Double) -> Void) {
        guard let u = URL(string: url) else { cb(-1, nil, "bad link", 0, 0); return }
        var r = URLRequest(url: u, timeoutInterval: 8)
        r.httpMethod = method
        r.setValue("1", forHTTPHeaderField: "ngrok-skip-browser-warning")
        if let json, let b = try? JSONSerialization.data(withJSONObject: json) {
            r.httpBody = b; r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let t0 = Date().timeIntervalSince1970
        URLSession.shared.dataTask(with: r) { data, resp, e in
            let t1 = Date().timeIntervalSince1970
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? JSON
            DispatchQueue.main.async { cb(code, obj, e?.localizedDescription, t0, t1) }
        }.resume()
    }

    func command(_ action: String, _ extra: JSON = [:]) {
        guard let base = baseUrl else { return }
        var j = extra; j["action"] = action
        request("POST", base + "/cal", j) { [weak self] _, r, err, _, _ in
            guard let self else { return }
            guard let r else { self.errorText = err ?? "No answer from the console"; return }
            if let st = r.obj("state") { self.render(st) }
            if !r.bool("ok"), r.has("msg") { self.errorText = r.str("msg") }
        }
    }

    private func offline(_ err: String) {
        linkText = "✗ Can't reach the console: \(err)\nIs app.py running on the Mac, and ngrok open?"
        linkOk = false
        s = nil
    }

    var connected: Bool { s != nil }

    // MARK: actions

    private var commonSettings: JSON {
        ["name": name.trimmingCharacters(in: .whitespaces), "channel": channels[channelIdx],
         "spacing_cm": Double(spacingText.replacingOccurrences(of: ",", with: ".")) ?? 6.0,
         "rfa_side": leftIdx == 0 ? "left" : "right", "tag": app.tag, "stop_other": true]
    }

    func measureFront() {
        app.beaconForCalibration()
        var j = commonSettings
        j["seconds"] = Double(secondsText.replacingOccurrences(of: ",", with: ".")) ?? 10.0
        command("front", j)
    }

    func measureSide() { app.beaconForCalibration(); command("side", ["stop_other": true]) }

    private func autoStart(_ m: String) {
        app.beaconForCalibration()
        beginSent = false; posesSent = 0
        _ = ar.drain()
        var j = commonSettings; j["method"] = m
        command("auto_start", j)
    }

    /// One button, three jobs: start the radio -> "go" when you start walking -> "done" at the end.
    func sweepPressed() {
        if stage == "sweep_running" && !autoBegun { command("auto_begin") }
        else if stage == "sweep_running" { command("auto_stop") }
        else { autoStart("sweep") }
    }

    func arStartPressed() {
        app.torchOn = false                       // the camera can't be shared with the flashlight
        arVisible = true
        if let e = ar.start() { errorText = e }
    }

    func setOrigin() {
        guard let p = ar.last else { return }
        origin = [p.x, p.y, p.z]
        app.show("Origin set: this spot is now the antennas")
    }

    func walkPressed() {
        if stage == "ar_running" {
            uploadPoses()
            var j: JSON = [:]; if let o = originJson { j["origin"] = o }
            command("auto_stop", j)
        } else { autoStart("ar") }
    }

    private var originJson: [Double]? { origin.map { $0.map(Double.init) } }

    /// Send the positions collected since last time, stamped on the console's clock.
    private func uploadPoses() {
        let batch = ar.drain()
        guard stage == "ar_running", !batch.isEmpty, let base = baseUrl else { return }
        if !beginSent { beginSent = true; command("auto_begin") }
        let arr: [[Double]] = batch.map { [($0.tMs + clockOffsetMs) / 1000, Double($0.x), Double($0.y), Double($0.z)] }
        posesSent += batch.count
        var j: JSON = ["action": "auto_poses", "poses": arr]
        if let o = originJson { j["origin"] = o }
        request("POST", base + "/cal", j) { _, _, _, _, _ in }
    }

    // MARK: derived display

    private func render(_ st: JSON) {
        s = st
        linkText = "✓ Connected to the Capture Console"; linkOk = true
        if !prefilled, let d = st.obj("defaults") {
            spacingText = fmt(d.dbl("spacing_m", 0.06) * 100, 1)
            let f = d.dbl("freq", 2.480e9)
            channelIdx = f < 2.41e9 ? 2 : (f < 2.45e9 ? 1 : 0)
            leftIdx = d.str("rfa_side") == "right" ? 1 : 0
            prefilled = true
        }
        let err = st.str("error")
        errorText = (err.isEmpty || err == "null") ? nil : err
        stage = st.has("stage") ? st.str("stage") : "idle"
        autoBegun = st.obj("auto")?.bool("begun") ?? false
        phonePkts = st.obj("progress")?.int("phone_pkts") ?? 0
    }

    var measuring: String { let v = s?.str("measuring") ?? ""; return v == "null" ? "" : v }
    var front: JSON? { s?.obj("front") }
    var side: JSON? { s?.obj("side") }
    var saved: JSON? { s?.obj("saved") }
    var autoFront: JSON? { front.flatMap { $0.has("method") ? $0 : nil } }
    var idle: Bool { measuring.isEmpty && stage != "fitting" }

    var usbStatus: (String, Color) {
        guard let usb = s?.obj("usb"), usb.has("connected") else { return ("USRP: checking…", Theme.textSub) }
        if !usb.bool("connected") { return ("✗ USRP not connected to the Mac", Theme.red) }
        let fast = usb.bool("fast_enough")
        var prod = usb.str("product"); if prod.hasPrefix("USRP ") { prod.removeFirst(5) }
        return ("USRP \(prod) on \(usb.str("usb")) (\(usb.str("rate")))" + (fast ? "" : "\nUSB 2 can drop samples: use a USB 3 (blue) port if you can."),
                fast ? Theme.green : Theme.amber)
    }

    var busyText: String? {
        guard let s, s.bool("busy"), measuring.isEmpty else { return nil }
        return "The B210 is running \(s.str("busy_kind")). Measuring will stop it."
    }

    func autoResult(_ f: JSON) -> String {
        var t = "✓ \(f.str("quality").uppercased()): offset \(signed(f.dbl("phase_deg")))° (spread ±\(fmt(f.dbl("phase_spread_deg"), 0))°)\n"
        t += "antenna spacing fitted \(fmt(f.dbl("spacing_m") * 100, 1)) cm" +
             (f.has("ruler_spacing_m") ? " (you entered \(fmt(f.dbl("ruler_spacing_m") * 100, 1)))" : "") + "\n"
        t += f.bool("flip") ? "left/right were mirrored: fixed\n" : "left/right correct\n"
        t += "power at 1 m \(fmt(f.dbl("ref_power_db"))) dB · \(f.int("n_bursts")) packets used"
        if f.has("path_loss_exp") { t += "\ndistance model: path-loss exponent \(fmt(f.dbl("path_loss_exp"), 2))" }
        if f.has("bearing_err_median_deg") {
            t += "\ntypical direction error of one packet: \(fmt(f.dbl("bearing_err_median_deg"), 0))° (walk covered \(fmt(f.dbl("span_deg"), 0))°)"
        }
        return t
    }

    // sweep card
    var sweep: (button: String, enabled: Bool, text: String, color: Color) {
        let elapsed = s?.obj("auto")?.dbl("elapsed", 0) ?? 0
        if stage == "sweep_running" && !autoBegun {
            return ("GO · I'M STARTING TO WALK NOW", phonePkts > 0,
                    phonePkts > 0 ? "Radio ready (\(phonePkts) packets from your phone). Stand at the LEFT end, tap GO, then walk the arc."
                                  : "Starting the B210 (about 5 s)… stay at the left end.", Theme.textMain)
        }
        if stage == "sweep_running" {
            return ("DONE · I'M AT THE RIGHT END", true, "Walking… \(fmt(elapsed, 0)) s · \(phonePkts) packets. Aim for 10–15 s.", Theme.textMain)
        }
        if stage == "fitting" { return ("START SWEEP", false, "Working it out…", Theme.textMain) }
        if let f = autoFront, f.str("method") == "sweep" { return ("SWEEP AGAIN", connected && idle, autoResult(f), Theme.green) }
        return ("START SWEEP", connected && idle, "Not measured yet.", Theme.textSub)
    }

    // AR card
    var walk: (button: String, enabled: Bool, text: String, color: Color) {
        if stage == "ar_running" { return ("DONE WALKING", origin != nil, "Recording: walk a wide arc in front of the antennas.", Theme.textMain) }
        if stage == "fitting" { return ("START WALK", false, "Working it out…", Theme.textMain) }
        let en = connected && idle && ar.tracking && origin != nil
        if let f = autoFront, f.str("method") == "ar" { return ("WALK AGAIN", en, autoResult(f), Theme.green) }
        return ("START WALK", en, "Not measured yet.", Theme.textSub)
    }

    var arStatus: (String, Color) {
        let running = ar.running
        let p = ar.last
        var dist: Double?
        if let p, let o = origin {
            let dx = Double(p.x - o[0]), dy = Double(p.y - o[1]), dz = Double(p.z - o[2])
            dist = (dx * dx + dy * dy + dz * dz).squareRoot()
        }
        let text: String
        if !running { text = ar.problem ?? "AR is off. Tap START AR." }
        else if !ar.tracking { text = "● " + (ar.problem ?? "Finding the phone's position…") }
        else if origin == nil { text = "● Tracking. Now hold the phone right between the two antennas and tap SET ORIGIN." }
        else if stage == "ar_running" {
            var t = "● Recording · \(fmt(dist ?? 0, 2)) m from the antennas · \(phonePkts) packets · \(posesSent) positions"
            if (dist ?? 1) < 0.4 { t += "\nToo close: packets nearer than 0.4 m are not used." }
            if bestRttMs > 400 && bestRttMs < .greatestFiniteMagnitude { t += "\nSlow link (\(fmt(bestRttMs, 0)) ms): walk slowly so timing errors stay small." }
            text = t
        } else { text = "● Tracking · \(fmt(dist ?? 0, 2)) m from the antennas. Ready to walk." }
        return (text, running && ar.tracking ? Theme.green : (running ? Theme.amber : Theme.textSub))
    }

    var arBusy: Bool { stage == "ar_running" || stage == "fitting" }

    // manual step 1
    var frontProgress: Double {
        if measuring == "front" { return progressFraction }
        return front != nil ? 1 : 0
    }
    var frontText: (String, Color) {
        if measuring == "front" { return (progressText, Theme.textMain) }
        guard let f = front else { return ("Not measured yet.", Theme.textSub) }
        let q = f.str("quality")
        var t = "✓ \(q.uppercased()): offset \(signed(f.dbl("phase_deg")))° (spread ±\(fmt(f.dbl("phase_spread_deg"), 0))°)\n" +
                "power at 1 m \(fmt(f.dbl("ref_power_db"))) dB · \(f.int("n_bursts")) packets from your phone"
        if q == "poor" { t += "\nPoor = the packets disagree a lot. Move away from walls/metal and measure again." }
        return (t, q == "good" ? Theme.green : (q == "ok" ? Theme.amber : Theme.red))
    }

    // manual step 2
    var sideProgress: Double {
        if measuring == "side" { return progressFraction }
        return side != nil ? 1 : 0
    }
    var sideText: (String, Color) {
        if measuring == "side" { return (progressText, Theme.textMain) }
        guard let sd = side else { return (front == nil ? "Do step 1 first." : "Not measured yet.", Theme.textSub) }
        let ok = sd.has("angle") || sd.bool("auto")
        let t: String
        if sd.has("angle") && !sd.bool("flip") { t = "✓ Left/right correct: your phone read \(fmt(sd.dbl("angle"), 0))° to the right." }
        else if sd.bool("auto") { t = "Left/right: \(sd.str("check"))" }
        else if sd.has("angle") { t = "↔ Mirrored: your phone read \(fmt(-sd.dbl("angle"), 0))° to the LEFT. The saved calibration will flip it, so that's fixed." }
        else { t = "Skipped: left and right are not verified." }
        return (t, ok ? Theme.green : Theme.amber)
    }

    private var progressFraction: Double {
        guard let p = s?.obj("progress") else { return 0 }
        let dur = p.dbl("dur", 10)
        return max(0, min(1, p.dbl("t", 0) / (dur > 0 ? dur : 10)))
    }
    private var progressText: String {
        guard let p = s?.obj("progress") else { return "Starting the B210 (about 4 s)…" }
        let t = p.dbl("t", 0), dur = p.dbl("dur", 10), n = p.int("phone_pkts")
        var out = "Measuring… \(fmt(t, 0)) / \(fmt(dur, 0)) s · \(n) packets from your phone"
        if p.has("phase") { out += "\noffset so far \(signed(p.dbl("phase")))° (spread ±\(fmt(p.dbl("spread"), 0))°)" }
        if n == 0 && t > 3 { out += "\nNo packets from your phone yet: is the beacon on and the phone close?" }
        return out
    }

    // save
    var summary: String {
        guard let f = front else { return "Nothing to save yet." }
        let cfg = s?.obj("cfg") ?? [:]
        var t = "name      \(cfg.str("name"))\n" +
                "channel   \(cfg.int("channel")) (\(fmt(cfg.dbl("freq", 0) / 1e9, 3)) GHz) · gain \(fmt(cfg.dbl("gain", 0), 0)) dB\n" +
                "spacing   \(fmt(cfg.dbl("spacing_m", 0) * 100, 1)) cm\n" +
                "offset    \(signed(f.dbl("phase_deg")))° · \(f.str("quality"))\n"
        if f.has("method") { t += "method    \(f.str("method") == "ar" ? "AR walk" : "sweep")\n" }
        if f.has("path_loss_exp") { t += "distance  path-loss exponent \(fmt(f.dbl("path_loss_exp"), 2))\n" }
        t += "sides     \(side.map { $0.str("check") } ?? "not checked yet")"
        if let sv = saved { t += "\n\n✓ Saved as \"\(sv.str("name"))\" and set active." }
        return t
    }
    var canSave: Bool { connected && measuring.isEmpty && front != nil && side != nil && saved == nil }
    var canLive: Bool { connected && saved != nil && measuring.isEmpty }
}
