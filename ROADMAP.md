# Roadmap — DogWalk

What to build next, in priority order. `HANDOFF.md` covers how to get the stack
running and what is already verified; this file is the forward-looking half.

Last updated at the end of the admin-panel session.

## Start here tomorrow

```bash
# 1. Docker Desktop must be running first
cd /c/Users/thoma/Downloads/dog/backend
docker compose up -d --build
curl -s http://127.0.0.1:8000/health

# 2. The admin panel needs no build — just open it
start http://127.0.0.1:8000/admin/ui     # operator@example.com / operator-password

# 3. Emulator, then the app
cd /c/Users/thoma/Downloads/dog/android
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
./gradlew :app:installDebug

# 4. Confirm nothing regressed
cd /c/Users/thoma/Downloads/dog/backend
./.venv/Scripts/python.exe -m pytest -q          # expect 207 passed
./.venv/Scripts/python.exe scripts/e2e.py        # expect 56/56
```

**The admin panel is committed** as `e5ed2ad`, and the tree is clean. See
**What landed in `e5ed2ad`** in `HANDOFF.md` for what that commit contains.

The app is signed in as `fresh@example.com` / `hunter2hunter2` with dog `Pickle`.
`emma@example.com` also uses `hunter2hunter2`. `buddy@example.com` was created by
`scripts/hold_walker.py` and uses the script password `script-walker-password`.
The two `e2e-*@example.com` accounts are created by `scripts/e2e.py` on each run.

## Where we are

P0 and P1 are done. The app is safe to put in front of friends and family on a
trusted network, which is the launch scope we settled on.

| P0 item | Status |
| --- | --- |
| No authentication — anyone could harvest every nearby walker's position | Accounts + JWT, walker id derived from the token |
| No TLS — `ws://` with `usesCleartextTraffic="true"` | `wss://` via Caddy, cleartext exemption is debug-only |
| Release build unsigned | `signingConfig` reads a gitignored `keystore.properties` |

| P1 item | Status |
| --- | --- |
| Exact coordinates broadcast to strangers | Distance + bearing only, quantised. No absolute position leaves the server |
| Tokens in plain `SharedPreferences` | Keystore-backed AES-GCM, `allowBackup="false"` |
| No password reset | 6-digit code flow, pluggable sender that logs in development |
| No account deletion | `DELETE /auth/account` + confirmation dialog |
| `JWT_SECRET` falls back to a known dev value | Hard `RuntimeError` at startup outside development |
| No rate limiting on auth | Per-IP fixed-window limiter, `X-Forwarded-For` overwritten by Caddy |
| `POST /auth/register` leaked whether an address had an account | Single-use invite codes, checked before the email lookup |

## P1 — done this session

Kept here as a record of what was decided and why, because two of these were
product decisions rather than engineering ones and the reasoning should not be
lost.

### 1. Exact coordinates were broadcast to strangers — done

Every `PROXIMITY_ALERT` used to carry the full-precision `lat`/`lon` of every
walker inside 500 m. For a dog-walking app — often used by people walking alone,
often at night — that was the single biggest risk in the product.

**Decision: distance and bearing, not coordinates.** The client draws the marker
relative to itself. `app/privacy.py` holds the whole policy: `bearing_degrees()`,
`quantise_distance()` (25 m), `quantise_bearing()` (30°). `NearbyWalker` is now
`id, name, distance_m, bearing_deg` and there is no field in the protocol that can
leak an absolute position, so a modified client cannot ask for one.

The quanta are env vars (`DISTANCE_QUANTUM_M`, `BEARING_QUANTUM_DEG`) because they
are a trade-off, not a constant: tighter means a more accurate map and a more
identifiable walker. They move together.

Residual risk, documented in `README.md`: a client that walks around and collects
many bearings to the same walker can trilaterate them. That is inherent to any
proximity feature. The quanta raise the cost; they do not remove it.

### 2. Tokens sat in plain `SharedPreferences` — done

`dogwalk_auth` held a 30-day refresh token in cleartext, and `allowBackup="true"`
meant Android Auto Backup swept it into the user's Google account.

`SecurePrefs` now encrypts each value with a Keystore-backed AES-GCM key
(`AES/GCM/NoPadding`, 256-bit, alias `dogwalk_auth_key`) and stores `iv:ciphertext`
base64. A value that fails to decrypt is dropped rather than crashing the app.
`EncryptedSharedPreferences` was the obvious choice but is deprecated in
`androidx.security`. Manifest now sets `allowBackup="false"` and
`fullBackupContent="false"`.

### 3. No password reset — done

**Decision: build it, log the email.** Full reset-token flow with a pluggable
sender. `app/email.py` defines an `EmailSender` protocol, a `LoggingEmailSender`
that writes the code to the backend log, and a `build_sender` factory that
`EMAIL_SENDER` selects from. A real `SmtpEmailSender` now sits behind
`EMAIL_SENDER=smtp`, so going live is credentials rather than code.

Codes are 6 digits, stored as a sha256 key, valid 15 minutes, single use, capped
at 10 attempts. `POST /auth/password-reset/request` returns 202 whether or not the
address exists, so it cannot be used to enumerate accounts. Confirming bumps
`token_version`, which invalidates every access token already in flight.

### 4. No account deletion — done

`DELETE /auth/account` needs the password, removes `dogwalk:users:<id>`, the email
index entry, the refresh index and every used-refresh marker, and closes any live
socket through `hub.close()`. Settings has a confirmation dialog that requires the
password again.

### 5. `JWT_SECRET` silently fell back to a known dev value — done

`Settings.validate()` runs at `create_app` time and raises `RuntimeError` when the
secret is still the built-in one and `DOGWALK_ENV` is not a development
environment, or when the secret is shorter than 32 bytes. A warning is not enough:
a deployment that logs a warning and carries on is a deployment where anyone who
has read this repository can mint a token for any account.

### 6. No rate limiting on the auth endpoints — done

`app/ratelimit.py` is a fixed-window per-IP limiter with a `LIMITS` table, keyed on
`X-Forwarded-For`. `Caddyfile` **overwrites** that header with `{remote_host}`
rather than appending, so a client cannot spoof its own address. Register is capped
at 20/hour. `RATE_LIMIT_ENABLED=0` turns it off for tests that need to drive the
per-email lockout without tripping the IP limit first.

## P1.5 — friend groups, done

Tap a nearby dog's name tag to mark it **liked** (green marker, quiet chime) or
**disliked** (red marker, alarm). Unmarked dogs stay amber with the original sound.

**Decision: the relationship never leaves the device.** It lives in
`SharedPreferences` under `marked_dogs` as `{id, name, relationship}`. The server
has no idea who likes whom, so the social graph cannot leak from Redis even if the
store were dumped. The whole feature needed zero backend change, and the backend
test count is unchanged at 71.

Consequences worth remembering:

- Three proximity notification channels, because Android fixes a channel's sound at
  creation and ignores later changes. The friend and disliked tones are two small
  generated WAVs in `res/raw` (`friend_chime.wav`, `disliked_alarm.wav`); the
  unmarked case keeps the system notification tone. `RingtoneManager` was not
  enough here — it exposes one notification tone and one alarm tone, so friends
  would have sounded identical to an unmarked dog.
- Three GeoJSON sources and layers rather than one with a data-driven `match`
  colour expression. More code, but a subtly-wrong expression fails silently and
  this project has already lost time to MapLibre features that do not behave as
  documented.
- `WalkRepository` now emits only walkers that *just* arrived inside the warning
  distance. Re-emitting the full set would replay the disliked alarm every time an
  unrelated friend appeared.
- Urgency when several relationships are present at once is
  **disliked > unmarked > friend**, as an explicit `urgency()` function. Enum
  ordinal order would have been wrong.

Verified on `emulator-5554` with `scripts/hold_walker.py --meters 60 --name Buddy`:
amber → neutral channel, green → `dogwalk_proximity_friend`, red →
`dogwalk_proximity_disliked` with `alarm_alert`, remove → back to amber and the
neutral channel. Marker colours checked by sampling screenshot pixels, not by eye.

## P1.6 — admin backend, done

A web panel at `/admin/ui` for accounts and invite codes, plus a JSON API under
`/admin`. Scope was held to user and invite management on purpose: no live
monitoring, and nothing that reads the geo index.

**Decision: admin tokens are a third JWT type, not a role claim.** A `role` field
on a walker's access token would mean a stolen walker token is one edited claim
away from operator rights. `typ: "admin"` means `decode_token` refuses it in both
directions and there is no claim to forge. `test_admin.py` asserts both
directions, including that an admin token is closed with `4401` on the walk
socket.

**Decision: the account list pages over an index, not a scan.** `SCAN` cannot
give a stable page and gets slower as the keyspace grows, so registration now
`ZADD`s to `dogwalk:users:created` with a float score — two accounts created in
the same second still order correctly. Accounts registered before this change
need `scripts/create_admin.py --backfill` once.

**Decision: no JavaScript.** The pages are server-rendered Jinja with form posts
and 303 redirects. The repository has no Node toolchain, and a React build step
would be the largest new dependency in the project for five screens. Minted codes
come back through a short-lived Redis key rather than a query string, because a
query string lands in the proxy access log and those are secrets.

Two new runtime dependencies: `jinja2`, and `python-multipart` (which FastAPI
requires for form posts). Test count went from 71 to 109.

**Added later: creating accounts from the panel.** The Accounts page has an
"Add an account" form, and `POST /admin/users` does the same over the API. No
invite code is spent: the code gates self-registration by a stranger, and an
operator is not one. Test count went from 109 to 123.

**Then: generated passwords and a forced change.** The operator no longer picks
the password — the server generates 16 characters and shows them once. Signing
in with it returns `must_change_password` and a `pwchange` token instead of a
session; that token is a fourth JWT `typ`, so `decode_token` refuses it
everywhere except `/auth/password/change`. The account cannot walk, refresh or
sign out until it has chosen its own password. The alternative — trusting the
client to show a change screen — would have left the handed-over password
working, which defeats the point of generating one. Test count went from 123 to
129; the Android client grew a second step on the login screen to match.

## Deferred — deliberately, per the friends-and-family scope

One P1 item is left undone. It is not forgotten; it is gated on a decision to go
public.

### 7. ~~Production TLS is still the local CA~~ — done

Closed on 2026-11-14. The VPS serves `backend.thomasfeyens.be` and
`maps.thomasfeyens.be` from Let's Encrypt, issued and renewed by Caddy itself.
`deploy/Caddyfile` carries no `tls internal` and no `default_sni`, because a real
hostname sends SNI and the workaround is unnecessary. `deploy/verify.sh` asserts
the issuer, so a regression to the local CA fails a check instead of waiting to
be noticed.

The local `backend/Caddyfile` still uses `tls internal` on `:8443`. That is the
emulator's path to `wss://10.0.2.2:8443` and is meant to stay.

### 8. The backend is single-instance

The connection hub lives in process memory (`app/hub.py`). Consequences: a restart
drops every walker mid-walk, and you cannot run two replicas because a walker
connected to replica A is invisible to replica B.

Fix: Redis pub/sub to fan out frames between instances. This is a real piece of
work and only matters once you have enough traffic to need a second replica — but
it is worth knowing it is a wall, not a tuning knob.

**Trigger to do this:** more than ~200 concurrent walkers, or the first time a
deploy mid-evening annoys someone.

## P2 — quality, before or shortly after launch

- **Android tests cover four classes.** 33 tests: `WalkSocketClient`,
  `WalkRepository`, `AuthStore` and `HttpBase`. `WalkRepository` was the valuable
  one and is done — it runs on three interfaces (`WalkSettings`, `WalkTransport`,
  `TokenRefresher`), so it needs no `Context`. `SettingsStore`,
  `NotificationHelper` and the Compose UI still have no tests at all.
- **CI is green but cannot gate anything.** `.github/workflows/ci.yml` runs all
  three jobs on every push and passed on `9bd0114` (134 backend tests, 56/56 e2e,
  Android tests + lint). The suite has since grown to 156 backend tests and 33
  Android tests as of the 2026-11-14 doc sync. But `main` has no branch
  protection: GitHub refuses it on a private repo without Pro (`403 Upgrade to
  GitHub Pro`), so a red run does not block a merge. Nothing deploys either — the
  VPS pulls by hand. See **CI → Where it runs** in `HANDOFF.md`.
- **Reconnect backoff is untested.** The client backs off 1 s → 30 s, but nobody
  has run it against a backend that stays down for minutes. Worth a manual pass
  and then a test.
- **The proximity alert goes stale when one of several walkers leaves.** It is
  withdrawn correctly once the last one is outside the warning distance, but if a
  disliked dog walks away while a friend stays, the notification keeps naming the
  dog that left. Re-posting the content without re-sounding it needs a path that
  `setOnlyAlertOnce(true)` cannot provide, because that would also silence the
  arrival the urgency ordering depends on.
- **Battery.** `PRIORITY_HIGH_ACCURACY` every 5 s for the length of a walk is
  heavy. Consider `BALANCED` when the walker is stationary, or a longer interval
  once the first fix lands.
- **No accessibility pass.** Content descriptions, touch target sizes and
  TalkBack have not been checked.
- **No `/me` endpoint.** The app trusts the email it stored at login. If the
  account changes server-side, the UI shows stale data until the next sign-in.
- **`POST /auth/register` still answers 409 for a taken email**, but only after a
  valid invite code has been spent, so an uninvited caller learns nothing. Anyone
  holding a code can still probe, and a leaked code is a leaked invitation. Email
  verification is the general fix and would let the gate come off.
- **Reset codes still go to the log on the VPS.** The sender is written —
  `SmtpEmailSender` behind `EMAIL_SENDER=smtp` — and the admin panel has an
  **Email** page that saves a configuration, rebuilds the sender without a restart
  and sends a test message first. But no provider account exists yet, so the
  deployment runs `EMAIL_SENDER=logging` with `ALLOW_LOGGING_EMAIL=1` and
  `Settings.validate()` lets it through. Until someone fills in that page, the
  acknowledgement is the only thing standing between a log reader and any account.
- **`POST /blocks/{id}` is capped and throttled, not validated.** The route takes
  any 32-hex id and writes two keys, and an id that never belonged to an account
  is cleaned up by nothing except an unblock — so the list is capped at
  `blocks.MAX_BLOCKS` (200, answered as 409) and the prefix is throttled at
  60/minute. The cap is deliberately not an existence check: a 404 would be an
  account oracle, and skipping the write for an unknown id would leak the same
  thing through `GET /blocks`. The ceiling check is not atomic with the write, so
  two concurrent blocks can overshoot by one.

## P3 — product ideas, not commitments

- Walk history and stats (distance, duration, dogs met).
- ~~Block or mute a specific walker.~~ **Done** — and done the way that makes
  someone feel safe: the filter is server-side, in `refresh_neighborhood`, so a
  blocked walker never receives the bearing at all. Invisibility is symmetric
  (blocking hides you from them too), the list lives in Redis so it survives a
  reinstall, and `GET`/`POST`/`DELETE /blocks` is the API. Block from the
  tap-a-dog dialog, unblock from settings. See **Blocking** in `HANDOFF.md`.
- Report a walker.
- Multiple dogs per account.
- ~~Replace the raster OSM style with a proper vector style.~~ **Done** — the
  style is now a glyph-free vector style over the self-hosted PMTiles on
  `maps.thomasfeyens.be`. The Compose-`Text` label workaround stays, because it
  is the glyph problem that forced it, not the raster source; see `HANDOFF.md`.

## Suggested next session

**The map is off third-party tiles.** `assets/style.json` is now a vector style
over the self-hosted PMTiles on `maps.thomasfeyens.be`, verified on the emulator
at street level. MapLibre Native reads PMTiles directly, so nothing new runs on
the VPS. The style has no symbol layers, so it needs no glyph server and the
Compose label overlays are unchanged.

CI is in. `.github/workflows/ci.yml` runs `pytest`, `testDebugUnitTest` and
`lintDebug`, and `scripts/e2e.py` against real Redis. Turning lint on found and
fixed one real error. The open question is the runner — see **CI → Where it runs**
in `HANDOFF.md`.

**`WalkRepository` tests** landed. The repository now runs on three interfaces —
`WalkSettings`, `WalkTransport` and `TokenRefresher`, each bound to its real
class in `AppModule` — and ten tests pin the proximity bookkeeping (arrive once,
not per fix; withdraw when the last dog leaves) and the three session-renewal
branches (refresh, refusal, no token).

**Blocking a walker is in and deployed.** The one social signal that could not
stay on the device: `app/blocks.py` keeps a hash of who you blocked and a set of
who blocked you, `refresh_neighborhood` drops the union before building the
frame, and `GET`/`POST`/`DELETE /blocks` drives it. Symmetric on purpose —
blocking hides you from them as well — and it survives a reinstall because it
lives in Redis. Verified on the emulator, including after `pm clear`, and on the
VPS over `wss://backend.thomasfeyens.be` with two real sockets: both visible →
`POST /blocks` 204 → both `PROXIMITY_CLEAR` → `DELETE` 204 → both visible again.
`deploy/verify.sh` passes 12/12 on the box.

**The docs were resynced on 2026-11-14.** Seven claims had drifted from the code:
production TLS was recorded as still on the local CA, blocking was recorded as
not existing, the admin panel was recorded as having no second gate, and four
test counts were stale. All corrected against a full gate run — 156 backend
tests, 56/56 e2e, 33 Android tests, 0 lint errors, 12/12 on the VPS.

The next visible thing is **walk history and stats** (P3): distance, duration and
dogs met are all derivable from what the app already receives, and it is the
first feature that gives a walk a reason to end. **Report a walker** is the other
half of the safety story and would reuse the block plumbing — a report is a block
plus a row an operator can read.

## Decisions I would like from you

All three from last session are answered and implemented:

1. ~~Coordinate precision~~ → **distance and bearing only**, quantised to 25 m / 30°.
2. ~~Email sending~~ → **build the flow, log the email in development**. Swap in a
   provider when you have one.
3. ~~Launch scope~~ → **friends and family on a trusted network**. P1.8 deferred
   with an explicit trigger above; P1.7 is now done.

New ones, none of which block anything today:

1. ~~**Email provider**~~ → **SMTP, provider-agnostic.** `SmtpEmailSender` speaks
   plain SMTP, so Resend, Postmark, Mailgun, Fastmail or a local relay all work
   without a code change. What is left is picking one and pasting the credentials
   into the panel's **Email** page.
2. **Do you want email verification?** The enumeration gap is closed by invite
   codes, so this is no longer a security fix — it is what would let the gate come
   off and the app open to strangers. It adds a second email flow and a "not yet
   verified" state to the UI.
