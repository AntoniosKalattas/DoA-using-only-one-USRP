import Combine
import SwiftUI
import UIKit

/// Main screen state + actions (port of MainActivity.kt). A 4 Hz tick republishes so the live
/// readouts follow the beacon / GPS / compass, like the Android refresh loop.
final class AppModel: ObservableObject {
    let beacon = Beacon()
    let truth = TruthSource()
    let torch = Torch()
    let link: Telemetry
    let site: SiteRecorder

    private let prefs = UserDefaults.standard
    // Current setup: ngrok tunnel to the Capture Console's phone ingest. The free ngrok link
    // changes whenever ngrok restarts, so whatever you type is remembered.
    static let defaultHost = "https://2711-62-228-144-38.ngrok-free.app"

    // ---- settings (persisted)
    @Published var tagText: String { didSet { prefs.set(tagText, forKey: "tag") } }
    @Published var blinkOn: Bool { didSet { prefs.set(blinkOn, forKey: "blink") } }
    @Published var blinkSecondsText: String { didSet { prefs.set(blinkSecondsText, forKey: "blink_s") } }
    @Published var torchOn: Bool { didSet { prefs.set(torchOn, forKey: "torch"); torch.enabled = torchOn } }
    @Published var hostText: String { didSet { prefs.set(hostText.trimmingCharacters(in: .whitespaces), forKey: "host"); retarget() } }
    @Published var portText: String { didSet { prefs.set(portText, forKey: "port"); retarget() } }
    @Published var streaming = false { didSet { if streaming != oldValue { toggleStreaming() } } }
    @Published var bearingText = "0"
    @Published var rangeText = "3"

    // ---- transient
    @Published var toast: String?
    @Published var siteStatus = ""
    @Published var siteStatusOk = true
    private(set) var markCount = 0
    private(set) var markBearing: Double?
    private(set) var markRange: Double?
    private var udpDest = ""
    private var ticker: AnyCancellable?

    init() {
        let p = UserDefaults.standard
        tagText = p.string(forKey: "tag") ?? "1"
        blinkOn = p.object(forKey: "blink") as? Bool ?? true
        blinkSecondsText = p.string(forKey: "blink_s") ?? "2"
        torchOn = p.bool(forKey: "torch")
        hostText = p.string(forKey: "host").flatMap { $0.isEmpty ? nil : $0 } ?? Self.defaultHost
        portText = p.string(forKey: "port").flatMap { $0.isEmpty ? nil : $0 } ?? "5005"
        link = Telemetry(store: TelemetryStore())
        site = SiteRecorder(truth: truth)
        torch.enabled = torchOn
        site.restore(pos: p.string(forKey: "site_pos"), face: p.string(forKey: "site_face"))
        site.send = { [weak self] json, done in
            guard let self else { return }
            let h = self.host
            if h.isEmpty { done("No laptop address in section 2.") } else { self.link.sendJson(host: h, port: self.port, json, onDone: done) }
        }
        ticker = Timer.publish(every: 0.25, on: .main, in: .common).autoconnect().sink { [weak self] _ in
            guard let self else { return }
            self.torch.follow(self.beacon.running && self.beacon.radiating)
            self.objectWillChange.send()
        }
    }

    var host: String { hostText.trimmingCharacters(in: .whitespaces) }
    var port: Int { Int(portText.trimmingCharacters(in: .whitespaces)) ?? 5005 }
    var tag: Int { min(255, max(0, Int(tagText.trimmingCharacters(in: .whitespaces)) ?? 1)) }

    func appeared() {
        UIApplication.shared.isIdleTimerDisabled = true      // the beacon only identifies itself in the foreground
        truth.start()
    }

    // MARK: telemetry

    func telemetryJson() -> [String: Any] {
        var o: [String: Any] = [
            "type": "usrp_beacon", "platform": "ios",
            "t_ms": nowMs(), "tag": beacon.tag,
            "adv_running": beacon.running, "adv_on": beacon.radiating,
            "adv_name": beacon.localName,
            "interval_ms": NSNull(), "tx_dbm": NSNull(),     // chosen by iOS, not visible to apps
        ]
        truth.putInto(&o)
        o["mark"] = markCount
        if let b = markBearing { o["truth_bearing_deg"] = b }
        if let r = markRange { o["truth_range_m"] = r }
        if UIApplication.shared.applicationState != .active { o["app_background"] = true }
        return o
    }

    // MARK: actions

    func toggleBeacon() {
        if beacon.running { beacon.stop(); return }
        let blinkS = blinkOn ? max(0.2, Double(blinkSecondsText.replacingOccurrences(of: ",", with: ".")) ?? 2.0) : 0
        beacon.start(tag: tag, blinkPeriod: blinkS)
    }

    /// Calibration wants lots of packets: non-stop (no blink).
    func beaconForCalibration() {
        blinkOn = false
        if beacon.running && !beacon.blinking && beacon.tag == tag { return }
        beacon.start(tag: tag, blinkPeriod: 0)
    }

    private func toggleStreaming() {
        if streaming {
            udpDest = Telemetry.isHttp(host) ? trimSlash(host) + "/phone" : "\(host):\(port)"
            link.start(host: host, port: port, rateHz: 5.0) { [weak self] in self?.telemetryJson() ?? [:] }
        } else { link.stop() }
    }

    /// Re-target the stream when the address is edited while streaming.
    private func retarget() { if streaming { toggleStreaming() } }

    func mark() {
        markCount += 1
        markBearing = Double(bearingText.replacingOccurrences(of: ",", with: "."))
        markRange = Double(rangeText.replacingOccurrences(of: ",", with: "."))
        if streaming { link.sendNow(host: host, port: port, telemetryJson()) }
        show("Mark #\(markCount) " + (streaming ? "sent to laptop" : "NOT sent — turn on 'Send info to laptop'"))
    }

    func recordSitePosition() { siteStatus = ""; site.recordPosition { [weak self] in self?.siteResult($0) } }
    func recordSiteFacing() { siteStatus = ""; site.recordFacing { [weak self] in self?.siteResult($0) } }

    private func siteResult(_ msg: String) {
        siteStatusOk = site.lastError == nil
        siteStatus = msg
        if siteStatusOk { prefs.set(site.positionText, forKey: "site_pos"); prefs.set(site.facingText, forKey: "site_face") }
    }

    func show(_ msg: String) {
        toast = msg
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) { [weak self] in if self?.toast == msg { self?.toast = nil } }
    }

    // MARK: readouts

    var statusTitle: (String, Color) {
        if !beacon.running { return ("IDLE", Theme.grey) }
        if !beacon.bluetoothOn { return ("NO BLUETOOTH", Theme.red) }
        return beacon.radiating ? ("TRANSMITTING", Theme.green) : ("PAUSED (blink)", Theme.amber)
    }

    var statusSub: String {
        if !beacon.running { return beacon.bluetoothProblem ?? beacon.state }
        if !beacon.bluetoothOn { return beacon.state }
        return beacon.radiating ? "Sending Bluetooth packets now\nas \"\(beacon.localName)\" · interval set by iOS"
                                : "Quiet part of the blink\nno signal right now"
    }

    var gpsText: String {
        if let p = truth.locationProblem, truth.location == nil { return p }
        guard let l = truth.location else { return "waiting… (needs sky view)" }
        return String(format: "%.6f, %.6f", l.coordinate.latitude, l.coordinate.longitude)
    }
    var accText: String {
        guard let l = truth.location else { return "—" }
        return String(format: "± %.1f m  ·  %.0f s old", l.horizontalAccuracy, truth.gpsAgeS() ?? 0)
    }
    var headingText: String {
        guard let h = truth.headingTrueDeg else { return "—" }
        return String(format: "%.0f°  %@", h, compass(h)) + (truth.headingAccDeg.map { String(format: "  ± %.0f°", $0) } ?? "")
    }
    var tiltText: String {
        guard let p = truth.pitchDeg else { return "—" }
        return String(format: "pitch %.0f°  roll %.0f°", p, truth.rollDeg ?? 0)
    }
    var magText: String {
        guard let f = truth.magFieldUt else { return "—" }
        let s = String(format: "%.0f µT  %@", f, (25...65).contains(f) ? "normal" : "DISTURBED")
        switch truth.magStatus { case 0: return s + " · uncalibrated"; case 1: return s + " · low accuracy"; default: return s }
    }
    var pressureText: String { truth.pressureHpa.map { String(format: "%.1f hPa", $0) } ?? "—" }
    var packetsText: String {
        if !streaming { return "off" }
        var s = "\(link.sent) → \(udpDest)"
        if let e = link.lastError { s += "\n⚠ \(e)" }
        let n = link.pending
        if n > 0 { s += "\n📦 \(n) saved on the phone, waiting for the laptop" + (link.backlogError.map { " (retrying: \($0))" } ?? "") }
        else if let u = link.lastUpload { s += "\n✓ all caught up (\(u))" }
        return s
    }
    var marksText: String {
        markCount == 0 ? "none yet" : "\(markCount)  (last: \(markBearing.map { fmt($0) } ?? "?")° at \(markRange.map { fmt($0) } ?? "?") m)"
    }

    private func compass(_ d: Double) -> String {
        ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Int(((d + 22.5).truncatingRemainder(dividingBy: 360)) / 45) % 8]
    }
}

func fmt(_ v: Double, _ d: Int = 1) -> String { v.isNaN ? "NaN" : String(format: "%.\(d)f", v) }
func signed(_ v: Double, _ d: Int = 1) -> String { (v >= 0 ? "+" : "") + fmt(v, d) }
