import CoreLocation
import CoreMotion
import Foundation

/// Everything the phone knows about where it is and which way it points (port of TruthSource.kt):
///  - GPS fix: position, altitude, accuracies, speed, course, fix time
///  - orientation: true-north heading (+ accuracy), magnetic heading, pitch, roll
///  - magnetometer: field strength (Earth = 25-65 uT) and calibration state
///  - barometer
/// iOS does not expose satellite counts, so sats_seen / sats_used are never sent.
final class TruthSource: NSObject, CLLocationManagerDelegate {
    private let lm = CLLocationManager()
    private let motion = CMMotionManager()
    private let altimeter = CMAltimeter()
    private var started = false

    private(set) var location: CLLocation?
    private(set) var headingTrueDeg: Double?
    private(set) var headingMagDeg: Double?
    private(set) var headingAccDeg: Double?
    private(set) var declinationDeg: Double?
    private(set) var pitchDeg: Double?          // 0 = lying flat
    private(set) var rollDeg: Double?
    private(set) var magFieldUt: Double?
    private(set) var magStatus: Int?            // 0 uncalibrated .. 3 high (same scale as Android)
    private(set) var pressureHpa: Double?
    private var rawFieldUt: Double?

    override init() {
        super.init()
        lm.delegate = self
        lm.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        lm.distanceFilter = kCLDistanceFilterNone
        lm.headingFilter = kCLHeadingFilterNone
        lm.headingOrientation = .portrait        // heading = direction of the phone's top edge
        lm.activityType = .other
    }

    var authorized: Bool {
        let s = lm.authorizationStatus
        return s == .authorizedWhenInUse || s == .authorizedAlways
    }

    var locationProblem: String? {
        switch lm.authorizationStatus {
        case .denied, .restricted: return "Location denied: Settings › USRP Beacon › Location › While Using, Precise on."
        case .notDetermined: return "Waiting for location permission…"
        default:
            if lm.accuracyAuthorization == .reducedAccuracy { return "Precise Location is off: turn it on in Settings › USRP Beacon › Location." }
            return nil
        }
    }

    func start() {
        if lm.authorizationStatus == .notDetermined { lm.requestWhenInUseAuthorization() }
        lm.startUpdatingLocation()
        if CLLocationManager.headingAvailable() { lm.startUpdatingHeading() }
        if location == nil { location = lm.location }
        guard !started else { return }
        started = true

        if motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 0.1
            let frames = CMMotionManager.availableAttitudeReferenceFrames()
            let frame: CMAttitudeReferenceFrame = frames.contains(.xMagneticNorthZVertical) ? .xMagneticNorthZVertical : .xArbitraryZVertical
            motion.startDeviceMotionUpdates(using: frame, to: .main) { [weak self] m, _ in
                guard let self, let m else { return }
                self.pitchDeg = m.attitude.pitch * 180 / .pi
                self.rollDeg = m.attitude.roll * 180 / .pi
                let f = m.magneticField
                switch f.accuracy {
                case .uncalibrated: self.magStatus = 0
                case .low: self.magStatus = 1
                case .medium: self.magStatus = 2
                case .high: self.magStatus = 3
                @unknown default: self.magStatus = nil
                }
                if f.accuracy != .uncalibrated {
                    let x = f.field.x, y = f.field.y, z = f.field.z
                    self.magFieldUt = (x * x + y * y + z * z).squareRoot()
                } else {
                    self.magFieldUt = self.rawFieldUt
                }
            }
        }
        if CMAltimeter.isRelativeAltitudeAvailable() {
            altimeter.startRelativeAltitudeUpdates(to: .main) { [weak self] d, _ in
                if let d { self?.pressureHpa = d.pressure.doubleValue * 10.0 }   // kPa -> hPa
            }
        }
    }

    func stop() {
        lm.stopUpdatingLocation(); lm.stopUpdatingHeading()
        motion.stopDeviceMotionUpdates(); altimeter.stopRelativeAltitudeUpdates()
        started = false
    }

    /// Seconds since the last GPS fix, or nil.
    func gpsAgeS() -> Double? { location.map { Date().timeIntervalSince($0.timestamp) } }

    /// All of it, as telemetry fields (missing = the phone doesn't know).
    func putInto(_ o: inout [String: Any]) {
        if let l = location, l.horizontalAccuracy >= 0 {
            o["lat"] = l.coordinate.latitude; o["lon"] = l.coordinate.longitude; o["alt"] = l.altitude
            o["acc_m"] = l.horizontalAccuracy
            o["gps_age_s"] = gpsAgeS()
            o["fix_ms"] = Int64(l.timestamp.timeIntervalSince1970 * 1000)
            o["provider"] = "ios"
            if l.speed >= 0 { o["speed_mps"] = l.speed }
            if l.course >= 0 { o["course_deg"] = l.course }
            if l.verticalAccuracy > 0 { o["vacc_m"] = l.verticalAccuracy }
            if l.speedAccuracy >= 0 { o["speed_acc_mps"] = l.speedAccuracy }
            if l.courseAccuracy >= 0 { o["course_acc_deg"] = l.courseAccuracy }
            if l.sourceInformation?.isSimulatedBySoftware == true { o["mock_location"] = true }
        }
        if let v = headingTrueDeg { o["heading_deg"] = v }
        if let v = headingMagDeg { o["heading_mag_deg"] = v }
        if let v = headingAccDeg { o["heading_acc_deg"] = v }
        if let v = declinationDeg { o["declination_deg"] = v }
        if let v = pitchDeg { o["pitch_deg"] = v }
        if let v = rollDeg { o["roll_deg"] = v }
        if let v = magFieldUt { o["mag_field_ut"] = v }
        if let v = magStatus { o["mag_status"] = v }
        if let v = pressureHpa { o["pressure_hpa"] = v }
    }

    // MARK: CLLocationManagerDelegate

    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        if let l = locs.last { location = l }
    }

    func locationManager(_ m: CLLocationManager, didUpdateHeading h: CLHeading) {
        let x = h.x, y = h.y, z = h.z
        rawFieldUt = (x * x + y * y + z * z).squareRoot()
        guard h.headingAccuracy >= 0 else { return }       // negative = invalid
        headingMagDeg = h.magneticHeading
        headingAccDeg = h.headingAccuracy
        if h.trueHeading >= 0 {
            headingTrueDeg = h.trueHeading
            var d = h.trueHeading - h.magneticHeading
            if d > 180 { d -= 360 } else if d < -180 { d += 360 }
            declinationDeg = d
        } else {
            headingTrueDeg = h.magneticHeading                 // no location yet: no declination
            declinationDeg = nil
        }
    }

    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        if authorized { m.startUpdatingLocation(); if CLLocationManager.headingAvailable() { m.startUpdatingHeading() } }
    }

    func locationManagerShouldDisplayHeadingCalibration(_ m: CLLocationManager) -> Bool { true }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {}
}
