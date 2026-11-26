import CoreLocation
import Foundation

/// Records where the USRP is, using the phone (port of SiteRecorder.kt):
///  - position: the phone lies on top of the USRP; GPS fixes are averaged for posSeconds.
///  - facing  : the phone is held flat with its top edge pointing where the antennas face;
///              the true-north heading is averaged for faceSeconds.
/// Results go to the console as "usrp_site" / "usrp_facing" messages.
final class SiteRecorder {
    static let posSeconds = 30
    static let faceSeconds = 4
    private static let maxFixAccM = 20.0

    private let truth: TruthSource
    var send: ([String: Any], @escaping (String?) -> Void) -> Void = { _, done in done("not connected") }

    private(set) var busy: String?
    private(set) var progress = ""
    private(set) var positionText = "not recorded"
    private(set) var facingText = "not recorded"
    private(set) var lastError: String?
    private var timer: Timer?

    init(truth: TruthSource) { self.truth = truth }

    func restore(pos: String?, face: String?) {
        if let pos { positionText = pos }
        if let face { facingText = face }
    }

    private func every(_ dt: TimeInterval, _ body: @escaping (Timer) -> Void) {
        timer?.invalidate()
        let t = Timer(timeInterval: dt, repeats: true, block: body)
        RunLoop.main.add(t, forMode: .common); timer = t
        t.fire()
    }

    // MARK: position

    func recordPosition(done: @escaping (String) -> Void) {
        guard busy == nil else { return }
        busy = "position"; lastError = nil
        var fixes: [[Double]] = []            // lat, lon, alt, acc, vacc
        var lastFix: Date?
        let t0 = Date()
        every(0.25) { [weak self] t in
            guard let self else { t.invalidate(); return }
            let el = Date().timeIntervalSince(t0)
            if let l = self.truth.location {
                let age = self.truth.gpsAgeS() ?? 99
                if l.timestamp != lastFix && age < 2 && l.horizontalAccuracy >= 0 && l.horizontalAccuracy <= Self.maxFixAccM {
                    lastFix = l.timestamp
                    fixes.append([l.coordinate.latitude, l.coordinate.longitude, l.altitude, l.horizontalAccuracy,
                                  l.verticalAccuracy > 0 ? l.verticalAccuracy : .nan])
                }
            }
            let acc = self.truth.location.map { String(format: " · now ± %.1f m", $0.horizontalAccuracy) } ?? " · no GPS yet"
            self.progress = String(format: "Keep it still on the USRP… %d s left · %d GPS fixes", max(0, Self.posSeconds - Int(el)), fixes.count) + acc
            if el < Double(Self.posSeconds) { return }
            t.invalidate()
            self.finishPosition(fixes, el, done)
        }
    }

    private func finishPosition(_ fixes: [[Double]], _ seconds: Double, _ done: @escaping (String) -> Void) {
        busy = nil; progress = ""
        if fixes.count < 8 {
            fail("Only \(fixes.count) usable GPS fixes in \(Self.posSeconds) s (each must be within \(Int(Self.maxFixAccM)) m). " +
                 "GPS needs open sky: do this outdoors and wait until the accuracy reads under 10 m.", done)
            return
        }
        var sw = 0.0, la = 0.0, lo = 0.0, al = 0.0
        for f in fixes { let w = 1 / (f[3] * f[3]); sw += w; la += w * f[0]; lo += w * f[1]; al += w * f[2] }
        la /= sw; lo /= sw; al /= sw
        let mLat = 111_320.0, mLon = 111_320.0 * cos(la * .pi / 180)
        let spread = (fixes.map { f -> Double in
            let dn = (f[0] - la) * mLat, de = (f[1] - lo) * mLon; return dn * dn + de * de
        }.reduce(0, +) / Double(fixes.count)).squareRoot()
        let accs = fixes.map { $0[3] }.sorted()
        let acc = max(accs[accs.count / 2] / 2, spread)
        let vaccs = fixes.map { $0[4] }.filter { !$0.isNaN }.sorted()
        var o: [String: Any] = ["type": "usrp_site", "id": UUID().uuidString.lowercased(), "t_ms": nowMs(),
                                "lat": la, "lon": lo, "alt": al, "acc_m": acc,
                                "n_fixes": fixes.count, "spread_m": spread, "duration_s": seconds]
        if !vaccs.isEmpty { o["vacc_m"] = vaccs[vaccs.count / 2] }
        let text = String(format: "%.7f, %.7f  ± %.1f m  (%d fixes, wandered %.1f m)", la, lo, acc, fixes.count, spread)
        send(o) { [weak self] err in
            guard let self else { return }
            if let err {
                self.fail("Measured (\(text)) but the laptop didn't answer (\(err)). It is saved on the phone and will be " +
                          "sent automatically once the laptop is reachable again.", done)
            } else { self.positionText = text; done("USRP position saved on the laptop.\n\(text)") }
        }
    }

    // MARK: facing

    func recordFacing(done: @escaping (String) -> Void) {
        guard busy == nil else { return }
        busy = "facing"; lastError = nil
        var heads: [Double] = [], pitches: [Double] = [], rolls: [Double] = [], fields: [Double] = []
        let t0 = Date()
        every(0.1) { [weak self] t in
            guard let self else { t.invalidate(); return }
            let el = Date().timeIntervalSince(t0)
            if let v = self.truth.headingTrueDeg { heads.append(v) }
            if let v = self.truth.pitchDeg { pitches.append(v) }
            if let v = self.truth.rollDeg { rolls.append(v) }
            if let v = self.truth.magFieldUt { fields.append(v) }
            self.progress = "Hold it flat and still, pointing where the antennas face… \(max(0, Self.faceSeconds - Int(el)) + 1) s"
            if el < Double(Self.faceSeconds) { return }
            t.invalidate()
            self.finishFacing(heads, pitches, rolls, fields, done)
        }
    }

    private func finishFacing(_ heads: [Double], _ pitches: [Double], _ rolls: [Double], _ fields: [Double],
                              _ done: @escaping (String) -> Void) {
        busy = nil; progress = ""
        if heads.count < 10 { fail("The compass gave no readings. Is Location allowed for this app?", done); return }
        let n = Double(heads.count)
        let s = heads.map { sin($0 * .pi / 180) }.reduce(0, +) / n
        let c = heads.map { cos($0 * .pi / 180) }.reduce(0, +) / n
        let mean = (atan2(s, c) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
        let r = min(1, max(1e-9, (s * s + c * c).squareRoot()))
        let spread = (-2 * log(r)).squareRoot() * 180 / .pi
        let avgAbs: ([Double]) -> Double = { $0.isEmpty ? 0 : $0.map(abs).reduce(0, +) / Double($0.count) }
        let tilt = max(avgAbs(pitches), avgAbs(rolls))
        let field = fields.isEmpty ? Double.nan : fields.sorted()[fields.count / 2]
        let status = truth.magStatus
        if tilt > 20 { fail(String(format: "The phone was tilted %.0f°. Hold it flat (screen up), like it is lying on a table.", tilt), done); return }
        if spread > 6 { fail(String(format: "The compass moved %.0f° while measuring. Hold the phone still and try again.", spread), done); return }
        if !field.isNaN && (field < 20 || field > 70) {
            fail(String(format: "The magnetic field is %.0f µT (Earth's is 25–65): metal or electronics are bending the compass. " +
                        "Hold the phone at least 30 cm from the USRP, the laptop and anything metal, and try again.", field), done); return
        }
        if status == 0 { fail("The compass is uncalibrated. Wave the phone in a figure 8 a few times, then try again.", done); return }
        var o: [String: Any] = ["type": "usrp_facing", "id": UUID().uuidString.lowercased(), "t_ms": nowMs(),
                                "facing_deg": mean, "facing_spread_deg": spread, "n_samples": heads.count]
        if !field.isNaN { o["mag_field_ut"] = field }
        if let status { o["mag_status"] = status }
        if let d = truth.declinationDeg { o["declination_deg"] = d }
        let warn = status == 1 ? "  (compass accuracy LOW: a figure 8 wave first helps)" : ""
        let text = String(format: "%.1f° true north  ± %.1f°", mean, spread) + warn
        send(o) { [weak self] err in
            guard let self else { return }
            if let err {
                self.fail("Measured (\(text)) but the laptop didn't answer (\(err)). It is saved on the phone and will be " +
                          "sent automatically once the laptop is reachable again.", done)
            } else { self.facingText = text; done("Antenna facing saved on the laptop.\n\(text)") }
        }
    }

    private func fail(_ msg: String, _ done: (String) -> Void) { lastError = msg; done(msg) }
}

func nowMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
