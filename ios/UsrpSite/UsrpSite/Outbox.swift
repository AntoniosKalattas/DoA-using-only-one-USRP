import Foundation
import Network

/// Every measurement is saved on the phone FIRST (Application Support/outbox.json), then sent.
/// It stays in the outbox until the laptop confirms it, so nothing is lost while offline:
/// the outbox retries every few seconds, whenever the network comes back, and at every app start.
///
/// Sent to <link>/phone/backlog as {"items":[...]} - the Capture Console's existing endpoint.
/// Each item carries sid + seq, and the console skips anything it already has, so re-sending is safe.
@MainActor
final class Outbox: ObservableObject {
    struct Item: Codable, Identifiable {
        var id: String            // = the message's "id"
        var json: String          // the message, exactly as it will be sent
        var created: Date
        var sentAt: Date?
    }

    @Published private(set) var items: [Item] = []      // newest first, sent ones kept for the history list
    @Published private(set) var status = "idle"
    @Published private(set) var online = true

    var link: () -> String = { "" }

    private let file: URL
    private let sid: String
    private var seq: Int
    private var sending = false
    private var timer: Timer?
    private let monitor = NWPathMonitor()

    var pending: [Item] { items.filter { $0.sentAt == nil } }

    init() {
        let fm = FileManager.default
        let dir = (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? fm.temporaryDirectory
        file = dir.appendingPathComponent("outbox.json")
        let d = UserDefaults.standard
        if let s = d.string(forKey: "sid") { sid = s } else {
            sid = "site-" + String(UUID().uuidString.lowercased().prefix(8)); d.set(sid, forKey: "sid")
        }
        seq = d.integer(forKey: "seq")
        if let data = try? Data(contentsOf: file), let saved = try? JSONDecoder().decode([Item].self, from: data) {
            items = saved
        }
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                guard let self else { return }
                self.online = path.status == .satisfied
                if self.online { self.flush() }
            }
        }
        monitor.start(queue: DispatchQueue(label: "net"))
        let t = Timer(timeInterval: 5, repeats: true) { [weak self] _ in Task { @MainActor in self?.flush() } }
        RunLoop.main.add(t, forMode: .common); timer = t
    }

    /// Save on the phone, then try to send.
    func add(_ msg: [String: Any]) {
        var o = msg
        seq += 1; UserDefaults.standard.set(seq, forKey: "seq")
        o["sid"] = sid; o["seq"] = seq
        o["platform"] = "ios-site"
        let id = (o["id"] as? String) ?? UUID().uuidString.lowercased()
        o["id"] = id
        let data = (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])) ?? Data("{}".utf8)
        items.insert(Item(id: id, json: String(decoding: data, as: UTF8.self), created: Date()), at: 0)
        save()
        flush()
    }

    func delete(_ id: String) { items.removeAll { $0.id == id }; save() }

    /// Send everything not yet confirmed, in one batch.
    func flush() {
        let todo = pending
        guard !sending, !todo.isEmpty else {
            if todo.isEmpty && !sending { status = items.isEmpty ? "nothing measured yet" : "all sent ✓" }
            return
        }
        guard let url = Self.endpoint(link(), path: "/phone/backlog") else {
            status = "set the laptop link to send \(todo.count) saved measurement(s)"; return
        }
        sending = true
        status = "sending \(todo.count)…"
        var req = URLRequest(url: url, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "ngrok-skip-browser-warning")
        req.httpBody = Data(("{\"items\":[" + todo.reversed().map(\.json).joined(separator: ",") + "]}").utf8)
        let ids = Set(todo.map(\.id))
        URLSession.shared.dataTask(with: req) { data, resp, err in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let ok = err == nil && code == 200 &&
                ((data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any])?["ok"] as? Bool ?? false)
            Task { @MainActor in
                self.sending = false
                if ok {
                    let now = Date()
                    for i in self.items.indices where ids.contains(self.items[i].id) { self.items[i].sentAt = now }
                    self.save()
                    self.status = "all sent ✓"
                    self.flush()                       // anything added meanwhile
                } else {
                    let why = err?.localizedDescription ?? (code == 0 ? "no answer" : "laptop answered HTTP \(code)")
                    self.status = "offline (\(why)) · \(todo.count) saved on phone, retrying"
                }
            }
        }.resume()
    }

    /// GET the link: the console answers "USRP Beacon ingest…".
    func test(done: @escaping (String) -> Void) {
        guard let url = Self.endpoint(link(), path: "/") else { done("Enter the laptop link first."); return }
        var req = URLRequest(url: url, timeoutInterval: 8)
        req.setValue("1", forHTTPHeaderField: "ngrok-skip-browser-warning")
        URLSession.shared.dataTask(with: req) { data, resp, err in
            let body = data.map { String(decoding: $0, as: UTF8.self) } ?? ""
            let msg: String
            if let err { msg = "✗ Can't reach it: \(err.localizedDescription)" }
            else if body.contains("USRP Beacon ingest") { msg = "✓ Connected to the Capture Console" }
            else { msg = "✗ Something answered (HTTP \((resp as? HTTPURLResponse)?.statusCode ?? 0)), but it isn't the console. " +
                         "On the Mac run: ngrok http 5080" }
            Task { @MainActor in done(msg); self.flush() }
        }.resume()
    }

    /// "abcd.ngrok-free.app" -> https://abcd.ngrok-free.app ; "192.168.1.5" -> http://192.168.1.5:5080
    nonisolated static func endpoint(_ raw: String, path: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        if !s.contains("://") {
            let host = s.split(separator: "/").first.map(String.init) ?? s
            let isIP = host.split(separator: ".").count == 4 && host.allSatisfy { $0.isNumber || $0 == "." || $0 == ":" }
            s = (isIP ? "http://" : "https://") + s
            if isIP && !host.contains(":") { s = s.replacingOccurrences(of: host, with: host + ":5080") }
        }
        while s.hasSuffix("/") { s.removeLast() }
        return URL(string: s + path)
    }

    private func save() {
        if let data = try? JSONEncoder().encode(items) { try? data.write(to: file, options: .atomic) }
    }
}
