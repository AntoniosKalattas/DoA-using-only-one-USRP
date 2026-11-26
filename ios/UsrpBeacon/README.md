# USRP Beacon — iPhone

SwiftUI port of the Android app in `android/UsrpBeacon`. Same screens, same telemetry JSON
(`type: usrp_beacon`, plus `platform: "ios"`), same console endpoints (UDP :5005, `/phone`,
`/phone/backlog`, `/cal`).

## Build & install

1. Install full Xcode (App Store), open it once, and add the iOS platform when asked.
2. `open UsrpBeacon.xcodeproj`
3. Target **UsrpBeacon › Signing & Capabilities** → Team: your Apple ID (a free account works;
   the app then expires after 7 days and you re-run it from Xcode).
4. Plug in the iPhone, trust the Mac, turn on **Settings › Privacy & Security › Developer Mode**.
5. Select the iPhone as the run destination and press ⌘R.
   First launch: **Settings › General › VPN & Device Management** → trust your developer certificate.

## Differences from Android (iOS limits)

| Android | iPhone |
|---|---|
| Manufacturer data `0xFFFF "USRP"+tag` | Local name `USRP-<tag>` (ble_decode.py maps both to `PHONE-<tag>`) |
| ADV_NONCONN_IND | ADV_IND (connectable), chosen by iOS |
| Interval 100/250/1000 ms, TX power levels | Set by iOS; `interval_ms`/`tx_dbm` are sent as null |
| Works with screen off | **Keep the app open on screen** — iOS strips the name in the background |
| Notification LED / torch | Flashlight only (steady while radiating) |
| ARCore | ARKit (same y-up, metres, start-origin frame) |
| Satellite count | Not available on iOS |
