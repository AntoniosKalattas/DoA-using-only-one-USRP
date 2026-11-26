import CoreLocation
import Foundation

/// Phone lying on top of the USRP -> GPS fixes averaged for `seconds` -> one "usrp_site" message
/// (same fields as the Android / iOS beacon apps, so the console treats it identically).
@MainActor
final class SiteModel: ObservableObject {
    static let maxFixAccM = 20.0          // ignore fixes worse than this

    @Published var link: String { didSet { UserDefaults.standard.set(link, forKey: "link") } }
    @Published var seconds: Int { didSet { UserDefaults.standard.set(seconds, forKey: "seconds") } }
    @Published private(set) var measuring = false
    @Published private(set) var progress = ""
    @Published private(set) var result = ""
    @Published var testResult = ""

    let gps = Location()
    let outbox = Outbox()
    private var timer: Timer?

    init() {
        let d = UserDefaults.standard
        link = d.string(forKey: "link") ?? ""
        seconds = d.integer(forKey: "seconds") == 0 ? 30 : d.integer(forKey: "seconds")
        outbox.link = { [weak self] in self?.link ?? "" }
    }

    func start() { gps.start(); outbox.flush() }

    func cancel() { timer?.invalidate(); measuring = false; progress = ""; result = "Cancelled." }

    func measure() {
        guard !measuring else { return }
        measuring = true; result = ""
        var fixes: [CLLocation] = []
        var lastTs: Date?
        let t0 = Date(), dur = Double(seconds)
        timer?.invalidate()
        let t = Timer(timeInterval: 0.25, repeats: true) { [weak self] tm in
            Task { @MainActor in
                guard let self else { tm.invalidate(); return }
                let el = Date().timeIntervalSince(t0)
                if let l = self.gps.last, l.timestamp != lastTs, (self.gps.ageS ?? 99) < 2,
                   l.horizontalAccuracy >= 0, l.horizontalAccuracy <= Self.maxFixAccM {
                    lastTs = l.timestamp; fixes.append(l)
                }
                let now = self.gps.last.map { String(format: "now ± %.1f m", $0.horizontalAccuracy) } ?? "no GPS yet"
                self.progress = "Don't move the phone · \(max(0, Int(dur - el))) s left · \(fixes.count) good fixes · \(now)"
                if el < dur { return }
                tm.invalidate()
                self.finish(fixes, el)
            }
        }
        RunLoop.main.add(t, forMode: .common); timer = t
    }

    private func finish(_ fixes: [CLLocation], _ el: Double) {
        measuring = false; progress = ""
        guard fixes.count >= 8 else {
            result = "✗ Only \(fixes.count) good GPS fixes (each must be under \(Int(Self.maxFixAccM)) m). " +
                     "Go outdoors with open sky, wait until it says under 10 m, and try again."
            return
        }
        // weight each fix by 1/accuracy²
        var sw = 0.0, la = 0.0, lo = 0.0, al = 0.0
        for f in fixes {
            let w = 1 / (f.horizontalAccuracy * f.horizontalAccuracy)
            sw += w; la += w * f.coordinate.latitude; lo += w * f.coordinate.longitude; al += w * f.altitude
        }
        la /= sw; lo /= sw; al /= sw
        let mLat = 111_320.0, mLon = 111_320.0 * cos(la * .pi / 180)
        let spread = (fixes.map { f -> Double in
            let dn = (f.coordinate.latitude - la) * mLat, de = (f.coordinate.longitude - lo) * mLon
            return dn * dn + de * de
        }.reduce(0, +) / Double(fixes.count)).squareRoot()
        let accs = fixes.map(\.horizontalAccuracy).sorted()
        let acc = max(accs[accs.count / 2] / 2, spread)
        let vaccs = fixes.map(\.verticalAccuracy).filter { $0 > 0 }.sorted()
        var o: [String: Any] = ["type": "usrp_site", "id": UUID().uuidString.lowercased(),
                                "t_ms": Int64(Date().timeIntervalSince1970 * 1000),
                                "lat": la, "lon": lo, "alt": al, "acc_m": acc,
                                "n_fixes": fixes.count, "spread_m": spread, "duration_s": el]
        if !vaccs.isEmpty { o["vacc_m"] = vaccs[vaccs.count / 2] }
        outbox.add(o)
        result = String(format: "✓ %.7f, %.7f  ± %.1f m\n%d fixes, wandered %.1f m. Saved on the phone; it is sent to the laptop as soon as it can be.",
                        la, lo, acc, fixes.count, spread)
    }

    func test() { testResult = "testing…"; outbox.test { [weak self] in self?.testResult = $0 } }
}
