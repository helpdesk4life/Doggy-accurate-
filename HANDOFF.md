# Handoff — DogWalk

State captured at the end of the blocking session (2026-11-14). Read this
first when picking the project back up. For what to build next, see
`ROADMAP.md`; for the iOS plan in full, `IOS-PORT.md`; for the iOS code that
exists, `ios/README.md`.

**The tree is clean and deployed to the VPS**, where `deploy/verify.sh` passes
12/12. The admin panel landed earlier as `e5ed2ad admin pagina en account
creation`, on top of `7d26413 Eerste versie`; see **What landed in `e5ed2ad`** at
the bottom for what that commit contains.

## What this project is

A dog-walking app that shows you and other walkers on a map and alerts you when
another dog comes within a configurable distance.

- `android/` — Kotlin, Jetpack Compose, MapLibre, Hilt, foreground location service
- `backend/` — FastAPI over WebSockets, Redis GEO for the proximity maths, Dockerised

## Current state

Everything below was verified running on 2026-11-14. If the machine has been
rebooted, see **Resuming** instead.

| Thing | State |
| --- | --- |
| `backend-backend-1` | Up (healthy), `127.0.0.1:8000` (loopback only) |
| `backend-redis-1` | Up (healthy), `127.0.0.1:6379` (loopback only), AOF on, `redis-data` volume |
| `backend-caddy-1` | Up, `0.0.0.0:8443`, `caddy-data` volume |
| Admin panel | Up at `/admin/ui`, operator `operator@example.com` / `operator-password` |
| Emulator | `Medium_Phone_API_37.0`, running as `emulator-5554` |
| App | Signed out at the login screen, on the VPS-default build. `fresh@example.com` is local-Redis only, so it cannot sign in against the VPS — see **Resuming** for the local-stack recipe. |

Redis holds 26 accounts, 75 unused invites and 46 used ones. One account,
`tokke@test.be`, was created through the panel and **has never completed its
first sign-in** — `must_change_password` is still `1` and its generated password
was shown once and is gone. Delete and recreate it, or use the reset flow, before
trying to sign in as it.

Accounts live in Redis under `dogwalk:users:*`, so they survive a stack restart
now that Redis has a volume. The geo index is disposable and is rebuilt from the
next fix.

Both published ports are on `127.0.0.1`, not `0.0.0.0`, so neither the plaintext
backend port nor Redis is reachable from the LAN. Caddy on `8443` is the only way
in from outside.

The VPS (`207.180.240.29`) is the production target: same two containers plus a
host Caddy, running the blocking release with a clean tree, and
`deploy/verify.sh` passes 12/12 there. It pulls from `github`, not `origin`, and
`git` must run as `thomas` — root has no deploy key. See `deploy/VPS.md`.

## Resuming

Docker Desktop does not start on boot. Launch it, wait for the whale to settle,
then:

```bash
cd /c/Users/thoma/Downloads/dog/backend
docker compose up -d --build
curl -s http://127.0.0.1:8000/health
curl -sk https://localhost:8443/health      # through Caddy
```

Expected: `{"status":"ok","radius_m":500.0,"walker_ttl_seconds":30.0}`

Then the admin panel, which needs no build step and no Node:

```bash
start http://127.0.0.1:8000/admin/ui          # or https://localhost:8443/admin/ui
```

Sign in as `operator@example.com` / `operator-password`. That operator is a dev
account in Redis; recreate it with `scripts/create_admin.py` if the volume was
wiped.

**The backend has no bind mount and no `--reload`.** The image is built from the
source, so a Python or template edit needs `docker compose up -d --build`, not a
restart. That is the single most common way to spend ten minutes debugging a
change that never reached the container.

Android side:

```bash
cd /c/Users/thoma/Downloads/dog/android
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
./gradlew :app:installDebug
```

`JAVA_HOME` is not set globally and the `java` on `PATH` is JDK 8, so Gradle
aborts with `Gradle requires JVM 17 or later` unless you export it first. The
Android Studio JBR is JDK 25.

The committed `DEFAULT_BACKEND_URL` is the VPS, and the in-app override lives
behind the login screen — so to sign in against the local stack you have to set
`DEFAULT_BACKEND_URL` to `wss://10.0.2.2:8443/ws/walk` in
`android/app/build.gradle.kts`, build, sign in, and revert it afterwards. The
local accounts (`fresh@example.com` / `hunter2hunter2`) exist only in the local
Redis. The map tiles are unaffected either way: `assets/style.json` points at
`maps.thomasfeyens.be` with an absolute URL, so the basemap loads from the VPS
even when the backend is local.

The emulator must be running first. If you recreate the AVD or run
`adb shell pm clear com.dogwalk`, re-grant the permissions — clearing wipes them:

```bash
adb shell pm grant com.dogwalk android.permission.ACCESS_FINE_LOCATION
adb shell pm grant com.dogwalk android.permission.ACCESS_COARSE_LOCATION
adb shell pm grant com.dogwalk android.permission.POST_NOTIFICATIONS
```

## iOS port — where it stands

The background-strategy question is settled and the first slice of the port is
written. **Nothing has been compiled.** There is no Swift toolchain on this
machine and no iOS target to build, so the code is reviewed but unverified.

### The decision: option A

Background location mode with the WebSocket held open, **When In Use
authorization only**. No APNs, no backend change, and the friend/disliked graph
stays on the device.

### Why — the research, so it does not get redone

The mechanism is `UIBackgroundModes: location` plus
`allowsBackgroundLocationUpdates = true`, started while the app is in the
foreground. Apple's
[Handling location updates in the background](https://developer.apple.com/documentation/corelocation/handling-location-updates-in-the-background):

> "If your app needs updates in a more timely manner, you can ask the system to
> not suspend your app while location services are active."

Apple DTS, [forum thread 35065](https://developer.apple.com/forums/thread/35065),
answering this exact question about holding a WebSocket open in the background:

> "If so, your app will never get suspended … if you can prevent your app from
> suspending then networking in the background works just like networking in the
> foreground."

A WebSocket is a TCP socket, so it inherits that. This is how navigation apps
work.

**The correction worth not reintroducing.** An earlier draft of `IOS-PORT.md`
claimed iOS suspends the app between location callbacks and tears down the
`URLSessionWebSocketTask`. That was wrong, and it inverted the recommendation —
it made foreground-only look like the safe choice when it is the worst one. If
you find that claim anywhere, it is stale.

### The four options, ranked

| Option | Verdict |
| --- | --- |
| **A. Background location mode, socket stays open** | **Best.** Documented, no backend change, matches the Android architecture. |
| **B. Reconnect on every location callback** | Resilience only, not a strategy. Still needed for the terminated case. |
| **C. Foreground only** | Worse. Gives up a capability iOS provides. |
| **D. APNs push** | **Worst as primary.** Apple caps background pushes at ["two or three per hour"](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app) and does not guarantee delivery. Also cannot pick the friend-vs-disliked sound, because that lives on the device. |

### What exists

| File | Role |
| --- | --- |
| `ios/DogWalk/Location/WalkLocationManager.swift` | The Core Location config that makes the strategy work. Read this first. |
| `ios/DogWalk/Remote/WalkSocketClient.swift` | `URLSessionWebSocketTask`, 1 s → 30 s backoff, 4401 → refresh, 20 s ping. |
| `ios/DogWalk/Remote/Protocol.swift` | Wire frames, field names unchanged from Android. |
| `ios/DogWalk/Session/WalkSession.swift` | Orchestration, entry/exit alerting, session renewal. |
| `ios/DogWalk/Notifications/ProximityNotifier.swift` | One sound per relationship, chosen per notification. |
| `ios/DogWalk/Model/Models.swift` | `WalkerLocation`, `NearbyWalker`, `DogRelationship`, `MarkedDog`. |
| `ios/DogWalk/Resources/Info.plist` | `UIBackgroundModes: location` and When-In-Use only. |

912 lines of Swift. Not written: auth, map, settings, UI. `WalkSession` reaches
them through the `WalkSessionEnvironment` protocol, so `AuthStore` and
`SettingsStore` only need to conform.

### The five settings that make or break it

Each one is a silent failure if wrong, and each is commented in
`WalkLocationManager.swift`:

1. Start updates **in the foreground** — starting from the background fails.
2. `pausesLocationUpdatesAutomatically = false` — defaults to `true`, and for an
   in-use app a pause "ends access to location changes until the app launches
   again".
3. `distanceFilter = kCLDistanceFilterNone` — since iOS 16.4, low accuracy plus
   distance filtering plus significant-change monitoring can get you suspended.
4. Handle approximate location — updates arrive "at most a few times per hour",
   "within 1–20 kilometers". Alerts become meaningless.
5. Restart the APIs on launch — the system can terminate you at any time.

Two things that differ from Android: **there is no interval** (iOS decides
cadence; you control `distanceFilter` only), and **When In Use is enough**
(Always only adds relaunch-after-termination for significant-change, visits and
region monitoring — not standard updates).

### Three iOS bugs already caught, worth not reintroducing

Found by review, not by a compiler, so they may not be the only ones:

1. **`URLSession.delegate` is read-only.** The session cannot be built in `init`
   with `self` as delegate. It is a `lazy var` now.
2. **`startUpdatingLocation()` is a no-op before authorization is granted**, and
   it does not begin on its own once the user answers the prompt. A
   `pendingStart` flag makes the delegate finish the start.
3. **`authorizationStatus` was `.notDetermined` at launch**, so `resumeIfNeeded()`
   would always refuse to resume a terminated walk. It is seeded from the
   manager in `init` now.

### First job tomorrow

1. Get it to compile. Xcode 15+, iOS 17 target, `xcodebuild`. Expect type and
   actor-isolation errors; the code was written without a compiler.
2. Work through the seven device tests in `ios/README.md`. They need a **physical
   iPhone** — the Simulator does not simulate suspension, termination or
   relaunch, so it cannot test any of this.
3. Test 4 is the one to watch: confirm a revoked token actually arrives as close
   code **4401**. `URLSessionWebSocketTask.CloseCode` is a Swift enum, so a
   non-standard code may not round-trip. If it does not, refresh on any close
   during an authenticated session instead.

### Still unverified

- **Force-quit.** Contested. Apple's old *App Programming Guide* said location
  apps *are* relaunched after a force-quit; that document has been removed, and
  DTS now says the gesture means "the user doesn't want your app running".
  `resumeIfNeeded()` will resume anyway, which is the intended trade-off.
- **Battery.** `kCLLocationAccuracyBest` with no distance filter is the most
  expensive Core Location configuration there is. Measure before shipping; the
  likely fix is a 5–10 m distance filter.
- **Review outcome.** Guideline 2.5.4 permits location background mode; it does
  not promise approval. Write the reviewer note in App Store Connect.

## Privacy — the frame format carries no coordinates

This is the most important design constraint in the codebase, and the easiest one
to break by accident.

`PROXIMITY_ALERT` describes each neighbour as a **distance and a bearing from
you**, both snapped to a grid. There is no `lat`/`lon` anywhere in the protocol,
so a modified client cannot ask for an absolute position — the server has no field
in which to send one.

```json
{"type": "PROXIMITY_ALERT", "radius_m": 500, "walkers": [
  {"id": "…", "name": "Buddy", "distance_m": 50.0, "bearing_deg": 180.0}
]}
```

- `app/privacy.py` holds the whole policy: `bearing_degrees()`,
  `quantise_distance()` (25 m), `quantise_bearing()` (30°).
- `app/proximity.py` builds every `NearbyWalker` through those three functions.
- The client places the marker with `GeoMath.destination(self, bearing, distance)`
  in `ui/map/MapLibreMap.kt`, via a private `ResolvedWalker` that is recomputed
  whenever either the self position or the neighbour list changes.
- The quanta are env vars (`DISTANCE_QUANTUM_M`, `BEARING_QUANTUM_DEG`) because
  they are a trade-off: tighter means a more accurate map and a more identifiable
  walker. `Settings.validate()` rejects negative values; `0` disables quantisation
  and is development-only.

Residual risk: a client that walks around and collects many bearings to the same
walker can trilaterate them. That is inherent to any proximity feature. The quanta
raise the cost; they do not remove it.

## Friend groups — client-side only, on purpose

Marking a dog as liked or disliked is stored in `SharedPreferences` under
`marked_dogs` and **never sent to the server**. The backend has no concept of a
relationship, so the social graph cannot leak from Redis even if the store were
dumped. The whole feature is Android-only; `pytest -q` stayed at 71 passed
(then — 156 now).

Where it lives:

| Concern | File |
| --- | --- |
| `MarkedDog`, `DogRelationship` | `model/Models.kt` |
| Persistence, `setRelationship` / `clearRelationship` | `data/SettingsStore.kt` |
| Channels, sounds, urgency pick | `notifications/NotificationHelper.kt` |
| Three marker sources, tappable name tags | `ui/map/MapLibreMap.kt` |
| Mark dialog, marked-dogs list | `ui/map/MapScreen.kt` |

Four things that will bite if changed carelessly:

1. **Android fixes a channel's sound at creation.** Editing the sound on an
   existing channel id does nothing on a device that already created it. That is
   why there are three proximity channels (`dogwalk_proximity`,
   `_friend`, `_disliked`) rather than one. Adding a fourth relationship means a
   fourth channel id, not a new sound on an old one. **Changing a tone also needs
   `pm clear` or a reinstall** to recreate the channel — otherwise you will test
   the old sound and conclude the change did nothing.
2. **The tones are bundled, not system.** `res/raw/friend_chime.wav` and
   `res/raw/disliked_alarm.wav`, addressed as
   `android.resource://<package>/<resId>`. `RingtoneManager` only offers one
   notification tone and one alarm tone, so friends would have sounded identical
   to an unmarked dog. Both WAVs are generated sine tones (see the README), so
   there is no licence to track.
3. **Urgency is `disliked > unmarked > friend`, as an explicit `urgency()`
   function.** Enum ordinal order is `FRIEND=0, DISLIKED=1, UNKNOWN=2`, which would
   rank a friend above a warning. Do not "simplify" it to `maxBy { it.ordinal }`.
4. **`WalkRepository` emits only newly-arrived walkers**, not the whole
   `withinWarning` set. Re-emitting the full set would replay the disliked alarm
   every time an unrelated friend appeared. `alertedIds` resets on stop, which is
   what makes the stop/start cycle a usable way to re-test a channel.
5. **The name tags are the tap targets, not the markers.** They are Compose `Text`
   with `Modifier.graphicsLayer { translationX/Y }`, and Compose does apply layer
   transforms to hit testing — verified by tapping a tag and getting the dialog.
   A MapLibre `queryRenderedFeatures` call would have been the alternative and is
   far less reliable here.
6. **Do not also call `Vibrator` explicitly.** The channel's `vibrationPattern`
   already fires; an extra `vibrator.vibrate()` in `notifyProximity` made every
   alert buzz twice. It was removed. The channel pattern also respects the user's
   per-channel settings and Do Not Disturb, which a direct call does not.

## Blocking — the one social signal that is server-side

Friend and disliked are local because they only change how a marker looks and
sounds. A block cannot be: the point is that the other walker stops receiving
your bearing at all, and only the server can stop sending it. So blocking is the
one piece of the social graph that lives in Redis.

Two keys per user, kept in step by `app/blocks.py`:

| Key | Type | Holds |
| --- | --- | --- |
| `dogwalk:blocks:<id>` | hash | blocked id -> the name shown in settings |
| `dogwalk:blocked_by:<id>` | set | ids that blocked this user |

**Invisibility is symmetric.** `hidden_for()` returns the union of both, and
`refresh_neighborhood()` drops those hits before building the frame. A one-way
filter would leave the blocker on the blocked walker's map, which is the exact
thing they blocked them to prevent. If you ever "simplify" this to a single key,
you reintroduce that hole.

The name is stored because the settings list has to name someone who is not
walking right now, and the hub only knows the names of live connections. It is
the same display name the server already broadcasts to everyone in range, so the
copy exposes nothing new.

Where it lives:

| Concern | File |
| --- | --- |
| The two keys, `block` / `unblock` / `hidden_for` / `forget` | `backend/app/blocks.py` |
| The filter, before the frame is built | `backend/app/proximity.py` |
| `GET` / `POST` / `DELETE /blocks` | `backend/app/routes_blocks.py` |
| `BlockedWalker`, `BlocksApi` | `model/Models.kt`, `data/blocks/BlocksApi.kt` |
| Block action, blocked list, unblock | `ui/map/MapScreen.kt`, `ui/map/MapViewModel.kt` |

Four things worth not undoing:

1. **The filter is in `refresh_neighborhood`, not the client.** A client-side
   filter would still send the bearing to the blocked walker's phone, which is
   the thing the feature exists to prevent.
2. **Block and unblock refresh both maps immediately.** `routes_blocks.py` calls
   `refresh_neighborhood` for both walkers, so the marker goes away on the tap
   rather than on the next GPS fix. It uses `refresh_neighborhood` rather than
   `update_walker` on purpose: the latter also refreshes every neighbour, which
   sends each of the two sockets the same frame twice. `last_neighbors` is left
   stale, which costs one redundant refresh on the next fix and can never leak,
   because every refresh filters against the hidden set of the walker it is sent
   to.
3. **Blocking clears the local relationship.** A blocked dog must not linger in
   the friends list. Unblocking does not restore it; the mark was about a walker
   you then wanted nothing to do with.
4. **Account deletion calls `blocks.forget()`.** Without it, everyone who blocked
   the deleted walker keeps a stale id and a name in their settings forever.

Verified on the emulator against the local stack: 14 `PROXIMITY_ALERT` frames
carrying the other walker, then nothing but `PROXIMITY_CLEAR` after the block;
the settings list showed the blocked walker; unblocking brought them back; and
the block was still enforced and still listed after `pm clear` (a reinstall).

## Authentication

Accounts plus JWT. There is no anonymous mode: the app opens on a login screen
and the map is unreachable until you sign in.

| Endpoint | Purpose |
| --- | --- |
| `POST /auth/register` | 201, needs an invite code, returns a token pair |
| `POST /auth/login` | Exchange email and password for a token pair |
| `POST /auth/refresh` | Exchange a refresh token for a new pair |
| `POST /auth/logout` | Revoke one refresh token (204) |
| `POST /auth/logout-all` | Revoke every refresh token for the account (204) |
| `POST /auth/password-reset/request` | Email a 6-digit code (always 202) |
| `POST /auth/password-reset/confirm` | Set a new password with a code (204) |
| `DELETE /auth/account` | Delete the account and its data (204, needs the password) |

- Access token 15 min, refresh token 30 days. Refresh tokens are indexed in
  `dogwalk:users:<id>:refresh` so they can be revoked individually, and a used
  refresh token is marked for `REPLAY_GRACE_SECONDS = 60` so a retried request on
  a flaky connection does not look like a replay. Past the grace window a reused
  token is treated as theft and the whole family is revoked.
- Passwords are hashed with stdlib `hashlib.scrypt` (N=2^14, r=8, p=1). No
  third-party crypto dependency. `authenticate()` compares against
  `_DUMMY_PASSWORD_HASH` for unknown emails so a missing account and a wrong
  password take the same time.
- Ten failed logins for one email locks it for 15 minutes.
- Reset codes are 6 digits, stored as a sha256 key, valid 15 minutes, single use,
  capped at 10 attempts. `request` returns 202 whether or not the address exists.
- Changing or resetting a password bumps `token_version`, invalidating every
  access token already in flight. Deleting an account also closes any live socket
  through `hub.close()`.
- Per-IP rate limits (`app/ratelimit.py`) apply to the auth endpoints, keyed on
  `X-Forwarded-For`. `Caddyfile` **overwrites** that header with `{remote_host}`
  rather than appending, so a client cannot spoof its own address. Register is
  capped at 20/hour. `RATE_LIMIT_ENABLED=0` disables the limiter.
- The WebSocket authenticates with an `Authorization: Bearer` header, not a query
  parameter, so the token never lands in an access log. A bad or expired token
  closes with application code **4401**.
- **The walker id is the account id, taken from the token.** A client cannot
  claim to be someone else by editing a query string.
- The `name` query parameter goes through `_clean_name()` in `app/ws.py`: control
  characters stripped, whitespace collapsed, 64-character cap.

### Registration is invite-gated

`POST /auth/register` needs a single-use code. Mint one with
`scripts/mint_invites.py` (see **Verification commands**).

The gate exists to close account enumeration, and the ordering is the whole
point: the code is checked **before** the email is looked up, so an uninvited
caller gets the same 403 whether or not the address exists. Changing the status
code would not have worked — an attacker could register with a password of their
choosing and then try to log in with it, and the result would tell them the same
thing. `app/invites.py` has the long version of that argument.

A code is consumed before the email check and refunded if the address is taken,
so a mistyped email does not burn an invitation. `INVITE_REQUIRED=0` turns the
gate off; only do that once there is email verification to replace it.

### `JWT_SECRET` now fails hard

`Settings.validate()` runs at `create_app` time and raises `RuntimeError` when the
secret is still `DEV_JWT_SECRET` and `DOGWALK_ENV` is not a development
environment, or when the secret is shorter than 32 bytes. A warning is not enough:
a deployment that logs a warning and carries on is a deployment where anyone who
has read this repository can mint a token for any account.

`docker-compose.yml` sets `DOGWALK_ENV` and a real `JWT_SECRET`. If you see the
`RuntimeError` on a dev box, set `DOGWALK_ENV=development`.

### Token storage is encrypted

`SecurePrefs` encrypts each value with a Keystore-backed AES-GCM key
(`AES/GCM/NoPadding`, 256-bit, alias `dogwalk_auth_key`) and stores `iv:ciphertext`
base64. A value that fails to decrypt is dropped rather than crashing the app.
`EncryptedSharedPreferences` was the obvious choice but is deprecated in
`androidx.security`.

`android:allowBackup="false"` and `fullBackupContent="false"` are set, so the
encrypted blob is not swept into a cloud backup that would restore it onto a device
without the Keystore key.

Verify it is actually encrypted:

```bash
export MSYS_NO_PATHCONV=1
adb shell run-as com.dogwalk cat /data/data/com.dogwalk/shared_prefs/dogwalk_auth.xml
```

Every value should read `iv:ciphertext` base64. If you can see a JWT or an email
address in there, something regressed.

## Admin panel — accounts and invites, nothing else

A server-rendered web panel at `/admin/ui`, plus a JSON API at `/admin/*` that
does the same things. It can list accounts, create one, sign one out, delete one,
mint invite codes, and show an audit log.

**It cannot see where anyone is.** That is a hard scope constraint, not an
oversight: no route in `app/routes_admin.py` or `app/admin_ui.py` reads
`dogwalk:walkers:geo`, and both test files assert that no admin response and no
rendered page contains `lat`, `lon`, `latitude`, `longitude`, `bearing`,
`bearing_deg`, `distance_m` or `accuracy`. If you add a feature here, keep that
assertion passing — it is the thing that stops the panel quietly becoming a
surveillance tool.

### Why server-rendered Jinja

The repo has no Node toolchain and no JS build step; it is Android plus Python.
Adding a React admin would have meant a second package manager, a second build,
and a second thing to keep working. Jinja templates in `app/templates/admin/`
render in the same FastAPI process, so the panel ships with the backend and
there is nothing to compile.

### The four decisions worth not undoing

1. **Admin is its own JWT `typ`, not a role claim.** `decode_token` refuses a
   token whose type is not the one asked for, so the boundary is enforced by the
   decoder rather than by a check each route has to remember. A walker token gets
   401 from every `/admin` route; an admin token gets 401 from `/auth/*` and
   close code `4401` from `/ws/walk`. Both directions are tested.
2. **The HTML pages use a cookie, the API uses a bearer token.** A browser form
   cannot set an `Authorization` header. The cookie is HttpOnly, `SameSite=Strict`
   and scoped to `/admin`; `SameSite=Strict` is what stands in for a CSRF token.
   There is no refresh token for admins — the session is
   `ADMIN_TOKEN_TTL_SECONDS` (8 h) and then you sign in again.
3. **Admins are created by script, never by an endpoint.** An endpoint that
   creates admins must itself be protected by an admin, which is a bootstrapping
   problem. `scripts/create_admin.py` is the only way in.
4. **The audit log records counts, never secrets.** Minted codes are logged as a
   number, not as codes; account creation logs the address and the new `user_id`,
   never the password. The log is readable by every admin, so it must not become
   a second copy of the things it audits.

### Generated passwords and the forced change

Creating an account takes an email and nothing else. The server generates a
16-character password from `PASSWORD_ALPHABET` (no `I`, `O`, `0`, `1` — 80 bits),
shows it once on the page the form redirects to, and stores only a hash.

The account is created with `must_change_password: "1"`. Login then answers
`must_change_password: true` with a **`pwchange` token** and `access_token` /
`refresh_token` **absent rather than empty**, so a client that ignores the flag
fails loudly instead of holding a blank session. `POST /auth/password/change`
spends the ticket, sets the password, bumps `token_version` and returns a normal
pair — the user is signed in the moment they finish.

Three consequences to keep in mind:

- **The operator cannot recover the password.** Only a hash is stored. If it is
  lost before handover, delete the account and make another. The reset flow also
  works as an escape hatch, because `users.set_password` clears the flag.
- **Changing to the same password is refused (400).** Accepting it back would
  clear the flag without changing anything.
- **The Android client had to change.** `AuthApi.login` decoded into a fixed
  `TokenResponse`, so the new response shape would have thrown a
  `SerializationException`. It now returns a `LoginResult` sealed interface
  (`SignedIn` / `MustChangePassword`), which keeps `AuthStore` untouched and
  means the ticket is never stored as a session. `LoginScreen` is a two-step
  form.

### Code map

| File | Role |
| --- | --- |
| `app/admin.py` | The store: operators, account creation, listing, revoke, delete, invites, audit. No HTTP. |
| `app/routes_admin.py` | The JSON API and `require_admin`. |
| `app/admin_ui.py` | The HTML pages, the cookie, and the one-shot flash. |
| `app/templates/admin/` | `base`, `login`, `dashboard`, `users`, `invites`, `audit`, `confirm_delete`. |
| `scripts/create_admin.py` | `--email`, `--list`, `--backfill`. |

### Two things that will cost you time

**The flash is a Redis key, not a session cookie.** `_flash_put` / `_flash_take`
store a JSON object under a random id with a 120 s TTL, and the redirect carries
the id in the query string. That is how the generated password survives exactly
one page load and then is gone. It replaced an invites-only list format, so both
the password notice and the minted-codes notice now go through the same two
helpers — do not add a third mechanism.

**`--backfill` is needed once.** The account list pages over
`dogwalk:users:created`, not a keyspace scan. Accounts registered before that
index existed do not appear until you run
`scripts/create_admin.py --backfill`.

## TLS

Two setups, deliberately different.

**Production** is the VPS: `deploy/Caddyfile` serves `backend.thomasfeyens.be`
and `maps.thomasfeyens.be` with Let's Encrypt certificates that Caddy issues and
renews itself. No `tls internal` and no `default_sni`, because a real hostname
sends SNI. `deploy/verify.sh` asserts the issuer, so a regression to the local CA
fails a check.

**Local development** is what the rest of this section describes: Caddy mints its
own CA with `tls internal`, so no domain and no internet are needed.

```
app ──wss://10.0.2.2:8443──▶ Caddy ──http──▶ backend:8000
```

Two things in `backend/Caddyfile` are load-bearing and easy to break:

1. **The site address needs explicit hostnames** — `localhost:8443, 10.0.2.2:8443`.
   A bare `:8443` gives `alert internal error` on every handshake because Caddy
   has no name to put in a certificate.
2. **`default_sni 10.0.2.2` is required.** TLS clients must omit SNI when the
   host is an IP literal (RFC 6066), and OkHttp does exactly that. With no SNI
   Caddy falls back to matching against its own container address, finds no
   certificate, and aborts the handshake. The client only sees a bare
   `SSLHandshakeException`, which looks identical to a wrong CA. `default_sni`
   gives those connections a name to match.

The debug build trusts the CA through
`android/app/src/debug/res/raw/caddy_root.crt`, wired up in
`android/app/src/debug/res/xml/network_security_config.xml`. Re-export it after
recreating the `caddy-data` volume:

```bash
cd backend
./scripts/export_ca.sh
```

The release build has **no** network security config and **no**
`usesCleartextTraffic` — it uses the system trust store and refuses cleartext.
That is the point of the split: the dev CA is debug-only.

For a physical device, change both the site address and `default_sni` to the
host's LAN IP, re-export the CA, and point the app at
`wss://<lan-ip>:8443/ws/walk`. `default_sni` holds one value, so two different
IPs cannot both work without SNI — use a hostname if you need that.

## Release signing

`android/app/build.gradle.kts` reads `android/keystore.properties`, which is
gitignored:

```properties
storeFile=/absolute/path/to/release.jks
storePassword=…
keyAlias=…
keyPassword=…
```

Generate the keystore yourself — it is not in the repo and must not be:

```bash
keytool -genkeypair -v -keystore release.jks -keyalg RSA -keysize 4096 \
  -validity 10000 -alias dogwalk
```

With the file present the release build produces `app-release.apk`, signed.
Without it the build still succeeds and produces `app-release-unsigned.apk`, so a
fresh clone can build without a keystore.

## Verification commands

```bash
cd /c/Users/thoma/Downloads/dog/backend

# Unit tests — fakeredis, no server needed
./.venv/Scripts/python.exe -m pytest -q

# End-to-end against the running Docker stack and real Redis
./.venv/Scripts/python.exe scripts/e2e.py

# Park a second dog on the map for UI testing
./.venv/Scripts/python.exe scripts/hold_walker.py --meters 60 --name Buddy

# Hand someone an invite code
./.venv/Scripts/python.exe scripts/mint_invites.py
./.venv/Scripts/python.exe scripts/mint_invites.py --list

# Create an operator for the admin panel at /admin/ui
./.venv/Scripts/python.exe scripts/create_admin.py --email you@example.com
./.venv/Scripts/python.exe scripts/create_admin.py --list

# Android unit tests — JVM only, no emulator and no backend needed
cd /c/Users/thoma/Downloads/dog/android
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
./gradlew :app:testDebugUnitTest

# Static analysis — a gate in CI, so run it before pushing
./gradlew :app:lintDebug
```

Accounts can also be created from the panel itself: the Accounts page has an
"Add an account" form, and `POST /admin/users` does the same over the API. No
invite code is spent — the code gates self-registration by a stranger, and an
operator is not one.

The password is generated (16 characters, shown once) and the account must
replace it at first sign-in: login answers `must_change_password` with a
`pwchange` token instead of a session, and that token opens only
`/auth/password/change`. The Android login screen has a second step for it. If
the operator loses the password before handing it over, delete the account and
make another — only a hash is stored.

`pytest -q` → **156 passed**. `scripts/e2e.py` → **56/56 passed**.
`./gradlew :app:testDebugUnitTest` → **33 passed**. `./gradlew :app:lintDebug` →
**0 errors**, 45 warnings.

| Test file | Covers |
| --- | --- |
| `test_auth.py` | Registration, duplicate email, password rules, login, lockout, refresh rotation, revocation, token tampering |
| `test_proximity.py` | Alert round trip, leaving the radius, disconnect cleanup, frame validation, the 30 s expiry sweep, the GEO query, and that alerts carry no coordinates |
| `test_privacy.py` | Bearing cardinal directions, quantisation snapping, the error bound, the zero-quantum escape hatch |
| `test_security.py` | Password reset (send, single use, wrong code, attempt cap, no enumeration), account deletion, refresh replay grace vs. revocation, config validation, cache headers |
| `test_ratelimit.py` | Per-address throttling, per-address isolation, health exempt, limiter can be disabled |
| `test_invites.py` | The gate, single use, refund on a taken email, and that a taken and a free address are indistinguishable without a code |
| `test_admin.py` | The admin/walker token boundary in both directions, admin lockout, user listing and paging, account creation and the forced password change it triggers, revoke and delete, invite minting, the audit log, and that no admin response carries a position |
| `test_admin_ui.py` | The admin cookie (HttpOnly, SameSite, path), redirects without one, the add-account form and its errors, the one-shot password notice, the delete confirmation page, the one-shot mint flash, and that no page mentions a coordinate |
| `test_blocks.py` | Blocking a walker: symmetric invisibility in both directions, unblock, the name kept for an offline walker, blocking yourself, a block surviving a new session, and that deleting an account clears it from other block lists |

`scripts/e2e.py` covers token rejection (missing and malformed), welcome frames,
bidirectional alerts at 100 m, name propagation, clear at 800 m, re-entry
re-firing, coordinate validation, malformed JSON rejection, the 30 s TTL sweep,
the coordinate-free alert shape (no `lat`/`lon`, bearing 0/180), the
password-reset and account-deletion surfaces, the invite gate, and the admin
API — login, the token boundary in both directions, listing, creating an account
(and that it spends no invite code), minting, revoking, deleting, and that the
audit log holds no codes and no passwords. It uses
Rotterdam as its reference point on purpose — see the comment at the top of the
file.

It clears the per-IP rate-limit buckets at startup, because the auth endpoints are
throttled per hour and a second run in the same hour would otherwise fail on
limits the first run spent rather than on anything real. The helper refuses to do
that against a non-local Redis. It also mints its own invite codes, the same way
an operator would.

`scripts/hold_walker.py` holds a walker at a fixed distance and re-sends every
5 s so it survives the TTL. Use `--meters 60` to trigger a notification at the
default 100 m warning distance, `--meters 120` to stay silent. It registers a
throwaway account derived from `--email`.

## What is verified

- `docker compose up -d --build` builds and runs clean; Redis healthcheck gates
  the backend start.
- `pytest -q` → 156 passed.
- `scripts/e2e.py` → 56/56 passed against real Redis.
- `https://localhost:8443/health` → 200 through Caddy, with a certificate issued
  by the local CA for both `localhost` and `10.0.2.2`.
- **The handed-over-password cycle, live on `:8000`**: created an account from the
  panel → password shown once → refresh shows nothing → login returns
  `must_change_password: True` with `access_token: None` → the `pwchange` ticket
  is refused by `/auth/logout-all` (401) → `POST /auth/password/change` 200 with a
  real session → the old password 401 → the new password 200 with
  `must_change_password: False` → cleanup 204.
- **The same through Caddy TLS** (`https://localhost:8443`): 303 → notice reading
  `tls-temp3@example.com · 5VH3NHWM8T7PMQRL` → refresh shows nothing.
- **The panel renders and the boundary holds**: `/admin/ui` without a cookie
  redirects to the login page; the cookie is HttpOnly, `SameSite=Strict`, path
  `/admin`; a walker token gets 401 from every `/admin` route and an admin token
  gets 401 from `/auth/*`.
- **No admin surface carries a position**: asserted over every JSON response and
  every rendered page, not just the ones that look likely.
- **Fresh install over TLS**: cleared app data, registered a new account through
  Caddy (`POST /auth/register 201`), started a walk, and the socket connected
  over `wss://10.0.2.2:8443` with the default URL. Walker id in
  `dogwalk:walkers:geo` matched the account id in `dogwalk:users:email:*`.
- **Token refresh**: with an expired access token the backend closes 4401, the
  app calls `POST /auth/refresh`, gets 200, reconnects, and the backend logs
  `walker … connected`. No user action.
- **Proximity over TLS, coordinate-free**: `hold_walker.py --meters 60 --name
  Buddy` produced frames of the form
  `{"id": "…", "name": "Pickle", "distance_m": 50.0, "bearing_deg": 180.0}` — no
  coordinates. Buddy's marker rendered due north of Pickle, header read
  `Live · 1 nearby`, and the notification fired on channel `dogwalk_proximity`
  (id 2001) with `Buddy is nearby / Buddy is 50 m away`.
- **Token storage is encrypted**: `run-as com.dogwalk cat
  shared_prefs/dogwalk_auth.xml` shows all four values as `iv:ciphertext` base64.
  No plaintext JWT or email address.
- **Release manifest is clean**: `allowBackup="false"`,
  `fullBackupContent="false"`, zero occurrences of `usesCleartextTraffic` or
  `networkSecurityConfig`, no `debuggable`. The debug manifest has the cleartext
  exemption. This is the proof that it is debug-only.
- **Password reset end to end**: dialog opens with the email pre-filled → "Send
  code" → `POST /auth/password-reset/request 202`, backend log shows
  `Your code is: …` → step two with a non-enumerating notice → `POST
  /auth/password-reset/confirm 204` → "Password changed. Sign in with the new
  one." → signed in with the new password.
- **Account deletion end to end**: wrong password → `DELETE /auth/account 401`
  and "Could not delete the account. Check your password and try again."; correct
  password on a throwaway account → `204`, app fell back to the login screen,
  prefs wiped to `<map />`, and signing in as the deleted account → 401.
- **Invite gate end to end**: registering with the field blank shows "Enter the
  invite code you were sent." without a request leaving the device; a minted code
  creates the account and lands on the map; the code moves from
  `dogwalk:invites` to `dogwalk:invites:used`. Against the live stack, a taken
  address and a never-seen one both answer `403 {"detail": "an invite code is
  required to register"}` — byte-identical, which is the property that matters.
- **Signing path**: with a throwaway keystore and `keystore.properties` present,
  `validateSigningRelease` runs, the output flips to `app-release.apk`, and
  `apksigner verify --print-certs` shows the expected subject. Without the file,
  `app-release-unsigned.apk`.
- **Friend groups end to end**, on `emulator-5554` with
  `hold_walker.py --meters 60 --name Buddy`:
  - unmarked → marker amber, notification on `dogwalk_proximity`;
  - tapped Buddy's name tag → mark dialog → **Likes** → marker turned green
    (`#2E9E5B`, sampled from the screenshot at the same pixel bounds the amber
    marker had occupied) and the next arrival fired on
    `dogwalk_proximity_friend` with `color=0xff2e9e5b`;
  - **Does not like** → marker red (`#C62828`, same bounds) and the next arrival
    fired on `dogwalk_proximity_disliked` with `color=0xffc62828` and title
    `Watch out - Buddy is nearby`;
  - `dumpsys notification` shows the channels carry three different sounds:
    `dogwalk_proximity` → `content://settings/system/notification_sound`,
    `dogwalk_proximity_friend` → `android.resource://com.dogwalk/2131427330`
    (`res/raw/friend_chime.wav`), `dogwalk_proximity_disliked` →
    `android.resource://com.dogwalk/2131427329` (`res/raw/disliked_alarm.wav`),
    with vibration patterns `[0,80,90,80]` and `[0,400,120,400,120,400]`.
    Cross-check the ids against
    `app/build/intermediates/runtime_symbol_list/debug/processDebugResources/R.txt`
    — `int raw disliked_alarm 0x7f0b0001`, `int raw friend_chime 0x7f0b0002`.
  - settings sheet listed Buddy under **Dogs yours does not like**; the remove
    button cleared it, `marked_dogs` went to `[]`, the empty-state hint appeared,
    the marker returned to amber and the next arrival fired on the neutral
    `dogwalk_proximity` channel again.
  - `marked_dogs` in `shared_prefs/dogwalk_settings.xml` round-trips as
    `[{"id":"…","name":"Buddy","relationship":"FRIEND"}]` and survives a restart.
    Nothing about it is sent to the server — the backend was not touched and
    `pytest -q` was still 71 passed at the time.
- **The proximity alert is withdrawn when the last walker leaves the warning
  distance**, on `emulator-5554` with a scratch script that moved one walker on a
  single socket (a disconnect would have sent `PROXIMITY_CLEAR` and exercised a
  different path). Holding at 60 m: notification `id=2001` present on
  `dogwalk_proximity`, header `1 nearby`. Moved to 300 m — outside the 100 m
  warning distance but inside the 500 m server radius: notification gone, header
  still `1 nearby`, which is the proof the withdrawal came from the warning-distance
  path and not from the walker leaving the radius. Back to 60 m: notification
  returned, so re-entry still alerts. Stopping the walk cleared both `2001` and
  the foreground `1001`.
- **Blocking a walker, end to end on `emulator-5554`**: the other walker was
  visible and alerting → blocked from the tap-a-dog dialog → gone from the map,
  and the other socket stopped receiving `PROXIMITY_ALERT` and got
  `PROXIMITY_CLEAR` instead, so the filter runs before the frame is built rather
  than in the UI. Settings listed the walker under **Blocked walkers**; unblocking
  restored both directions; the block survived `pm clear`.
- **Blocking on the VPS over `wss://backend.thomasfeyens.be`**, with two real
  sockets: both walkers saw each other → `POST /blocks` 204 → both received
  `PROXIMITY_CLEAR` → `GET /blocks` 200 carrying the name → `DELETE` 204 → both
  visible again. `GET /blocks` answers 401 unauthenticated, which is how the route
  was confirmed live after the deploy.
- **`deploy/verify.sh` on the VPS**: 12/12 PASS, including the Let's Encrypt
  issuer check and a 206 range request against `benelux.pmtiles`.

## Android tests

33 JVM tests in `android/app/src/test/`, run with
`./gradlew :app:testDebugUnitTest`. No emulator, no backend, no Robolectric.

| File | Covers |
| --- | --- |
| `data/auth/AuthStoreTest.kt` | The sign-in state machine: save, updateTokens, clear, and the three restore cases |
| `data/repository/WalkRepositoryTest.kt` | Proximity bookkeeping (arrival once, not per fix; withdrawal on the last leaving and on a clear frame) and the three session-renewal branches |
| `data/remote/WalkSocketClientTest.kt` | Connect, bearer header, name query parameter, frame decoding, 4401, reconnect, disconnect, the backoff ladder |
| `data/remote/HttpBaseTest.kt` | `httpBaseOf()`: `ws` → `http`, `wss` → `https`, the query string dropped, and a URL without the socket path used as-is |
| `testing/InMemorySecureStore.kt` | Shared fake, not a test |

Six things about the setup that will cost time if undone:

1. **`SecureStore` exists so `AuthStore` can be built on the JVM.** `SecurePrefs`
   needs the Android Keystore, which does not exist off-device. `AuthStore` takes
   the interface and `AppModule` binds the real implementation, so the tests use
   an in-memory map while the app still gets AES-GCM.
2. **`WalkSocketClient` takes the application scope.** It used to build its own
   `CoroutineScope(SupervisorJob() + Dispatchers.IO)`, which leaked and could not
   be cancelled by a test.
3. **The socket tests use `runBlocking`, not `runTest`.** OkHttp delivers its
   callbacks on its own threads, so virtual time advances past a real event and
   turns every wait into a timeout. `runTest` is right for pure logic and wrong
   for anything waiting on the network stack.
4. **The fake server has to ack the client's close in `onClosing`**, for exactly
   the reason the client does. `MockWebServer.shutdown()` waits for its queues to
   go idle and throws `Gave up waiting for queue to shut down` if a socket is left
   half-open. That failure surfaces in `tearDown`, so it reads like a broken test
   rather than a broken fake.
5. **`WalkRepository` depends on three interfaces, not the concrete classes.**
   `WalkSettings` (the settings the walk reads), `WalkTransport` (what
   `WalkSocketClient` implements) and `TokenRefresher` (the one auth call the
   repository makes, split out of `AuthApi`) — each bound to the real class in
   `AppModule` with a one-line `@Provides`, so production wiring is unchanged and
   the repository runs on the JVM. Adding a dependency to the repository means
   deciding which seam it belongs behind.
6. **The repository test gives the session a scope of its own.** On the
   `TestScope` itself the collectors block `runTest` from ever finishing (each
   test burns its whole timeout); on `backgroundScope` they only run while the
   test body happens to suspend, so a frame emitted directly is silently
   dropped. The `session {}` helper builds the repository a supervisor on the
   test's own scheduler, cancelled in `finally`, and the fake transport enqueues
   its emissions behind the collectors — queue order, not timing, decides who
   sees what.

`backoffMillis()` is `internal` rather than private so the ladder can be asserted
directly; driving it through the socket would mean waiting real seconds per rung.

## CI

`.github/workflows/ci.yml` runs three jobs on every push to `main` and every pull
request. Each runs the same command as **Verification commands** above, so a green
run means what a green local run means.

| Job | Runs | Needs |
| --- | --- | --- |
| `backend` | `pytest -q` on Python 3.12 | Nothing — the tests inject fakeredis |
| `android` | `:app:testDebugUnitTest :app:lintDebug` on JDK 17 | Android SDK 36, installed by the job |
| `e2e` | `scripts/e2e.py` | `docker compose up -d --build`, then a health poll |

Python 3.12 is deliberate: it is what `backend/Dockerfile` ships, so CI tests the
interpreter that runs in production rather than the 3.14 on this machine.

The two Gradle tasks share one invocation so the Kotlin compile happens once, and
`--continue` keeps lint running when a test fails — otherwise the first failure
hides the second.

**Lint is a gate, not a report.** Turning it on found one real thing:
`NotificationHelper.notifyProximity` calls `NotificationManager.notify` without a
permission check lint can see. The check is there — `canPostNotifications()` three
lines above — but lint's dataflow does not cross a private method, so the call
carries `@SuppressLint("MissingPermission")` with the reason in the KDoc. Keep the
guard and the annotation honest if you add another notification path.

The workflow was checked with `actionlint` 1.7.12, with `shellcheck` 0.11.0 on
`PATH` so the `run:` blocks are linted too — clean. `actionlint` does not verify
that an action tag exists, so the pinned majors were confirmed separately with
`git ls-remote --tags`: `checkout@v7`, `setup-python@v7`, `setup-java@v6`,
`setup-android@v4`, `setup-gradle@v6`, `upload-artifact@v7`.

### Where it runs

GitHub Actions, on `github.com/helpdesk4life/doggy` (private). The first run,
`9bd0114`, finished green on all three jobs:

| Job | Result | Evidence from the log |
|---|---|---|
| Backend tests | success | `134 passed, 1 warning in 16.91s` |
| End-to-end against real Redis | success | `56/56 passed` |
| Android tests and lint | success | `BUILD SUCCESSFUL in 3m 35s`, `testDebugUnitTest` and `lintDebug` both executed |

The two `skipped` steps in that run — `Upload reports` and `Dump container logs`
— are both `if: failure()`, so skipping is the correct outcome on a green run.

The suite has grown since: 156 backend tests and 33 Android tests as of the
2026-11-14 doc sync. That run is the last one confirmed green from here — the
GitHub connector exposes no workflow-run status, so check the Actions tab after a
push rather than assuming.

`origin` is still the self-hosted Gitea at `192.168.3.70:2222`, a private address
the VPS cannot reach. `github` is the remote that matters: it is what CI watches
and what the server clones from. Push to both, or drop `origin` — as of
2026-11-14 it is behind `main` and drifts further with every push that only goes
to `github`.

`.github/dependabot.yml` bumps the action majors weekly. They move: `checkout` went
v5 → v7 while this project has existed, and a deprecated major fails the run rather
than warning.

## Bugs fixed, worth not reintroducing

**OkHttp `onClosing` must acknowledge the close.** OkHttp only calls `onClosed`
once *both* peers have sent a close frame. When the server closes first, the
client has to reply in `onClosing` or the close is completely silent: no
`onClosed`, no `onFailure`. The app then sat at `CONNECTED` forever, showing
"Live" over a dead socket, never refreshing its token and never reconnecting.
`WalkSocketClient.onClosing` now calls `webSocket.close(NORMAL_CLOSURE, null)`.

**The backend had no logging configuration.** Uvicorn only configures its own
loggers, so every `logger.info` in `app.*` was dropped by the root logger's
last-resort handler. Walker connect/disconnect and rejected-socket lines were
invisible, which made a rejected token look like a client that never connected.
`app/main.py` now calls `logging.basicConfig(level=INFO)`.

**`LoginViewModel` never cleared `isBusy` on success.** `LoginScreen()` uses
`hiltViewModel()` with no NavHost, so the view model is Activity-scoped and
survives sign-out. `submit()` set `isBusy = true` and only reset it on failure, so
after signing out the login form came back permanently disabled with a spinner —
the user was locked out until they killed the app. `onSuccess` now sets
`isBusy = false` and clears `password` (so it is not held for the app's lifetime).

**The rate-limit tests were a coin flip.** `app/ratelimit.py` buckets on
`int(time.time()) // window_seconds`, so its counters reset on a wall-clock
boundary. Every test that sends exactly `limit.requests + 1` requests therefore
fails whenever the boundary falls between the last allowed request and the one
that should be blocked — the counter restarts at zero and the request goes
through. `test_admin_login_is_rate_limited` was the first to lose that toss, on
`697aa50`, a docs-only commit: the 429 came back as the per-email lockout
(`{"detail":"too many failed attempts"}`, 37 bytes) instead of the limiter
(`{"detail":"too many requests"}`, 30 bytes, with `Retry-After`).

The behaviour is correct — a fixed window is documented as coarse, and a burst
straddling a boundary gets two windows' worth. The tests were wrong to assume
otherwise. `tests/conftest.py` now provides `frozen_window`, which replaces
`ratelimit.time` with a clock that only moves when a test calls `.advance()`.
Any new test that counts requests against a limit must take that fixture.
`test_a_window_boundary_resets_the_counter` pins the tradeoff itself, so the
next person meets it as a passing test rather than a mystery failure.
`MapViewModel.deleteAccount()` had the same latent flaw and got the same fix.

## Known gaps

- **The Android tests cover the transport, the session, the repository and the
  auth store, not the rest.** 33 tests: `WalkSocketClient` (connect, bearer
  header, frame decoding, 4401, reconnect, the backoff ladder), `WalkRepository`
  (proximity bookkeeping and the session-renewal branches), `AuthStore` (the
  sign-in state machine) and `HttpBase` (the `ws` → `http` mapping). `SettingsStore`,
  `NotificationHelper` and the Compose UI have none.
- No reconnect backoff tuning — the client reconnects, but the retry cadence has
  not been stress-tested against a backend that stays down.
- **The proximity alert is withdrawn when the last walker leaves the warning
  distance, but not when one of several leaves.** The notification names the most
  urgent walker in the set, so if a disliked dog walks away while a friend stays,
  it keeps naming the dog that left. Fixing that means re-posting the content
  without re-sounding it, and `setOnlyAlertOnce(true)` cannot do it here — it
  would also silence the arrival that the urgency ordering depends on.
- **Friend groups do not survive a reinstall or a `pm clear`.** They are
  `SharedPreferences` only, with no backup (`allowBackup="false"`). That is the
  price of keeping the social graph off the server, and it is the right trade for
  now — but if it ever needs to sync across devices, it needs a server-side store
  and a privacy decision to go with it.
- ~~**Marking is not blocking.**~~ Closed: marking is still local and cosmetic,
  but blocking is now real and server-side. `app/blocks.py` keeps a hash of who
  you blocked and a set of who blocked you, and `refresh_neighborhood` drops the
  union before the frame is built, so a blocked walker never receives your
  bearing. Symmetric on purpose. See **Blocking** above.
- **Invite codes are the only thing closing account enumeration.** Anyone holding
  a code can still probe addresses, and a leaked code is a leaked invitation.
  Email verification is the general fix; until then keep `INVITE_REQUIRED=1` and
  mint codes one at a time.
- **Reset emails need SMTP credentials to actually arrive.** `app/email.py` has a
  real `SmtpEmailSender` behind `EMAIL_SENDER=smtp`, so the code path is written
  and tested; what is missing is a provider account. Until `SMTP_HOST` and
  `SMTP_FROM` are set, the VPS runs `EMAIL_SENDER=logging` with
  `ALLOW_LOGGING_EMAIL=1` and reset codes land in `docker compose logs backend`.
  Switching to `smtp` without those two is a boot failure, not a silent fallback —
  see `Settings.validate()`.
- ~~**The admin panel has no second gate.**~~ Closed: `deploy/Caddyfile` puts
  Caddy `basic_auth` on `/admin*` (user `thomas`) ahead of the app's own login, and
  an unauthenticated request gets 401. The hash is bcrypt — rotate it with
  `caddy hash-password` and reload. The app's own lockout is tighter than the
  walker one on purpose — 5 failures in 15 minutes, because a handful of operator
  accounts can each delete every account in the deployment — but there is still no
  password-change flow for operators, so `scripts/create_admin.py` is the only way
  to set one.
- **A generated password is unrecoverable by design.** If the operator loses it
  before handover, the account must be deleted and recreated. Worth a "copy"
  button on the notice if this ever gets used by someone other than you.
- **The panel cannot edit an account**, only create, revoke and delete. Changing
  an email or a dog name is not possible from the UI.
- **No pagination on the audit page** — it shows the newest 100 of a 1000-entry
  capped list.
- `default_sni` holds a single value, so the local TLS setup works for one
  IP-or-hostname at a time.
- The backend is single-instance; the hub lives in process memory. See
  `ROADMAP.md` P1.8 for the trigger to fix it.
- `pytest` emits a `StarletteDeprecationWarning` about `httpx` in
  `starlette.testclient`. Harmless, pre-existing, unrelated to Docker.
- **The iOS code has never been compiled.** No Swift toolchain on Windows and no
  iOS target. Bracket balance and plist validity were checked; type errors,
  actor-isolation warnings and API misuse are all still possible. Treat
  `ios/` as a reviewed starting point, not working code.
- **The iOS port has no tests and no CI.** Same gap as Android, one platform
  further out.
- **`assets/style.json` is a vector style over the self-hosted PMTiles** at
  `pmtiles://https://maps.thomasfeyens.be/benelux.pmtiles`, so no third-party
  tile service is involved and the OSM tile-usage violation is gone. MapLibre
  Native reads PMTiles directly — supported since Android 11.7.0, and this
  project is on 12.0.0 — so there is no tile server to run: Caddy serves the
  archive and the client makes range requests against it. The style has no
  symbol layers and no `glyphs` key on purpose; see the glyph note under
  **Environment facts**. `ios/` still has no style file.

## Environment facts worth not rediscovering

- JDK: `C:\Program Files\Android\Android Studio\jbr` (JDK 25)
- **No Swift toolchain on this machine.** `swift` and `swiftc` are absent, and
  iOS targets cannot be built on Windows at all. The `ios/` directory can only be
  compiled on a Mac with Xcode 15 or later.
- Gradle 9.5.1, AGP 8.13.0, Kotlin 2.2.20, KSP 2.2.20-2.0.4, Hilt 2.57,
  Compose BOM 2025.09.00, MapLibre 12.0.0, OkHttp 4.12
- Backend venv: `backend/.venv/Scripts/python.exe`
- `ruff` is installed in that venv for linting only, and is deliberately **not**
  in `requirements.txt`. Run it as
  `.venv/Scripts/python.exe -m ruff check --select F,E9 app/ tests/ scripts/`.
- **`curl -X POST -L` re-sends the POST to the redirect target and drops the
  body.** Testing the panel's form handlers with `-L` silently does the wrong
  thing. Use `-D -` to read the `Location` header, then GET it.
- **A cookie set on `localhost` is not sent to `127.0.0.1`.** They are different
  hosts to a cookie jar, so a jar captured through Caddy on `localhost:8443`
  authenticates nothing against `127.0.0.1:8000`. The failure looks like a
  redirect to the login page, which reads like a broken session rather than a
  missing cookie.
- `export MSYS_NO_PATHCONV=1` is required for `adb` device paths **and**
  `docker compose exec` container paths in Git Bash
- Emulator reference point is the Amsterdam Dam: `adb emu geo fix 4.8952 52.3702`
  (longitude first)
- **`adb exec-out screencap` images are 1080x2400 but render at 900x2000 when
  viewed.** Multiply any visual estimate by 1.2, or better, read exact bounds from
  `adb shell uiautomator dump /sdcard/ui.xml`. Guessing coordinates cost several
  failed taps this session.
- The soft keyboard shifts dialog layout between the tap that opens it and the
  `input text` that follows. Re-dump bounds after each keyboard show/hide.
- The soft keyboard covers the login form's submit button. Press `keyevent 4`
  to hide it before tapping, or the tap types into the password field.
- MapLibre `SymbolLayer` does not work here — remote glyphs return `304` that
  MapLibre's HTTP stack never turns into a usable body, and bundling PBFs as
  `asset://` also failed. Name labels are Compose `Text` overlays in
  `ui/map/MapLibreMap.kt`, positioned via `Projection.toScreenLocation` and
  refreshed on every camera move. Do not try to reintroduce glyphs.
  `deploy/Caddyfile` strips `ETag` and `Last-Modified` from `/fonts/*` and
  `/sprites/*` to dodge the `304`, but that only removes one cause; the vector
  style in `assets/style.json` is therefore written with no symbol layers at
  all, and the Compose overlays remain the only labels.
- **`.smoke/ui.py`** dumps the current screen and prints every non-empty `text`
  with its exact tap centre, optionally filtered:
  `backend/.venv/Scripts/python.exe .smoke/ui.py buddy`. It is the fastest way to
  get a reliable tap coordinate and it replaced a lot of guesswork. `.smoke/` also
  holds ~10 MB of scratch screenshots from these sessions; safe to delete.
- **Marker colours are verified by sampling screenshot pixels**, not by eye:
  `PIL` over `adb exec-out screencap -p`, matching `#2E9E5B` / `#C62828` /
  `#E0A030` within a tolerance and printing the bounding box. The bbox is the
  proof the marker moved or recoloured rather than a second marker appearing.
  Note `#E0A030` is both the unmarked marker and the warning ring, so filter by
  region when checking the amber case.
- `adb shell pm clear com.dogwalk` resets the dog name to a new random one. If you
  clear app data during a test, set the name back to `Pickle` in settings before
  finishing, or the docs stop matching the device.

## What landed in `e5ed2ad`

The admin panel and the generated-password flow are committed as
`e5ed2ad admin pagina en account creation` (32 files, +3437/−97), on top of
`7d26413 Eerste versie`. The tree is clean. It went in as one commit rather than
the three-way split that was planned, so `git show e5ed2ad` is the whole story.

New files:

```
backend/app/admin.py
backend/app/admin_ui.py
backend/app/routes_admin.py
backend/app/templates/admin/          (7 templates)
backend/scripts/create_admin.py
backend/tests/test_admin.py
backend/tests/test_admin_ui.py
```

Modified: `backend/app/{auth,config,main,ratelimit,routes_auth,users}.py`,
`backend/scripts/{_auth,e2e}.py`, `backend/requirements*.txt`, the four Android
auth files plus `strings.xml`, and `README.md` / `ROADMAP.md` / `HANDOFF.md` /
`IOS-PORT.md`.

The counts in `README.md` and `ROADMAP.md` (156 tests, 56 e2e checks) describe
this state. Re-run the gates to confirm the baseline before starting new work —
`pytest` needs nothing, `e2e.py` needs the Docker stack up (see **Resuming**):

```bash
cd /c/Users/thoma/Downloads/dog/backend
.venv/Scripts/python.exe -m pytest -q                    # expect 156 passed
.venv/Scripts/python.exe scripts/e2e.py                  # expect 56/56
.venv/Scripts/python.exe -m ruff check --select F,E9 app/ tests/ scripts/
```
