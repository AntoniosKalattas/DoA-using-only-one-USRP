import SwiftUI

struct ContentView: View {
    @EnvironmentObject var m: AppModel
    @Environment(\.scenePhase) private var phase
    @State private var showCal = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header
                    status
                    BigButton(label: "CALIBRATE THE USRP WITH THIS PHONE  ›", color: Theme.slate) { showCal = true }
                    beaconCard
                    laptopCard
                    markCard
                    siteCard
                    liveCard
                }
                .padding(EdgeInsets(top: 20, leading: 16, bottom: 40, trailing: 16))
            }
            .background(Theme.bg.ignoresSafeArea())
            .scrollDismissesKeyboard(.interactively)
            .navigationDestination(isPresented: $showCal) { CalibrationView(app: m) }
            .toolbar(.hidden, for: .navigationBar)
        }
        .overlay(alignment: .bottom) { toastView }
        .preferredColorScheme(.dark)
        .onAppear { m.appeared() }
        .onChange(of: phase) { p in if p == .active { m.appeared() } }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("USRP Beacon").font(.system(size: 26, weight: .semibold)).foregroundColor(Theme.textMain)
            Note("Turns this iPhone into a known Bluetooth transmitter so you can test and calibrate the USRP B210 detector.",
                 color: Theme.textSub, size: 13)
        }
    }

    private var status: some View {
        let (title, color) = m.statusTitle
        return HStack(spacing: 14) {
            Circle().fill(color).frame(width: 18, height: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 22, weight: .semibold)).foregroundColor(Theme.textMain).lineLimit(1)
                Text(m.statusSub).font(.system(size: 13)).foregroundColor(Theme.textSub).lineLimit(2)
                    .frame(height: 36, alignment: .topLeading)
            }
            Spacer(minLength: 0)
        }
        .padding(18)
        .background(RoundedRectangle(cornerRadius: 16).fill(Theme.card))
        .padding(.top, 4)
    }

    private var beaconCard: some View {
        Card(title: "1 · Bluetooth beacon",
             subtitle: "What the phone transmits: a Bluetooth advertisement named \"USRP-<tag>\" on channels 37, 38 and 39. " +
                       "Keep this app open on screen: iOS hides the name when the app is in the background.") {
            Setting(label: "Tag number (0–255)",
                    note: "Sent as the name USRP-<tag>, so you can tell phones apart if you use more than one. The console tracks it as PHONE-<tag>.") {
                InputField(text: $m.tagText, keyboard: .numberPad)
            }
            Setting(label: "Blink mode",
                    note: "Switches the signal on and off in a steady rhythm. Only your phone turns on and off on this beat, so it's easy to spot on the USRP.") {
                Toggle("Blink on / off", isOn: $m.blinkOn).tint(Theme.accent).foregroundColor(Theme.textMain)
            }
            Setting(label: "Blink time (seconds)", note: "How long each on and off part lasts. 2 means on for 2 s, then off for 2 s, and repeat.") {
                InputField(text: $m.blinkSecondsText, keyboard: .decimalPad)
            }
            Setting(label: "Flashlight while transmitting",
                    note: m.torch.available
                        ? "The flashlight is on while the phone transmits and off during the blink's quiet part. It follows the app's timing, not each packet."
                        : "This device has no flashlight.") {
                Toggle("Flashlight", isOn: $m.torchOn).tint(Theme.accent).foregroundColor(Theme.textMain).disabled(!m.torch.available)
            }
            Note("iOS chooses the transmit interval (roughly 20–100 ms in the foreground) and the power itself. Apps can't set them like on Android.")
                .padding(.top, 18)
            BigButton(label: m.beacon.running ? "STOP BEACON" : "START BEACON", color: m.beacon.running ? Theme.red : Theme.green) {
                m.toggleBeacon()
            }.padding(.top, 20)
        }
    }

    private var laptopCard: some View {
        Card(title: "2 · Send info to laptop",
             subtitle: "Sends the phone's location, compass direction and on/off state so the laptop can match it against what the USRP recorded.") {
            Setting(label: "Laptop IP address or link",
                    note: "Same Wi-Fi: type the Mac's IP (run 'ipconfig getifaddr en0' on the Mac). If the Wi-Fi blocks that, paste the https:// ngrok link " +
                          "instead and the info goes through the internet. Works on mobile data too.") {
                InputField(text: $m.hostText, keyboard: .URL)
            }
            Setting(label: "Port", note: "Has to match the laptop listener. Leave it at 5005. It's ignored when you use a link.") {
                InputField(text: $m.portText, keyboard: .numberPad)
            }
            Setting(label: "Streaming", note: "Turn this on while testing. Sends 5 small updates per second, so it barely uses any data.") {
                Toggle("Send info to laptop", isOn: $m.streaming).tint(Theme.accent).foregroundColor(Theme.textMain)
            }
        }
    }

    private var markCard: some View {
        Card(title: "3 · Calibration point",
             subtitle: "Put the phone at a measured spot, enter where it is, then tap MARK. The laptop then knows the exact right answer and can " +
                       "check or correct the USRP's direction estimate.") {
            Setting(label: "Angle from antennas (°)",
                    note: "Direction of the phone as seen from the USRP antennas. 0 = straight in front. Use the same left/right sign (+ or −) as your MUSIC output.") {
                InputField(text: $m.bearingText, keyboard: .numbersAndPunctuation)
            }
            Setting(label: "Distance (m)", note: "Distance from the antennas to the phone in meters. Measure it with a tape, because GPS is too rough at short range.") {
                InputField(text: $m.rangeText, keyboard: .decimalPad)
            }
            BigButton(label: "MARK · phone is here now", color: Theme.accent) { m.mark() }.padding(.top, 20)
            Note("Tap MARK again each time you move the phone to a new spot.").padding(.top, 8)
        }
    }

    private var siteCard: some View {
        let busy = m.site.busy != nil
        return Card(title: "4 · USRP position",
                    subtitle: "Tell the laptop exactly where the USRP is and which way its antennas face, so every direction the USRP measures can be " +
                              "checked against where GPS says this phone was (doa_compare.py). Do it outdoors. 'Send info to laptop' must have an address.") {
            Note("Step 1 · Lay the phone flat on top of the USRP, then tap. Don't touch it for \(SiteRecorder.posSeconds) s while GPS fixes are averaged.",
                 color: Theme.textMain, size: 13).padding(.top, 14)
            BigButton(label: "RECORD POSITION · phone is on the USRP", color: Theme.accent, enabled: !busy) { m.recordSitePosition() }.padding(.top, 10)
            Readout(name: "Saved", value: m.site.positionText, note: "Latitude, longitude and how far off it may be.")
            Note("Step 2 · Hold the phone flat, about 30 cm in front of the USRP (away from the laptop and metal), with its TOP edge pointing straight " +
                 "out the front of the antennas, the radar's 0°. Tap and hold still for \(SiteRecorder.faceSeconds) s.",
                 color: Theme.textMain, size: 13).padding(.top, 18)
            BigButton(label: "RECORD FACING · top edge = antenna front", color: Theme.accent, enabled: !busy) { m.recordSiteFacing() }.padding(.top, 10)
            Readout(name: "Saved", value: m.site.facingText,
                    note: "Compass direction the antennas face (0° = North, 90° = East). If it is a little off, doa_compare.py measures the real value from the walk.")
            if busy {
                Note(m.site.progress, color: Theme.textSub, size: 13).padding(.top, 14)
            } else if !m.siteStatus.isEmpty {
                Note(m.siteStatus, color: m.siteStatusOk ? Theme.green : Theme.amber, size: 13).padding(.top, 14)
            }
            Note("Then start live tracking on the laptop, walk around 15–60 m away with the beacon on, and run: python3 capture_console/doa_compare.py")
                .padding(.top, 10)
        }
    }

    private var liveCard: some View {
        Card(title: "5 · Live readings", subtitle: "What the phone knows right now. Updates 4 times a second.") {
            Readout(name: "GPS position", value: m.gpsText, note: "Where satellites say the phone is. Usually doesn't work well indoors.")
            Readout(name: "GPS accuracy", value: m.accText, note: "How far off the GPS position could be. Under 5 m is good.")
            Readout(name: "Facing", value: m.headingText, note: "Direction the top of the phone points. 0° = North, 90° = East.")
            Readout(name: "Tilt", value: m.tiltText, note: "0° / 0° = lying flat. The compass is only trustworthy when flat.")
            Readout(name: "Magnetic field", value: m.magText, note: "Earth's field is 25–65 µT. Outside that, metal or electronics nearby are bending the compass.")
            Readout(name: "Air pressure", value: m.pressureText, note: "From the barometer.")
            Readout(name: "Sent to laptop", value: m.packetsText, note: "Updates sent so far. If this keeps going up, the link is working.")
            Readout(name: "Marks", value: m.marksText, note: "How many calibration points you've sent.")
        }
    }

    @ViewBuilder private var toastView: some View {
        if let t = m.toast {
            Text(t).font(.system(size: 14)).foregroundColor(.white)
                .padding(.horizontal, 16).padding(.vertical, 10)
                .background(Capsule().fill(Color.black.opacity(0.85)))
                .padding(.bottom, 30)
                .transition(.opacity)
        }
    }
}
