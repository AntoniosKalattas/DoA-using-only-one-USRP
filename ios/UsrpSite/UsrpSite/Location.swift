import CoreLocation
import Foundation

/// Live GPS for the site measurement.
@MainActor
final class Location: NSObject, ObservableObject, CLLocationManagerDelegate {
    @Published private(set) var last: CLLocation?
    @Published private(set) var auth: CLAuthorizationStatus = .notDetermined
    @Published private(set) var precise = true

    private let mgr = CLLocationManager()

    override init() {
        super.init()
        mgr.delegate = self
        mgr.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        mgr.distanceFilter = kCLDistanceFilterNone
        mgr.activityType = .other
        auth = mgr.authorizationStatus
    }

    func start() {
        if mgr.authorizationStatus == .notDetermined { mgr.requestWhenInUseAuthorization() }
        mgr.startUpdatingLocation()
    }

    /// Seconds since the last fix (nil = no fix yet).
    var ageS: Double? { last.map { -$0.timestamp.timeIntervalSinceNow } }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let l = locations.last else { return }
        Task { @MainActor in self.last = l }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let s = manager.authorizationStatus
        let p = manager.accuracyAuthorization == .fullAccuracy
        Task { @MainActor in
            self.auth = s; self.precise = p
            if s != .notDetermined && s != .denied && s != .restricted { self.mgr.startUpdatingLocation() }
        }
    }
}
