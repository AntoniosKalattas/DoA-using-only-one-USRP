import Foundation
import Network

struct LinkError: LocalizedError {
    let msg: String
    init(_ m: String) { msg = m }
    var errorDescription: String? { msg }
}

/// Sends one JSON message per tick to the laptop (port of UdpTelemetry.kt). Transport is picked
/// from the "Laptop IP" field:
///  - plain IP / hostname -> UDP datagram to host:port (same Wi-Fi, fastest)
///  - http(s)://… link    -> HTTP POST to <link>/phone (ngrok tunnel to the console's port 5080)
/// Every message is first saved on the phone (TelemetryStore); anything the laptop missed is
/// uploaded later to <link>/phone/backlog (or http://<ip>:5080/phone/backlog for UDP).
final class Telemetry {
    static let backlogPort = 5080
    private static let uploadEvery: TimeInterval = 3
    private static let retryAfterFail: TimeInterval = 10

    let store: TelemetryStore
    private let q = DispatchQueue(label: "telemetry")
    private var timer: Timer?
    private var udp: NWConnection?
    private var udpKey = ""
    private var nextUpload = Date.distantPast
    private var inFlight = 0                       // main-thread only

    private(set) var sent = 0
    private(set) var lastError: String?
    private(set) var backlogError: String?

    var pending: Int { store.pending }
    var lastUpload: String? { store.lastUpload }

    init(store: TelemetryStore) { self.store = store }

    static func isHttp(_ h: String) -> Bool { h.hasPrefix("http://") || h.hasPrefix("https://") }

    private func backlogUrl(_ host: String) -> String {
        Self.isHttp(host) ? trimSlash(host) + "/phone/backlog" : "http://\(host):\(Self.backlogPort)/phone/backlog"
    }

    /// Start streaming. `build` runs on the main thread each tick and returns the message.
    func start(host: String, port: Int, rateHz: Double, build: @escaping () -> [String: Any]) {
        stop()
        // HTTP through a tunnel has ~100-300 ms round trips; 4 Hz keeps it from backing up.
        let hz = Self.isHttp(host) ? min(rateHz, 4.0) : rateHz
        nextUpload = .distantPast
        let t = Timer(timeInterval: max(0.02, 1.0 / hz), repeats: true) { [weak self] _ in
            guard let self, self.inFlight < 2 else { return }
            let msg = build()
            self.inFlight += 1
            self.q.async {
                do {
                    try self.sendOne(host, port, msg)
                    self.sent += 1; self.lastError = nil
                } catch {
                    self.lastError = error.localizedDescription
                }
                // Over HTTP: only when the live link works again. Over UDP there's no answer, so try anyway.
                if self.lastError == nil { self.drainBacklog(host) }
                DispatchQueue.main.async { self.inFlight -= 1 }
            }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        t.fire()
    }

    func stop() {
        timer?.invalidate(); timer = nil
        q.async { self.store.flush() }
    }

    /// Fire one extra message right now (MARK events).
    func sendNow(host: String, port: Int, _ msg: [String: Any]) {
        q.async {
            do { try self.sendOne(host, port, msg); self.sent += 1 } catch { self.lastError = error.localizedDescription }
        }
    }

    /// One message that matters (USRP position). HTTP: the laptop's answer is reported. UDP has no
    /// answer, so it is sent 3 times (the console ignores repeats by id). Saved on the phone first.
    /// onDone(nil) = delivered, onDone(text) = what went wrong. Called on the main thread.
    func sendJson(host: String, port: Int, _ json: [String: Any], onDone: @escaping (String?) -> Void) {
        q.async {
            var err: String?
            do {
                let (line, idx) = self.store.append(json)
                let body = Data((line + "\n").utf8)
                if Self.isHttp(host) {
                    try self.sendBody(host, port, body); self.store.confirmLive(idx)
                } else {
                    for _ in 0..<3 { try self.sendBody(host, port, body); Thread.sleep(forTimeInterval: 0.15) }
                }
            } catch { err = error.localizedDescription }
            DispatchQueue.main.async { onDone(err) }
        }
    }

    // MARK: worker-queue only

    private func sendOne(_ host: String, _ port: Int, _ msg: [String: Any]) throws {
        let (line, idx) = store.append(msg)
        try sendBody(host, port, Data((line + "\n").utf8))
        if Self.isHttp(host) { store.confirmLive(idx) }      // HTTP 200 = the laptop has it
    }

    private func drainBacklog(_ host: String) {
        let now = Date()
        if now < nextUpload || store.pending == 0 { return }
        do {
            var batches = 0
            while batches < 5, try store.uploadOne({ try Self.postHttp(backlogUrl(host), Data($0.utf8), timeout: 20) }) { batches += 1 }
            backlogError = nil
            nextUpload = now.addingTimeInterval(Self.uploadEvery)
        } catch {
            backlogError = error.localizedDescription
            nextUpload = now.addingTimeInterval(Self.retryAfterFail)
        }
    }

    private func sendBody(_ host: String, _ port: Int, _ body: Data) throws {
        if Self.isHttp(host) {
            try Self.postHttp(trimSlash(host) + "/phone", body, timeout: 4)
        } else {
            try sendUdp(host, port, body)
        }
    }

    private func sendUdp(_ host: String, _ port: Int, _ body: Data) throws {
        guard let p = NWEndpoint.Port(rawValue: UInt16(clamping: port)), !host.isEmpty else { throw LinkError("bad address \(host):\(port)") }
        let key = "\(host):\(port)"
        if udp == nil || udpKey != key || udp?.state == .cancelled {
            udp?.cancel()
            let c = NWConnection(host: NWEndpoint.Host(host), port: p, using: .udp)
            c.start(queue: q)
            udp = c; udpKey = key
        }
        if case .failed(let e) = udp!.state { udp?.cancel(); udp = nil; throw LinkError("UDP: \(e.localizedDescription)") }
        if case .waiting(let e) = udp!.state { throw LinkError("UDP waiting: \(e.localizedDescription)") }
        udp!.send(content: body, completion: .contentProcessed { [weak self] e in
            if let e { self?.lastError = "UDP: \(e.localizedDescription)" }
        })
    }

    private final class Box: @unchecked Sendable { var code = -1; var err: Error? }

    /// Blocking POST (worker queue only). Throws on network error or non-2xx.
    static func postHttp(_ link: String, _ body: Data, timeout: TimeInterval) throws {
        guard let url = URL(string: link) else { throw LinkError("bad link \(link)") }
        var r = URLRequest(url: url, timeoutInterval: timeout)
        r.httpMethod = "POST"; r.httpBody = body
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue("1", forHTTPHeaderField: "ngrok-skip-browser-warning")
        let box = Box()
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: r) { _, resp, e in
            box.err = e; box.code = (resp as? HTTPURLResponse)?.statusCode ?? -1; sem.signal()
        }.resume()
        sem.wait()
        if let e = box.err { throw e }
        if !(200...299).contains(box.code) { throw LinkError("HTTP \(box.code) from \(url.host ?? link)") }
    }
}

func trimSlash(_ s: String) -> String {
    var t = s
    while t.hasSuffix("/") { t.removeLast() }
    return t
}
