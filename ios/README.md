# Mikywalk for iOS

The background strategy, implemented. This is not a complete app — it is the
part of the port that decides whether the product works, written out so the
decision in `../IOS-PORT.md` is concrete rather than a paragraph.

**None of this has been compiled.** It was written on Windows, which has no Swift
toolchain and cannot build iOS targets. Treat it as a reviewed starting point,
not as working code. The first job on a Mac is to make it build.

## The strategy in one paragraph

Android keeps a foreground service alive so the WebSocket stays open. iOS does
the same thing through a different door: `UIBackgroundModes: location` plus
`allowsBackgroundLocationUpdates = true`, started while the app is in the
foreground, tells the system not to suspend the app while location services are
active. Apple documents this directly — "you can ask the system to not suspend
your app while location services are active" — and Apple DTS confirms the
consequence: "if you can prevent your app from suspending then networking in the
background works just like networking in the foreground." A WebSocket is a TCP
socket, so it inherits that. No backend change, no APNs, and the friend/disliked
graph stays on the device.

## What is here

| File | Role |
|---|---|
| `Mikywalk/Location/WalkLocationManager.swift` | The Core Location configuration that makes the strategy work. **Read this first.** |
| `Mikywalk/Remote/WalkSocketClient.swift` | `URLSessionWebSocketTask`, 1 s → 30 s backoff, 4401 → refresh, 20 s keepalive ping. |
| `Mikywalk/Remote/Protocol.swift` | The wire frames, field names unchanged from Android. |
| `Mikywalk/Session/WalkSession.swift` | Orchestration: fixes out, proximity frames in, session renewal. |
| `Mikywalk/Notifications/ProximityNotifier.swift` | One sound per relationship, chosen per notification. |
| `Mikywalk/Model/Models.swift` | `WalkerLocation`, `NearbyWalker`, `DogRelationship`, `MarkedDog`. |
| `Mikywalk/Resources/Info.plist` | The two keys that matter, with the reasoning inline. |

## What is not here

Auth, the map, settings, and the UI. Those are ordinary work that does not depend
on the background decision, and they are Phases 1, 3 and 5 in `../IOS-PORT.md`.
`WalkSession` talks to them through the `WalkSessionEnvironment` protocol, so
`AuthStore` and `SettingsStore` just need to conform.

## Building it on a Mac

1. Xcode 15 or later, iOS 17 deployment target (`@Observable` needs it).
2. New iOS App target, SwiftUI, bundle id `com.mikywalk`.
3. Drag `Mikywalk/` into the project. Replace the generated `Info.plist` with
   `Mikywalk/Resources/Info.plist`, or copy its keys into the generated one.
4. Signing & Capabilities → Background Modes → tick **Location updates**. This
   writes the same `UIBackgroundModes` entry the plist already has; having both
   is harmless, having neither is a crash.
5. Copy the two tones into the app bundle, unchanged:
   ```
   cp ../android/app/src/main/res/raw/friend_chime.wav   Mikywalk/Resources/
   cp ../android/app/src/main/res/raw/disliked_alarm.wav Mikywalk/Resources/
   ```
   They must be in the bundle root or a folder reference, not a group, or
   `UNNotificationSound(named:)` will not find them.
6. Point `backendURL` at the host. The Simulator reaches it at `localhost:8000`;
   a physical device needs the Mac's LAN IP and a certificate whose SAN includes
   it.

## Verification, in the order that finds bugs fastest

Everything below needs a **physical iPhone**. The Simulator does not simulate
suspension, termination or relaunch, so it cannot test any of this.

| # | Test | Pass condition |
|---|---|---|
| 1 | Start a walk, lock the screen, wait 5 minutes | Fixes keep arriving at the backend. If they stop, the background mode or `pausesLocationUpdatesAutomatically` is wrong. |
| 2 | Same, but walk past a marked dog | Alert fires with the right sound while the phone is locked. |
| 3 | Same, 20 minutes backgrounded | Socket still open, no reconnect storm in the log. |
| 4 | Revoke the session server-side mid-walk | Close arrives as **4401**, token refreshes, socket reconnects. **Confirm the code actually arrives as 4401** — `CloseCode` is a Swift enum and a non-standard code may not round-trip. If it does not, the fallback is to refresh on any close during an authenticated session. |
| 5 | Grant approximate location instead of precise | App detects it and asks for precise. Alerts are meaningless otherwise. |
| 6 | Force-quit mid-walk, then move | Record what happens. This is the one behaviour Apple's documentation no longer states clearly. Do not assume it resumes. |
| 7 | Let the system terminate the app (memory pressure), then move | `resumeIfNeeded()` restarts the walk. |

## Known gaps

- **Battery.** `kCLLocationAccuracyBest` with `kCLDistanceFilterNone` is the most
  expensive Core Location configuration there is. Measure before shipping; the
  likely fix is a distance filter of 5–10 m, which costs a little alert latency.
- **No interval control.** iOS decides cadence. The Android app's fixed 5,000 ms
  has no equivalent, and the backend already tolerates irregular `ts` values.
- **Force-quit.** Contested. `resumeIfNeeded()` will resume after a force-quit,
  which is the intended trade-off but may not match what iOS actually allows.
- **Time Sensitive entitlement.** `ProximityNotifier` sets
  `.timeSensitive` for disliked dogs. Without the entitlement iOS delivers
  normally rather than failing, so it is safe to leave in, but the warning will
  not break through Focus until the entitlement is granted.
- **Tiles.** `assets/style.json` points at `tile.openstreetmap.org`, which
  violates the OSM tile policy for a published app. That is a problem on both
  platforms and is not fixed here.
