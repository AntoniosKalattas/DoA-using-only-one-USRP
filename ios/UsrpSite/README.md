# USRP Site (iPhone)

Single-purpose app: lay the phone on the USRP, GPS is averaged (30 s default), and the result is
sent to the Capture Console as a `usrp_site` message (same format as the beacon apps).

- Laptop link: the ngrok link from the radar page (📱 phone link), or `<laptop-ip>` on the LAN (port 5080 is added).
- Sends to `<link>/phone/backlog`. Every measurement is saved on the phone first (`outbox.json`) and kept
  until the console confirms it. Retries every 5 s, when the network comes back, and when the app is opened.
  Duplicates are ignored by the console (sid + seq), so re-sending is safe.
- The console uses the measurement time (`t_ms`), and a late-arriving older measurement never overwrites a newer one.

## Build
Needs full Xcode (not installed on this Mac as of 2026-11-24). `open UsrpSite.xcodeproj` → Signing: your
Apple ID team → plug in the iPhone → ⌘R. Free account: re-run from Xcode every 7 days.
