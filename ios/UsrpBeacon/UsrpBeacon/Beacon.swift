import CoreBluetooth
import Foundation

/// iPhone version of the BLE beacon.
///
/// iOS gives apps far less control than Android does:
///  - no manufacturer data, so the beacon is identified by its local name "USRP-<tag>"
///    (capture_console/ble_decode.py maps that name to the same tag as the Android payload);
///  - iOS picks the advertising interval and TX power itself (no API for either);
///  - the packets are connectable ADV_IND, sent on all three advertising channels (37/38/39);
///  - the local name is only sent while the app is IN THE FOREGROUND. In the background iOS
///    strips it and the B210 can no longer tell this phone apart, so keep the app open.
///
/// "Blink" keys the beacon on and off with a known period (stop/start advertising), exactly like
/// the Android app, so the phone can be picked out of other BLE traffic using adv_on in the telemetry.
final class Beacon: NSObject, CBPeripheralManagerDelegate {

    private var pm: CBPeripheralManager!
    private var blinkTimer: Timer?
    private var wantStart = false

    private(set) var running = false        // the user switched it on
    private(set) var radiating = false      // actually advertising right now
    private(set) var tag = 1
    private(set) var blinkPeriod: TimeInterval = 0
    private(set) var state = "Press START to begin transmitting."
    private(set) var bluetoothState: CBManagerState = .unknown

    var blinking: Bool { blinkPeriod > 0 }
    var bluetoothOn: Bool { bluetoothState == .poweredOn }
    var localName: String { "USRP-\(tag)" }

    override init() {
        super.init()
        // Creating the manager is what makes iOS ask for Bluetooth permission.
        pm = CBPeripheralManager(delegate: self, queue: nil)
    }

    func start(tag: Int, blinkPeriod: TimeInterval) {
        stop()
        self.tag = max(0, min(255, tag))
        self.blinkPeriod = blinkPeriod
        running = true
        guard bluetoothOn else {
            wantStart = true
            state = bluetoothProblem ?? "Waiting for Bluetooth…"
            return
        }
        begin()
    }

    func stop() {
        wantStart = false
        blinkTimer?.invalidate(); blinkTimer = nil
        if pm.isAdvertising { pm.stopAdvertising() }
        if running { state = "Stopped" }
        running = false; radiating = false
    }

    private func begin() {
        wantStart = false
        advertise()
        if blinkPeriod > 0 {
            let t = Timer(timeInterval: blinkPeriod, repeats: true) { [weak self] _ in self?.blinkTick() }
            RunLoop.main.add(t, forMode: .common)
            blinkTimer = t
        }
    }

    private func advertise() {
        pm.startAdvertising([CBAdvertisementDataLocalNameKey: localName])
        radiating = true      // optimistic; didStartAdvertising confirms
    }

    private func blinkTick() {
        guard running, bluetoothOn else { return }
        if radiating { pm.stopAdvertising(); radiating = false } else { advertise() }
    }

    var bluetoothProblem: String? {
        switch bluetoothState {
        case .poweredOn: return nil
        case .poweredOff: return "Bluetooth is off. Turn it on in Control Center or Settings."
        case .unauthorized: return "Bluetooth permission denied. Settings › USRP Beacon › Bluetooth."
        case .unsupported: return "This device can't advertise over Bluetooth LE."
        default: return "Waiting for Bluetooth…"
        }
    }

    // MARK: CBPeripheralManagerDelegate

    func peripheralManagerDidUpdateState(_ p: CBPeripheralManager) {
        bluetoothState = p.state
        if p.state == .poweredOn {
            if wantStart && running { begin() }
        } else if running {
            // Bluetooth went away: remember to come back when it returns.
            blinkTimer?.invalidate(); blinkTimer = nil
            radiating = false; wantStart = true
            state = bluetoothProblem ?? "Bluetooth unavailable"
        }
    }

    func peripheralManagerDidStartAdvertising(_ p: CBPeripheralManager, error: Error?) {
        if let error {
            radiating = false
            state = "Advertise failed: \(error.localizedDescription)"
        } else {
            state = "Advertising as \"\(localName)\""
        }
    }
}
