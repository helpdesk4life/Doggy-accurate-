# Porting Mikywalk to iPhone

A plan for a native iOS client against the existing backend. Written after reading
the whole Android app and the whole backend, so the numbers and file names here are
real.

**The backend does not change.** It is HTTP plus one WebSocket, with no Android
assumptions anywhere in it. `pytest` (71) and `scripts/e2e.py` (27) stay the
contract that both clients are measured against.

## The shape of the job

| | Android today | iOS target |
|---|---|---|
| Language | Kotlin 2.2.20 | Swift 6 |
| UI | Jetpack Compose, Material 3 | SwiftUI |
| App code | 21 files, **3,345 lines** | ~2,400–2,800 lines |
| Backend | 15 files, 1,648 lines Python | **unchanged** |
| Min OS | API 26 | iOS 17 (see decision 3) |

3,345 lines is small. This is a rewrite, not a migration, and it is a rewrite you
can finish. The reason to say that plainly is that the *hard* parts are not the
lines — they are three platform behaviours that have no iOS equivalent, covered
below.

## What ports cleanly

These are mechanical. Budget a day for the lot.

| Android | Lines | iOS | Notes |
|---|---|---|---|
| `model/Models.kt` | 57 | `Models.swift` | Plain structs and one enum |
| `data/remote/Protocol.kt` | 46 | `Protocol.swift` | `Codable`, `CodingKeys` for the snake_case fields |
| `data/auth/AuthModels.kt` | 89 | `AuthModels.swift` | Request/response DTOs, plus the `LoginResult` split |
| `geo/GeoMath.kt` | 45 | `GeoMath.swift` | Same haversine destination formula, same earth radius |
| `data/auth/AuthApi.kt` | 169 | `AuthAPI.swift` | `URLSession`, 9 endpoints |
| `data/auth/AuthStore.kt` | 87 | `SessionStore.swift` | Same shape; the storage underneath changes to Keychain |
| `data/SettingsStore.kt` | 140 | `Settings.swift` | `UserDefaults` instead of `SharedPreferences` |
| `ui/auth/LoginScreen.kt` | 358 | `LoginScreen.swift` | SwiftUI is more concise here; two steps, see below |
| `ui/auth/LoginViewModel.kt` | 284 | `LoginModel.swift` | `@Observable` instead of `StateFlow` |
| `ui/theme/Theme.kt` | 44 | `Theme.swift` | Same three marker colours |

Two files get **deleted** rather than ported:

- `data/auth/SecurePrefs.kt` (109 lines) — hand-rolled AES-GCM over the Android
  Keystore. iOS has the Keychain, which is built for exactly this. Use
  `SecItemAdd` with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`. The port
  is a net *simplification* and a security improvement, because the Keychain is
  hardware-backed and not included in iCloud backup with that accessibility class.
- `di/AppModule.kt` (34 lines) — Hilt has no iOS equivalent worth using at this
  size. One `AppEnvironment` struct passed through `.environment()` is clearer.

## The wire contract you are porting against

Worth having in one place, because it is the only thing both clients must agree on.

**WebSocket** `wss://<host>/ws/walk?name=<urlencoded>`, `Authorization: Bearer <access>`.

Client sends one frame type:

```json
{"type": "location", "lat": 52.3702, "lon": 4.8952, "accuracy": 8.0, "ts": 1794472773000}
```

Server sends four:

```json
{"type": "welcome", "walker_id": "…", "radius_m": 500.0}
{"type": "PROXIMITY_ALERT", "radius_m": 500.0,
 "walkers": [{"id": "…", "name": "Buddy", "distance_m": 50.0, "bearing_deg": 180.0}]}
{"type": "PROXIMITY_CLEAR"}
{"type": "error", "message": "…"}
```

**There is no latitude or longitude in `PROXIMITY_ALERT`, and there never will be.**
The server sends a distance and a bearing, both quantised (25 m, 30°), and the
client resolves them against its own fix. Do not add a coordinate field while
porting "for convenience" — that is the entire privacy model.

Close code **4401** means "token missing, expired or revoked". The client must
refresh and reconnect, not back off. Any other close code means back off.

**REST**, all under `/auth`: `register`, `login`, `refresh`, `logout`,
`logout-all`, `password/change`, `password-reset/request`,
`password-reset/confirm`, and `DELETE /account`. Registration needs an invite
code.

`login` has two outcomes. An account an operator set up still has the password
they were handed, and the server answers `must_change_password: true` with a
`change_token` instead of a token pair. That token is a separate JWT `typ` and
opens only `password/change`, so the port needs the same second step the Android
login screen has: ask for a new password, post it with the ticket, and store the
pair that comes back. Do not store the ticket as a session — it is not one.

## The background socket — the good news

Android runs `LocationTrackingService`, a foreground service with `START_STICKY`
and a persistent notification. That keeps the process alive and the socket open
indefinitely, which is why proximity alerts arrive with the phone in a pocket.

**iOS can do the same thing, and Apple documents it.** The mechanism is
`UIBackgroundModes: location` plus `allowsBackgroundLocationUpdates = true`,
started while the app is in the foreground. Apple's guide on
[handling location updates in the background](https://developer.apple.com/documentation/corelocation/handling-location-updates-in-the-background)
says the system suspends most apps and *enqueues* their location updates — then
adds the sentence that decides this port:

> "If your app needs updates in a more timely manner, you can ask the system to
> not suspend your app while location services are active."

Apple's DTS engineer is blunter in
[forum thread 35065](https://developer.apple.com/forums/thread/35065), answering
this exact question about holding a WebSocket open in the background:

> "Does that mean you're using the `location` background mode? If so, your app
> will never get suspended and thus my earlier comment applies: if you can
> prevent your app from suspending then networking in the background works just
> like networking in the foreground."

And earlier in the same thread: "if your app is executing … you can use any
networking API you like." A WebSocket is just a TCP socket, so it inherits that.

So the socket survives. This is how navigation apps work, and Apple's own list of
legitimate uses for background updates includes "generate time-sensitive
notifications or updates" — which is precisely what a proximity alert is.

### The four options, ranked

| Option | Verdict |
|---|---|
| **A. Background location mode, socket stays open** | **Best.** Documented by Apple, no backend change, keeps the social graph on-device, matches the Android architecture one-for-one. |
| **B. Reconnect on every location callback** | Keep as *resilience*, not as the strategy. You still need reconnect logic for the cases where the app really was terminated — but it is no longer the primary transport. |
| **C. Foreground only** | **Worse.** It gives up a capability iOS actually provides, and it breaks the core promise: "buzz me when a dog I don't like is near" would only work with the screen on. |
| **D. APNs push** | **Worst as a primary transport.** Apple caps background notifications at "two or three per hour" and does not guarantee delivery — see [Pushing background updates to your app](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app). A proximity alert needs sub-minute latency. It also cannot pick the friend-vs-disliked sound, because that relationship lives only on the device. |

### What you must get right, or it silently fails

These are the documented ways option A degrades. Each one is a real bug report
waiting to happen:

| Requirement | Why |
|---|---|
| Start updates **in the foreground** | `requestWhenInUseAuthorization()`: "attempts to start location updates while your app runs in the background will fail." Never start from a push handler. |
| `pausesLocationUpdatesAutomatically = false` | Defaults to `true`. Apple: "For apps that have in-use authorization, a pause to location updates ends access to location changes until the app launches again." |
| `distanceFilter = kCLDistanceFilterNone` | Since iOS 16.4, combining `startUpdatingLocation()` with `startMonitoringSignificantLocationChanges()` at low accuracy **and** distance filtering can get you suspended. |
| Handle approximate location | If the user grants approximate, updates arrive "at most a few times per hour" and are "usually within 1–20 kilometers". Proximity alerts become meaningless. Detect it and prompt for precise. |
| Restart on launch | "The system can terminate apps at any time… If your app actively receives and processes location updates and terminates, it should restart those APIs upon launch." |

### Two things that genuinely differ from Android

**There is no interval.** Android sets 5,000 ms. iOS has no equivalent — "the
operating system decides how often it reports a position, which is far less often
while the device is stationary." You control `distanceFilter`, not cadence. The
backend already tolerates irregular `ts` values, so this is a client-side change
only, but the 5-second mental model has to go.

**When In Use is enough.** Always authorization is *not* what keeps updates
flowing in the background. Apple: "If you enable background location updates, an
app with When in Use authorization continues to run in the background when
location services are active." Always only adds relaunch-after-termination, and
only for significant-change, visits and region monitoring — not for standard
updates. **Requesting When In Use only removes the single biggest App Store
review risk in this port.** App Store guideline 2.5.4 permits location background
mode outright: "Multitasking apps may only use background services for their
intended purposes: VoIP, audio playback, location, task completion, local
notifications, etc."

### What is still unverified

- **Force-quit.** Whether iOS relaunches a location app the user swiped away is
  contested. Apple's old *App Programming Guide* said location apps *are*
  relaunched after force-quit; that document has been removed, and Apple DTS now
  says the system treats the gesture as "a strong indication that the user
  doesn't want your app running." Do not design around it. Assume a force-quit
  ends the walk until the user reopens the app.
- **Battery.** `kCLLocationAccuracyBest` with no distance filter is the most
  expensive configuration Core Location offers. Measure on a real device before
  committing to it.
- **Review outcome.** Guideline 2.5.4 permits the mode; it does not promise
  approval. Write the reviewer note in App Store Connect explaining the proximity
  use case.
- **The simulator cannot test any of this.** It does not simulate suspension,
  termination or relaunch. Every claim above needs a physical iPhone.

## What does not port

### 1. Notification channels do not exist on iOS

Android has four channels (`mikywalk_tracking`, `mikywalk_proximity`,
`_friend`, `_disliked`) because a channel's sound is fixed at creation. iOS has no
channels at all: you set the sound per notification with
`UNNotificationSound(named: UNNotificationSoundName("friend_chime.wav"))`.

So the three sounds port directly — **copy `res/raw/friend_chime.wav` and
`res/raw/disliked_alarm.wav` into the iOS bundle unchanged** — and the code gets
simpler. What you lose is user control: on Android someone can mute "disliked"
independently; on iOS there is one app-level notification toggle.

`UNNotificationCategory` is not a substitute. It groups notifications for action
buttons, not for sound settings.

### 2. The foreground-service notification has no equivalent

Android shows a persistent "Mikywalk is tracking your walk" notification with a
Stop action. On iOS the blue location indicator in the status bar is system-owned
and not customisable. The closest analogue is a **Live Activity** (iOS 16.1+),
which would show the walk state on the Lock Screen and Dynamic Island. That is a
nice touch and a real differentiator, but it is extra work — treat it as Phase 6,
not Phase 1.

## File-by-file mapping

| Android | Lines | iOS | Notes |
|---|---|---|---|
| `data/remote/WalkSocketClient.kt` | 211 | `WalkSocket.swift` | `URLSessionWebSocketTask`. Keep the 4401-vs-backoff split and the 1 s→30 s cap. |
| `data/repository/WalkRepository.kt` | 179 | `WalkSession.swift` | `@Observable`. Keep "emit only newly-arrived walkers". |
| `notifications/NotificationHelper.kt` | 224 | `Notifications.swift` | `UNUserNotificationCenter`. Keep the `urgency()` ordering. |
| `service/LocationTrackingService.kt` | 196 | `LocationTracker.swift` | `CLLocationManager`, 5 s, `kCLLocationAccuracyBest`. |
| `ui/map/MapLibreMap.kt` | 401 | `MapView.swift` | `UIViewRepresentable` wrapping `MLNMapView`. |
| `ui/map/MapScreen.kt` | 637 | `MapScreen.swift` | The bulk of the UI work. |
| `ui/map/MapViewModel.kt` | 173 | *(folded into `WalkSession`)* | SwiftUI needs less view-model ceremony. |
| `MikywalkApp.kt` + `MainActivity.kt` | 51 | `MikywalkApp.swift` | |

### Map specifics

MapLibre Native iOS is mature and the existing `style.json` should load as-is.
Three things to carry over deliberately:

1. **Three sources and three layers** (`friend`, `disliked`, `unknown`), not one
   source with a data-driven `match` colour expression. The Android notes explain
   why: a subtly-wrong expression fails silently. Same reasoning applies.
2. **Name labels as SwiftUI overlays**, positioned with
   `mapView.convert(coordinate, toPointTo: view)` inside
   `mapView(_:regionDidChangeAnimated:)`. This is the direct analogue of the
   Compose `Text` overlay and it is *cleaner* on iOS — no `graphicsLayer`
   translation trick needed, and hit testing is a plain SwiftUI `.onTapGesture`.
3. **The `SymbolLayer` glyph problem was Android-specific.** MapLibre Android's
   HTTP stack mishandled a `304`. iOS may be fine. Do not assume either way —
   try a symbol layer for labels, and if it renders, use it and delete the overlay
   code. If it does not, the overlay approach is already proven.

The ring polygons come from `GeoMath.circle(...)` — same 64-segment approximation.

**One thing to fix while you are in there.** `assets/style.json` points straight at
`https://tile.openstreetmap.org/{z}/{x}/{y}.png`. The OSM Foundation's tile usage
policy forbids exactly this for a published app: it requires a valid identifying
User-Agent, and it blocks clients that generate real traffic. This is fine for
development and will get you blocked in production — on Android too, it is not an
iOS problem. Budget for a tile source before either app ships: a paid provider
(MapTiler, Stadia, Thunderforest) or self-hosted tiles. Changing it is a one-line
edit to `style.json` plus a key, and it is the same edit for both platforms.

## What iOS needs that Android did not

`Info.plist` entries, all of which are App Store review surface:

```xml
<key>NSLocationWhenInUseUsageDescription</key>
<string>Mikywalk shows you other walkers within 500 m and alerts you when a dog you have marked comes near.</string>
<key>UIBackgroundModes</key>
<array><string>location</string></array>
```

Note what is **not** there: `NSLocationAlwaysAndWhenInUseUsageDescription`. You do
not need Always authorization, because When In Use plus the `location` background
mode already keeps the app running while location services are active. Only add
the Always key if you later decide you need relaunch-after-termination, and accept
that it costs you a second permission prompt and a harder review.

Two compliance notes:

- **Account deletion is mandatory** under App Store Guideline 5.1.1(v) for any app
  that lets users create an account. You already have `DELETE /auth/account` and a
  confirmation dialog. Port it in Phase 1, not "later" — it is a rejection reason.
- **Background location is scrutinised, but the bar is lower than you think.**
  Guideline 2.5.4 lists location as a permitted background service, and Apple's
  own documentation names "generate time-sensitive notifications or updates" as a
  legitimate reason to want real-time background updates. Write that sentence into
  the App Store Connect reviewer note. Make sure the app degrades gracefully if
  the user grants approximate location or denies background access.

## Development environment gotchas

- `10.0.2.2` is the Android emulator's alias for the host. The iOS Simulator
  reaches the host at `localhost` directly. The default backend URL differs per
  platform — keep it in one place.
- **ATS is stricter than Android's network security config.** A local CA needs to
  be trusted in the Simulator's keychain, or you add a dev-only
  `NSAppTransportSecurity` exception. For a physical device on the LAN, the cert's
  SAN must include the Mac's LAN IP, not just `localhost`.
- **Background location cannot be tested properly in the Simulator.** Use
  Xcode's GPX "Freeway Drive", but confirm real background behaviour on hardware.
  This is the one part of the port you cannot verify from a desk.

## Phased plan

Each phase ends at something you can actually check against the running backend.

| Phase | Scope | How you know it works |
|---|---|---|
| **0. Decisions** | Answer the four questions at the bottom. | Written down. |
| **1. Skeleton + auth** | Xcode project, `AppEnvironment`, Keychain session store, login, register with invite code, refresh, password reset, delete account. | Sign in as `fresh@example.com`, kill the app, relaunch still signed in. `DELETE /auth/account` on a throwaway account returns 204 and the app returns to login. |
| **2. Walk session** | `CLLocationManager`, `URLSessionWebSocketTask`, reconnect with backoff, 4401 → refresh → reconnect. | Start a walk, run `hold_walker.py --meters 60 --name Buddy`, watch `PROXIMITY_ALERT` frames arrive with `distance_m` and `bearing_deg` and no coordinates. |
| **3. Map** | MapLibre, three marker layers, two rings, name labels, tap-to-mark dialog. | Buddy's marker appears due north. Tap the label, mark liked → green. Mark disliked → red. |
| **4. Notifications** | Three sounds, `urgency()` ordering, permission request. | `xcrun simctl push` a test payload, or trigger a real alert and confirm the sound differs per relationship. |
| **5. Settings** | Dog name, backend URL, warning distance, marked-dogs list with remove. | Marked dogs survive a relaunch; removing one returns the marker to amber. |
| **6. Background + polish** | `UIBackgroundModes: location`, `allowsBackgroundLocationUpdates = true`, `pausesLocationUpdatesAutomatically = false`, `distanceFilter = kCLDistanceFilterNone`, restart-on-launch, approximate-location prompt. Live Activity if wanted. App Store metadata. | On a physical iPhone: start a walk, lock the screen, put the phone in a pocket, walk past a marked dog, and the alert still fires with the right sound. Then repeat with the app backgrounded for 20 minutes. |

Phases 1–5 are roughly a week of focused work at this size. Phase 6 is where the
schedule risk lives.

## How to verify the port without guessing

The Android app is a working reference implementation against the same backend.
Use it:

1. Run both clients against the same stack at once. Two accounts, two devices or
   one device plus the emulator. Compare what each shows for the same
   `hold_walker.py` walker.
2. The backend logs every connect, disconnect and rejected socket. If the iOS
   client is misbehaving, the log tells you whether the frame ever arrived.
3. `scripts/e2e.py` already pins the server contract. Run it before and after any
   backend change you are tempted into during the port — the answer should be
   "no backend change".

**Neither client has tests.** The Android side has none at all, which is its
biggest weakness. The port is a chance to start the iOS side with tests from
Phase 1 rather than inheriting the gap: `GeoMath`, the frame decoding, and the
`urgency()` ordering are all pure functions and all worth a test each.

## Decisions I need from you

1. **Background alerts — decided: option A.** Background location mode with the
   socket held open, When In Use authorization only. Implemented in
   `ios/Mikywalk/Location/WalkLocationManager.swift` and
   `ios/Mikywalk/Remote/WalkSocketClient.swift`. APNs stays on the shelf as a
   possible later addition for the terminated case, but not as the primary
   transport, because Apple throttles background pushes to a few per hour.
2. **MapLibre iOS or MapKit?** MapLibre keeps visual parity with Android and
   reuses `style.json`. MapKit is one less dependency and better battery, but you
   lose the custom raster style and the ring polygons get clumsier.
3. **Minimum iOS version?** iOS 17 unlocks the `@Observable` macro and makes the
   view models much cleaner. iOS 16 reaches more devices and costs you
   `ObservableObject` boilerplate. iOS 17 is the better engineering choice.
4. **Is Android still maintained?** If iOS becomes primary, the two clients will
   drift and the backend contract becomes the only thing holding them together.
   Worth deciding now whether Android gets new features or only fixes.

## What I would do first

Answer question 1, then build Phase 1 and Phase 2 before touching the map. Auth
and the socket are where the real risk is, and both are verifiable from a desk
against the existing backend. The map is the most visible work and the least
risky — save it for when the plumbing is proven.
