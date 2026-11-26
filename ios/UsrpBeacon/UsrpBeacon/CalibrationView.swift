import SwiftUI

struct CalibrationView: View {
    @StateObject private var c: CalibrationModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var phase

    init(app: AppModel) { _c = StateObject(wrappedValue: CalibrationModel(app: app)) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Button { dismiss() } label: { Text("‹ Back").font(.system(size: 16)).foregroundColor(Theme.accent) }
                    .padding(.vertical, 4)
                Text("Calibrate the USRP").font(.system(size: 26, weight: .semibold)).foregroundColor(Theme.textMain)
                Note("This phone becomes the known transmitter. Put it where the steps say and tap Measure. " +
                     "The Mac does the math and saves the calibration for the radar.", color: Theme.textSub, size: 13)
                connection
                settings
                methodCard
                switch c.method {
                case "ar": arCard
                case "sweep": sweepCard
                default:
                    manual1
                    manual2
                }
                saveCard
                if !c.measuring.isEmpty {
                    BigButton(label: "CANCEL MEASUREMENT", color: Theme.red) { c.command("cancel") }
                }
            }
            .padding(EdgeInsets(top: 16, leading: 16, bottom: 40, trailing: 16))
        }
        .background(Theme.bg.ignoresSafeArea())
        .scrollDismissesKeyboard(.interactively)
        .toolbar(.hidden, for: .navigationBar)
        .onAppear { c.show() }
        .onDisappear { c.hide() }
        .onChange(of: phase) { p in if p != .active { c.stopAr() } }
    }

    private var connection: some View {
        Card(title: "Connection") {
            Text(c.linkText).font(.system(size: 14, weight: .semibold))
                .foregroundColor(c.linkOk == true ? Theme.green : (c.linkOk == false ? Theme.red : Theme.textSub))
                .fixedSize(horizontal: false, vertical: true).padding(.top, 8)
            if c.connected {
                let (u, col) = c.usbStatus
                Note(u, color: col, size: 13).padding(.top, 6)
            }
            if let b = c.busyText { Note(b, color: Theme.amber, size: 13).padding(.top, 6) }
            if let e = c.errorText { Note("⚠ " + e, color: Theme.red, size: 13).padding(.top, 8) }
        }
    }

    private var settings: some View {
        Card(title: "Settings", subtitle: "Usually fine as they are. They're filled in from your current calibration.") {
            Setting(label: "Calibration name", note: "What it's saved as on the Mac. Pick it later on the radar page.") {
                InputField(text: $c.name)
            }
            Setting(label: "Bluetooth channel to listen on",
                    note: "The USRP listens to one of the 3 Bluetooth advertising channels. Channel 39 sits above most Wi-Fi, so it's the cleanest. " +
                          "Live tracking must then use the same channel.") {
                Picker2(items: ["Channel 39 · 2.480 GHz (recommended)", "Channel 38 · 2.426 GHz", "Channel 37 · 2.402 GHz"], selection: $c.channelIdx)
            }
            Setting(label: "Antenna spacing (cm)",
                    note: "Distance between the centres of the two antennas. Measure it with a ruler. 6 cm or less avoids mirror images at 2.4 GHz.") {
                InputField(text: $c.spacingText, keyboard: .decimalPad)
            }
            Setting(label: "Measure time (seconds)", note: "Longer = steadier result. 10 s is plenty.") {
                InputField(text: $c.secondsText, keyboard: .decimalPad)
            }
            Setting(label: "Which antenna is on your left?",
                    note: "Stand behind the USRP looking out toward the phone. Only used to label the radar's left/right sides.") {
                Picker2(items: ["RF A is on my left", "RF B is on my left"], selection: $c.leftIdx)
            }
        }
    }

    private var methodCard: some View {
        Card(title: "How to calibrate", subtitle: "Leave it on Automatic: the app uses AR if this phone can, otherwise the sweep.") {
            Picker2(items: ["Automatic (the app chooses)", "AR walk · most accurate", "Sweep · one arc", "Manual · two positions"],
                    selection: $c.methodIdx).padding(.top, 12)
            let (t, col) = c.methodNote
            Note(t, color: col, size: 13).padding(.top, 8)
        }
    }

    private var arCard: some View {
        let running = c.ar.running
        let (st, stc) = c.arStatus
        let w = c.walk
        return Card(title: "AR walk", subtitle: "The phone works out where it is from the camera. You only show it where the antennas are, then walk.") {
            Note("1. Tap START AR and move the phone a little until it says Tracking.\n" +
                 "2. Hold the phone right between the two antennas and tap SET ORIGIN.\n" +
                 "3. Tap START WALK, step back and walk slowly for 20–30 s: a wide arc from one side of the antennas to the other, " +
                 "1–3 m away, also changing distance. Stay in front of the antennas and keep the camera uncovered.\n4. Tap DONE WALKING.",
                 color: Theme.textSub, size: 13).padding(.top, 10)
            ZStack {
                RoundedRectangle(cornerRadius: 10).fill(Theme.field)
                if c.arVisible { ArPreview(tracker: c.ar).clipShape(RoundedRectangle(cornerRadius: 10)) }
            }.frame(height: 220).padding(.top, 12)
            Text(st).font(.system(size: 13, weight: .semibold)).foregroundColor(stc)
                .fixedSize(horizontal: false, vertical: true).padding(.top, 8)
            BigButton(label: running ? "AR IS RUNNING" : "START AR (uses the camera)", color: Theme.grey, enabled: !running) {
                c.arStartPressed()
            }.padding(.top, 12)
            BigButton(label: c.origin == nil ? "SET ORIGIN · phone is between the antennas" : "SET ORIGIN AGAIN", color: Theme.grey,
                      enabled: running && c.ar.tracking && !c.arBusy) { c.setOrigin() }.padding(.top, 10)
            BigButton(label: w.button, color: Theme.accent, enabled: w.enabled) { c.walkPressed() }.padding(.top, 10)
            Note(w.text, color: w.color, size: 13).padding(.top, 8)
        }
    }

    private var sweepCard: some View {
        let sw = c.sweep
        return Card(title: "Sweep", subtitle: "One slow walk around the front of the antennas, from the left end to the right end.") {
            Diagram(text: "            ·  ·  📱 ·  ·\n        ·                  ·\n      ·        ~1 m          ·\n" +
                          "  START                      END\n   📱 ──── [A]──────[B] ──── 📱\n   left        🧍 you        right")
                .padding(.top, 10)
            Note("Stand about 1 m to the LEFT of the antennas, exactly in line with the two of them (left as seen from behind the antennas). " +
                 "Tap START SWEEP and wait for the radio. Tap GO, walk a half circle around the front at arm's length from your body, keeping " +
                 "about 1 m from the antennas, and stop exactly in line with them on the RIGHT. Take 10–15 s, don't go back, then tap DONE.",
                 color: Theme.textSub, size: 13).padding(.top, 10)
            BigButton(label: sw.button, color: Theme.accent, enabled: sw.enabled) { c.sweepPressed() }.padding(.top, 14)
            Note(sw.text, color: sw.color, size: 13).padding(.top, 8)
        }
    }

    private var manual1: some View {
        let (t, col) = c.frontText
        return Card(title: "Step 1 · Phone straight in front") {
            Diagram(text: "            📱   phone, screen up\n            │\n            │  1 m\n            │\n" +
                          "     [A]────┴────[B]   antennas\n            🧍  you stand behind").padding(.top, 10)
            Note("Put the phone 1 m straight out from the middle of the two antennas, at about the same height. Keep it still and don't stand " +
                 "between the phone and the antennas. The beacon is switched to non-stop for this.", color: Theme.textSub, size: 13).padding(.top, 10)
            BigButton(label: c.front != nil ? "MEASURE FRONT AGAIN" : "MEASURE FRONT", color: Theme.accent,
                      enabled: c.connected && c.measuring.isEmpty) { c.measureFront() }.padding(.top, 14)
            ProgressView(value: c.frontProgress).tint(Theme.accent).padding(.top, 10)
            Note(t, color: col, size: 13).padding(.top, 6)
        }
    }

    private var manual2: some View {
        let (t, col) = c.sideText
        let canMeasure = c.connected && c.measuring.isEmpty && c.front != nil
        return Card(title: "Step 2 · Phone to the right") {
            Diagram(text: "                    📱  phone, ~45° right\n                  ╱\n                ╱  ~1 m\n              ╱\n" +
                          "     [A]────●────[B]   antennas\n            🧍  you stand behind").padding(.top, 10)
            Note("Move the phone about 45° to YOUR right (seen from behind the antennas), still about 1 m away. This checks the radar doesn't " +
                 "show left and right swapped.", color: Theme.textSub, size: 13).padding(.top, 10)
            HStack(spacing: 10) {
                BigButton(label: "MEASURE RIGHT SIDE", color: Theme.accent, enabled: canMeasure) { c.measureSide() }.layoutPriority(2)
                BigButton(label: "SKIP", color: Theme.grey, enabled: canMeasure && c.side == nil) { c.command("skip_side") }.frame(width: 100)
            }.padding(.top, 14)
            ProgressView(value: c.sideProgress).tint(Theme.accent).padding(.top, 10)
            Note(t, color: col, size: 13).padding(.top, 6)
        }
    }

    private var saveCard: some View {
        Card(title: "Save", subtitle: "Saves the calibration on the Mac and makes it the active one.") {
            Text(c.summary).font(.system(size: 12, design: .monospaced)).foregroundColor(Theme.textMain)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.field)).padding(.top, 10)
            BigButton(label: "SAVE & USE THIS CALIBRATION", color: Theme.green, enabled: c.canSave) { c.command("save") }.padding(.top, 14)
            BigButton(label: "START LIVE TRACKING", color: Theme.accent, enabled: c.canLive) { c.command("live") }.padding(.top, 10)
            Note("After saving you can put the phone anywhere and use the main screen's MARK button to check how accurate the radar is.")
                .padding(.top, 8)
        }
    }
}
