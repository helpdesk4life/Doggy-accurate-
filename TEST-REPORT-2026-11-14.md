# TEST-REPORT-2026-11-14 - Mikywalk Android app

## Verdict: GO WITH CONDITIONS

Everything that can run in this environment passes: lint (0 errors), 29/29 unit
tests, debug and release builds, and a live emulator smoke test that signed in
against the real VPS backend over HTTPS and opened a `wss://` walk socket through
Caddy (`walker ... connected` in the backend log). Conditions: there is no
instrumented-test source set, no coverage and no benchmark module, so those layers
are absent rather than green, and the map still uses third-party OSM raster tiles
instead of the self-hosted PMTiles.

## Environment

- Device/emulator: `Medium_Phone_API_37.0` (emulator-5554, 1080x2400, API 37), app installed from `app-debug.apk`
- JDK / Gradle: Android Studio JBR 25.0.3 / Gradle 9.5.1 (AGP 8.13.0, Kotlin 2.2.20)
- Variants tested: debug (lint, unit tests, APK, emulator run) and release (compile only)
- Backend under test: the live VPS `backend.thomasfeyens.be` (default URL since commit `f043c7b`)
- Skipped layers and why: coverage (no JaCoCo in the build), instrumented tests
  (`app/src/androidTest` does not exist), performance (no benchmark module),
  release-on-device (no physical device attached)

## Results by layer

| Layer | Command run | Result | Notes |
|---|---|---|---|
| Lint static analysis | `./gradlew :app:lintDebug` | PASS | 0 errors, 46 warnings (mostly `GradleDependency` "newer version available", `OldTargetApi`, `UnusedResources`, `UseKtx`) |
| Unit & integration | `./gradlew :app:testDebugUnitTest` | PASS | 29 tests, 0 failures, 0 errors |
| Coverage (JaCoCo) | - | SKIPPED | no JaCoCo plugin configured in the build |
| Instrumented/UI E2E | `./gradlew connectedDebugAndroidTest` | SKIPPED | no `androidTest` source set exists; replaced by the manual emulator smoke test below |
| Performance | - | SKIPPED | no macrobenchmark module |
| Security | manifest review + secret scan + dependency review | PASS | see findings |

### Emulator smoke test against the live VPS (manual, executed)

1. `adb install app-debug.apk` — Success.
2. App launched to the sign-in screen; email + password entered via the UI.
3. Tap "Sign in" -> VPS backend log shows `POST /auth/login HTTP/1.1" 200 OK`
   (via Caddy; also visible in Caddy's access log). App navigated to the map.
4. Permissions granted; tap "Start walk" -> app shows `Live` / `Stop walk`.
5. VPS backend log: `WebSocket /ws/walk?name=Ziggy" [accepted]`,
   `walker 62cd0335... connected (Ziggy)`.
6. Tap "Stop walk" -> VPS logs `walker 62cd0335... disconnected`.

The full app -> Caddy (TLS) -> backend -> WebSocket round trip works against
`wss://backend.thomasfeyens.be/ws/walk`.

## Failures

None in the layers that ran.

One operational note, not a code defect: a device that previously overrode the
backend URL in the in-app settings keeps that override — the new default only
applies to fresh installs. Observed live: the emulator still had
`wss://10.0.2.2:8443/ws/walk` persisted and retried against it until the app data
was cleared.

## Security findings

- Release merged manifest: no `usesCleartextTraffic`, no `networkSecurityConfig`,
  no `debuggable`. Exported components: only `MainActivity` (launcher, required)
  and the androidx `ProfileInstallReceiver`. Clean.
- Debug manifest: `debuggable=true`, cleartext + local-CA trust — debug-only by
  design, needed for the local emulator Caddy CA. Acceptable.
- Secret scan over tracked files (`git grep` for key/secret/password/token
  patterns, plus `.env`/`.pem`/`.jks`/keystore check): clean. No `.env` in git.
- Dependencies are current (OkHttp 4.12.0, MapLibre 12.0.0, AGP 8.13.0, Kotlin
  2.2.20, Compose BOM 2025.09.00). No known-vulnerable versions spotted by eye.
- Gap: no dependency-vulnerability scanner is wired into CI (no
  `dependencyCheck`/`owasp-dependency-check` task), so this review was manual.

## Coverage gaps

- No `androidTest` source set: zero instrumented/UI tests exist. The emulator
  smoke test above was manual and not repeatable in CI.
- No JaCoCo: line/branch coverage of the 29 unit tests is unknown.
- No benchmark module: startup, map-render and walk-loop performance unmeasured.
- Release variant only compiled, not installed/run on a device.
- Map tiles not exercised against `maps.thomasfeyens.be` because
  `assets/style.json` still points at `tile.openstreetmap.org`.

## Recommendations

1. Point `assets/style.json` at the self-hosted PMTiles on
   `https://maps.thomasfeyens.be` (removes the OSM tile-policy violation noted in
   HANDOFF.md:927 and makes the map work offline-ish from your own box).
2. Add an `androidTest` smoke test (launch -> sign in -> start walk -> assert
   connected) so the VPS path is covered in CI instead of by hand.
3. Enable JaCoCo (`jacoco` plugin + `jacocoDebugReport`) and publish coverage.
4. Add a dependency vulnerability scanner to CI.
5. Note: `thomasfeyens.be` has a wildcard A record — Caddy answers 308 for
   arbitrary subdomains (`vpn.`, `podgrab.` were seen in the access log). No site
   block matches them, so nothing is served, but if you do not want the box to
   answer at all, remove the wildcard or add a catch-all that 404s.

## Sign-off

The app builds, passes lint and its unit tests, and works end to end against the
production VPS backend from a real emulator session; release-blocking gaps are
the missing instrumented tests and the still-external map tile source, not the
backend wiring.
