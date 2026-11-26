import Foundation

/// Every message the app sends to the laptop is first written here, on the phone
/// (Application Support/telemetry/<start>_<sid>.jsonl, one JSON per line). Port of TelemetryStore.kt:
/// whatever the laptop has not confirmed is uploaded later, in batches, to <laptop>/phone/backlog.
/// The laptop skips anything it already has (by sid + seq).
///
/// Not thread-safe on purpose: Telemetry calls it only from its serial queue.
/// `pending` / `lastUpload` are read by the UI.
final class TelemetryStore {
    static let batch = 300
    private static let keepDays = 30.0

    private let dir: URL
    let sid: String = String(UUID().uuidString.lowercased().prefix(8))
    private let file: URL

    private var offsets: [UInt64] = []
    private var size: UInt64 = 0
    private var seq: Int64 = 0
    private var acked = 0
    private var ahead = Set<Int>()
    private var lastSave = Date.distantPast
    private var handle: FileHandle?

    private final class Old { let f: URL; var acked: Int; var offset: UInt64; let total: Int
        init(f: URL, acked: Int, offset: UInt64, total: Int) { self.f = f; self.acked = acked; self.offset = offset; self.total = total } }
    private var old: [Old] = []

    private(set) var pending: Int = 0
    private(set) var stored: Int = 0
    private(set) var lastUpload: String?

    init() {
        let fm = FileManager.default
        let base = (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? fm.temporaryDirectory
        dir = base.appendingPathComponent("telemetry", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let df = DateFormatter(); df.locale = Locale(identifier: "en_US_POSIX"); df.dateFormat = "yyyyMMdd-HHmmss"
        file = dir.appendingPathComponent(df.string(from: Date()) + "_" + sid + ".jsonl")

        let cutoff = Date().addingTimeInterval(-Self.keepDays * 86_400)
        let files = ((try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [])
            .filter { $0.pathExtension == "jsonl" && $0 != file }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        for f in files {
            let total = Self.countLines(f)
            let (a, off) = readCursor(f)
            if a < total {
                old.append(Old(f: f, acked: a, offset: off, total: total))
            } else if let m = try? f.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, m < cutoff {
                try? fm.removeItem(at: f); try? fm.removeItem(at: cursorFile(f))
            }
        }
        recount()
    }

    /// Stamp the message with sid/seq, write it to the phone, return (stamped json line, line index).
    func append(_ json: [String: Any]) -> (String, Int) {
        var o = json
        seq += 1
        o["sid"] = sid; o["seq"] = seq
        let data = (try? JSONSerialization.data(withJSONObject: o, options: [])) ?? Data("{}".utf8)
        let line = String(decoding: data, as: UTF8.self)
        let bytes = Data((line + "\n").utf8)
        write(bytes)
        offsets.append(size); size += UInt64(bytes.count)
        stored = offsets.count; recount()
        return (line, offsets.count - 1)
    }

    private func write(_ d: Data) {
        if handle == nil {
            if !FileManager.default.fileExists(atPath: file.path) { FileManager.default.createFile(atPath: file.path, contents: nil) }
            handle = try? FileHandle(forWritingTo: file)
            _ = try? handle?.seekToEnd()
        }
        try? handle?.write(contentsOf: d)
    }

    /// The laptop answered this live send.
    func confirmLive(_ index: Int) {
        if index < acked { return }
        ahead.insert(index)
        while ahead.remove(acked) != nil { acked += 1 }
        saveCursor(force: false); recount()
    }

    /// Upload one batch of unconfirmed lines (older sessions first). `post` throws on failure.
    /// Returns true when something was uploaded (call again for the next batch).
    func uploadOne(_ post: (String) throws -> Void) rethrows -> Bool {
        if let o = old.first {
            let (lines, newOff) = Self.readLines(o.f, from: o.offset, max: Self.batch)
            if lines.isEmpty { old.removeFirst(); recount(); return true }
            try post(body(lines))
            o.acked += lines.count; o.offset = newOff
            writeCursor(o.f, lines: o.acked, offset: o.offset)
            if o.acked >= o.total { old.removeFirst() }
            lastUpload = "\(lines.count) older updates uploaded"
            recount(); return true
        }
        while acked < offsets.count && ahead.remove(acked) != nil { acked += 1 }
        if acked >= offsets.count { return false }
        var end = acked
        while end < offsets.count && end - acked < Self.batch && !ahead.contains(end) { end += 1 }
        let from = offsets[acked]
        let to = end < offsets.count ? offsets[end] : size
        try? handle?.synchronize()
        var text = ""
        if let r = try? FileHandle(forReadingFrom: file) {
            defer { try? r.close() }
            try? r.seek(toOffset: from)
            if let d = try? r.read(upToCount: Int(to - from)) { text = String(decoding: d, as: UTF8.self) }
        }
        let lines = text.split(separator: "\n").map(String.init).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        try post(body(lines))
        acked = end
        while ahead.remove(acked) != nil { acked += 1 }
        saveCursor(force: true)
        lastUpload = "\(lines.count) stored updates uploaded"
        recount(); return true
    }

    func flush() { try? handle?.synchronize(); saveCursor(force: true) }

    private func body(_ lines: [String]) -> String { "{\"items\":[" + lines.joined(separator: ",") + "]}" }

    private func recount() {
        pending = (offsets.count - acked) - ahead.count + old.reduce(0) { $0 + ($1.total - $1.acked) }
    }

    private func saveCursor(force: Bool) {
        if !force && Date().timeIntervalSince(lastSave) < 2 { return }
        lastSave = Date()
        writeCursor(file, lines: acked, offset: acked < offsets.count ? offsets[acked] : size)
    }

    private func cursorFile(_ f: URL) -> URL { URL(fileURLWithPath: f.path + ".sent") }

    private func writeCursor(_ f: URL, lines: Int, offset: UInt64) {
        try? "\(lines) \(offset)".write(to: cursorFile(f), atomically: true, encoding: .utf8)
    }

    private func readCursor(_ f: URL) -> (Int, UInt64) {
        guard let s = try? String(contentsOf: cursorFile(f), encoding: .utf8) else { return (0, 0) }
        let p = s.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ")
        guard p.count == 2, let a = Int(p[0]), let o = UInt64(p[1]) else { return (0, 0) }
        return (a, o)
    }

    private static func countLines(_ f: URL) -> Int {
        guard let d = try? Data(contentsOf: f) else { return 0 }
        return d.reduce(0) { $1 == 0x0A ? $0 + 1 : $0 }
    }

    /// Up to `max` lines starting at byte `from`; returns them and the byte offset after the last one.
    private static func readLines(_ f: URL, from: UInt64, max: Int) -> ([String], UInt64) {
        guard let r = try? FileHandle(forReadingFrom: f) else { return ([], from) }
        defer { try? r.close() }
        try? r.seek(toOffset: from)
        guard let d = try? r.readToEnd(), !d.isEmpty else { return ([], from) }
        var out: [String] = []
        var start = d.startIndex
        var pos = from
        var i = d.startIndex
        while i < d.endIndex && out.count < max {
            if d[i] == 0x0A {
                let line = String(decoding: d[start..<i], as: UTF8.self)
                pos = from + UInt64(i - d.startIndex + 1)
                if !line.trimmingCharacters(in: .whitespaces).isEmpty { out.append(line) }
                start = d.index(after: i)
            }
            i = d.index(after: i)
        }
        return (out, pos)
    }
}
