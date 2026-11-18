# Installation Guide

## Prerequisites

- Docker Desktop with the compose plugin (backend stack: Redis, FastAPI, Caddy)
- JDK 17 or newer to run Gradle — on this machine that is the Android Studio JBR, `C:\Program Files\Android\Android Studio\jbr` (the `java` on `PATH` is JDK 8 and Gradle aborts on it). Set `JAVA_HOME` before every Gradle command.
- Android Studio with Android SDK 36 (`compileSdk = 36`, `minSdk = 26`, `targetSdk = 36` in `android/app/build.gradle.kts`)
- Gradle 9.5.1 via the wrapper (`android/gradlew` — no global Gradle needed); AGP 8.13.0, Kotlin 2.2.20
- Python 3.12 only if you run the backend without Docker (`backend/Dockerfile` ships `python:3.12-slim`)
- An Android emulator (AVD) or a physical device with USB debugging, for installing the app

## 1. Backend Setup

1. **Configure environment:** Create `backend/.env` (gitignored, no `.env.example` ships — the variables are documented in `backend/docker-compose.yml` and the README config table). For local development the defaults are enough; the only key that must be set before exposing the service anywhere is:

   ```bash
   # backend/.env
   MIKYWALK_ENV=production
   JWT_SECRET=<generate: python -c "import secrets; print(secrets.token_urlsafe(48))">
   PUBLIC_BASE_URL=https://your-host.example
   ALLOW_LOGGING_EMAIL=1        # only while EMAIL_SENDER=logging; reset codes then land in the logs
   ```

   `MIKYWALK_ENV` anything other than `development` makes `Settings.validate()` refuse to boot with the built-in dev `JWT_SECRET`.

2. **Start services:**

   ```bash
   cd backend
   docker compose up -d --build
   docker compose ps        # redis, redis-geo, backend and caddy healthy / running
   ```

   Ports: backend on `127.0.0.1:8000` (loopback only, by design), Redis on `127.0.0.1:6379` (loopback only, no password), Caddy on `8443` on all interfaces (emulator and phones reach it through here).

3. **Seed an operator and an invite:** accounts are invite-gated (`INVITE_REQUIRED=1` default), so before anyone can register:

   ```bash
   cd backend
   docker compose exec backend python scripts/create_admin.py --email you@example.com   # prompts for a password
   docker compose exec backend python scripts/mint_invites.py --count 3
   ```

   The admin panel is at `http://127.0.0.1:8000/admin/ui` (or `https://localhost:8443/admin/ui` through Caddy).

4. **Verify backend:**

   ```bash
   curl -s http://127.0.0.1:8000/health        # {"status":"ok","radius_m":500.0,"walker_ttl_seconds":30.0}
   curl -sk https://localhost:8443/health      # same answer through Caddy TLS
   ```

   **Without Docker:** `python -m venv .venv`, install `backend/requirements.txt` (`.venv/Scripts/python` on Windows, `source .venv/bin/activate` on macOS/Linux), provide a Redis at `REDIS_URL`, then `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` from `backend/`. Or `python dev_server.py` for an in-memory fakeredis backend (nothing persisted). Tests: `pip install -r requirements-dev.txt`, then `python -m pytest -q`; `python scripts/e2e.py` needs the Docker stack up.

## 2. Android App Setup

1. **Configure API endpoint:** The URL is baked in at build time as `DEFAULT_BACKEND_URL` in `android/app/build.gradle.kts` — currently `wss://backend.thomasfeyens.be/ws/walk` (the VPS, Let's Encrypt via system trust store). For local development against the Docker stack, edit it to:

   ```kotlin
   buildConfigField("String", "DEFAULT_BACKEND_URL", "\"wss://10.0.2.2:8443/ws/walk\"")
   ```

   `10.0.2.2` is the host machine as seen from the Android emulator. On a physical phone use the machine's LAN IP instead (`wss://192.168.1.20:8443/ws/walk`), add that IP to the site line and to `default_sni` in `backend/Caddyfile`, and restart Caddy. Once signed in, the URL can also be changed in-app from the settings sheet (gear icon) — but that screen is only reachable after a sign-in, so the first local sign-in needs the build-time edit above. Revert the edit before building for the VPS.

2. **Trust the local CA (debug builds only):**

   ```bash
   cd backend && ./scripts/export_ca.sh     # writes android/app/src/debug/res/raw/caddy_root.crt
   ```

   Only needed again if the `caddy-data` volume is wiped. The debug manifest (`android/app/src/debug/AndroidManifest.xml`) enables cleartext and trusts this CA plus user certs; release builds have neither and only trust the system store.

3. **Build & install:**

   ```bash
   cd android
   export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"   # Windows; any JDK 17+ elsewhere
   ./gradlew :app:assembleDebug
   ./gradlew :app:installDebug        # with an emulator running or a device attached
   ```

   APK lands in `android/app/build/outputs/apk/debug/app-debug.apk`. In Android Studio: open the `android/` folder, let it sync, select the `app` run configuration, run on your device/emulator. Release signing reads the gitignored `android/keystore.properties`; without it `assembleRelease` still produces `app-release-unsigned.apk`.

4. **Device permissions & network:** Grant on the emulator (a `pm clear` wipes them):

   ```bash
   adb shell pm grant com.mikywalk android.permission.ACCESS_FINE_LOCATION
   adb shell pm grant com.mikywalk android.permission.ACCESS_COARSE_LOCATION
   adb shell pm grant com.mikywalk android.permission.POST_NOTIFICATIONS
   ```

   The app needs fine location, notifications and foreground-service-location at runtime; it asks on first walk. Map tiles load from `https://maps.thomasfeyens.be/benelux.pmtiles` (absolute URL in `android/app/src/main/assets/style.json`), so even a local backend needs internet for the basemap.

5. **Verify app:** Start the emulator, set a location (`adb emu geo fix 4.8952 52.3702`), install, open the app, register with a minted invite code (or sign in as an account you created), tap **Start walk** — the status pill should read `Live` and `POST /auth/login` plus `WebSocket /ws/walk` appear in `docker compose logs backend`. Park a second walker with `python scripts/hold_walker.py --meters 60 --name Buddy` from `backend/` (with the venv active) to see a proximity alert and the marker. Stopping the walk adds an entry to the history dialog (clock icon).

## Troubleshooting (Top 3 Issues)

- **`Gradle requires JVM 17 or later`** — the `java` on `PATH` is too old. Export `JAVA_HOME` to a JDK 17+ (Android Studio's JBR) before running `./gradlew`.
- **`SSLHandshakeException` against `wss://10.0.2.2:8443` on the emulator** — the debug APK doesn't trust Caddy's local CA. Run `backend/scripts/export_ca.sh` and rebuild; also confirm `default_sni 10.0.2.2` is in `backend/Caddyfile` (an IP-literal host sends no SNI, and without `default_sni` every handshake fails).
- **Sign-in against the local stack fails** — the committed `DEFAULT_BACKEND_URL` points at the VPS and local accounts only exist in the local Redis. Edit `DEFAULT_BACKEND_URL` in `android/app/build.gradle.kts` to `wss://10.0.2.2:8443/ws/walk`, rebuild, sign in, then revert. Also note the backend image has no bind mount: after editing backend Python or templates, run `docker compose up -d --build`, not just a restart.
