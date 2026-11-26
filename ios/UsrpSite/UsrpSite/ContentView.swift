import CoreLocation
import SwiftUI

@main
struct UsrpSiteApp: App {
    @StateObject private var model = SiteModel()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            ContentView(model: model, gps: model.gps, outbox: model.outbox)
                .onAppear { model.start() }
                .onChange(of: phase) { p in if p == .active { model.outbox.flush() } }
        }
    }
}

struct ContentView: View {
    @ObservedObject var model: SiteModel
    @ObservedObject var gps: Location
    @ObservedObject var outbox: Outbox

    var body: some View {
        NavigationStack {
            Form {
                Section("Laptop link") {
                    TextField("https://xxxx.ngrok-free.app", text: $model.link)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                    HStack {
                        Button("Test link") { model.test() }
                        Spacer()
                        Text(model.testResult).font(.footnote).foregroundStyle(.secondary)
                    }
                    Text("Copy it from the radar page (📱 phone link) on the Mac.").font(.footnote).foregroundStyle(.secondary)
                }

                Section("GPS") {
                    Text(gpsLine).font(.system(.body, design: .monospaced))
                    if gps.auth == .denied || gps.auth == .restricted {
                        Text("Location is off for this app: Settings › USRP Site › Location › While Using.").foregroundStyle(.red)
                    } else if !gps.precise {
                        Text("Precise Location is off: Settings › USRP Site › Location › turn on Precise Location.").foregroundStyle(.red)
                    }
                }

                Section("Measure the USRP position") {
                    Text("1. Go outside, open sky.\n2. Lay the phone flat ON TOP of the USRP.\n3. Wait until GPS says under 10 m.\n4. Press Measure and don't touch it.")
                        .font(.callout)
                    Stepper("Average for \(model.seconds) s", value: $model.seconds, in: 15...180, step: 15)
                        .disabled(model.measuring)
                    if model.measuring {
                        ProgressView().frame(maxWidth: .infinity)
                        Text(model.progress).font(.footnote)
                        Button("Cancel", role: .destructive) { model.cancel() }
                    } else {
                        Button { model.measure() } label: {
                            Text("Measure").font(.headline).frame(maxWidth: .infinity)
                        }.buttonStyle(.borderedProminent)
                    }
                    if !model.result.isEmpty { Text(model.result).font(.footnote) }
                }

                Section {
                    Text(outbox.status).font(.footnote)
                    ForEach(outbox.items) { it in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(summary(it)).font(.system(.footnote, design: .monospaced))
                            Text((it.sentAt != nil ? "✓ sent " : "⏳ waiting · saved on phone · ") +
                                 it.created.formatted(date: .abbreviated, time: .standard))
                                .font(.caption).foregroundStyle(it.sentAt != nil ? .green : .orange)
                        }
                        .swipeActions { Button("Delete", role: .destructive) { outbox.delete(it.id) } }
                    }
                } header: {
                    Text("Measurements (\(outbox.pending.count) waiting to send)")
                } footer: {
                    Text("Every measurement is saved on the phone first. If the laptop can't be reached it waits here and is sent automatically when it's back (keep the app open, or just open it again later).")
                }
            }
            .navigationTitle("USRP Site")
        }
    }

    private var gpsLine: String {
        guard let l = gps.last else { return "waiting for GPS…" }
        let age = gps.ageS ?? 99
        let a = l.horizontalAccuracy
        let tag = a <= 10 ? "good" : (a <= 20 ? "ok" : "too rough")
        return String(format: "± %.1f m (%@)%@\n%.6f, %.6f", a, tag, age > 3 ? String(format: " · %.0f s old", age) : "",
                      l.coordinate.latitude, l.coordinate.longitude)
    }

    private func summary(_ it: Outbox.Item) -> String {
        guard let o = try? JSONSerialization.jsonObject(with: Data(it.json.utf8)) as? [String: Any],
              let la = o["lat"] as? Double, let lo = o["lon"] as? Double else { return it.id }
        return String(format: "%.7f, %.7f ± %.1f m", la, lo, (o["acc_m"] as? Double) ?? 0)
    }
}
