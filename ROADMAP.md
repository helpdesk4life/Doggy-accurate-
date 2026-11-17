# Roadmap — DogWalk

What to build next, in priority order. `HANDOFF.md` covers how to get the stack
running and what is already verified; this file is the forward-looking half.

Last updated 2026-11-16 (see **Session log — 2026-11-16** in `HANDOFF.md`).

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
./.venv/Scripts/python.exe -m pytest -q          # expect 235 passed
./.venv/Scripts/python.exe scripts/e2e.py        # expect 56/56
```

**The tree is clean**, HEAD `99b95bc` — PR #13 (`0906161`, dependency scanners:
pip-audit + OSV-Scanner, docs on top), then the three Android PRs merged in
order on 2026-11-16: #14 (`7a2803f`, alert refresh), #16 (`f6ca3d8`, urgency
ranks who is present), #15 (`1ea8141`, GPS sleeps while stationary). The same
day the VPS went live on SMTP, the three leaked-or-stale secrets were rotated
(JWT, admin password, Caddy gate) and the panel login was recorded in the
secrets file — see **Session log — 2026-11-16** in `HANDOFF.md`, **What landed
in `e5ed2ad`** for the panel, and **Blocking** for the block store. Earlier
layers: the pip floor bumps (#2–#6) and walk history (`c9313d1`).

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
| No password reset | 6-digit code flow, pluggable sender: SMTP, or the log in development |
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

## P1.7 — account requests, done

The invite gate is the right answer for "may a stranger register?", but it needs
someone to hand out a code. This adds the self-service half: the app's login
screen has **Request an account**, the request waits at `/admin/ui/requests`, and
an operator approves or rejects it.

The endpoint answers 202 for every address, for the same reason the invite gate
exists — a different answer would say whether an address is registered. Approval
creates the account with a password that is thrown away, so the requester sets
their own through "Forgot password"; no credential travels by email.

Not done, and worth deciding: the request carries no note, so the operator sees
an address and nothing else. A short "who are you" field would make the decision
easier for any address you do not recognise.

## Security review — done (2026-11-14)

A pass over the deployment looking for gaps the P0/P1 tables do not cover. Two of
them needed code; both are closed.

**Reset codes could not leave the log.** `app/email.py` had a `LoggingEmailSender`
and a protocol, but no real sender, so the only way a reset code reached a user
was `docker compose logs backend`. `SmtpEmailSender` now speaks plain SMTP on a
worker thread (`asyncio.to_thread`), so a slow provider cannot stall the event
loop, and a failed send is logged rather than raised — a 500 for a known address
and a 202 for an unknown one would be the enumeration oracle the endpoint exists
to avoid. `Settings.validate()` refuses to boot on an incomplete configuration,
an unknown security mode, or a password over an unencrypted connection.

**The SMTP configuration is editable from the panel.** `/admin/ui/smtp` stores a
configuration in `dogwalk:settings:smtp`, rebuilds the running sender in place,
and sends a test message through the values on screen before you commit to them.
A saved configuration wins over `EMAIL_SENDER`, which is what makes the page
usable on a deployment that still says `EMAIL_SENDER=logging`. The password is
write-only: never returned by the API, never in the template context, never in
the audit log. The same pass closed a smaller bug — reset codes were indexed per
email but never removed, so `consume_reset_code` and `delete_user` now drain
`dogwalk:reset:index:<email>`.

**`POST /blocks/{id}` was unbounded.** It accepted any 32-hex id and wrote two
Redis keys per call, and an id that never belonged to an account is cleaned up by
nothing except an unblock. The list is now capped at `blocks.MAX_BLOCKS` (200,
answered 409) and the route is throttled at 60/minute on the `/blocks/` prefix.
The cap is deliberately not an existence check — the P2 bullet below has the
argument for why a 404 would have been the wrong fix.

Test count went from 129 to 207 across the sessions since the panel, 43 of them
in `test_email.py` and `test_smtp_settings.py`. `deploy/verify.sh` passes 16/16
on the VPS.

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

- **Android tests cover five areas, seven now.** 66 tests: `WalkSocketClient`,
  `WalkRepository`, `AuthStore`, `HttpBase`, `AuthApi`, and now the walk history
  (`WalkStatsRecorder`, `WalkHistoryStore`), `MovementDetector` and
  `ProximityAlertPolicy`. `WalkRepository` was the valuable
  one and is done — it runs on its seams (`WalkSettings`, `WalkTransport`,
  `TokenRefresher`, `WalkHistoryStore`), so it needs no `Context`. `SettingsStore`,
  the notification-posting code and the Compose UI still have no tests at all
  (the alert rule that drove the posting *is* tested, as `ProximityAlertPolicy`).
- **CI is green but cannot gate anything.** `.github/workflows/ci.yml` runs all
  three jobs on every push and passed on `9bd0114` (134 backend tests, 56/56 e2e,
  Android tests + lint). The suite has since grown to 207 backend tests and 33
  Android tests as of the 2026-11-14 doc sync. But `main` has no branch
  protection: GitHub refuses it on a private repo without Pro (`403 Upgrade to
  GitHub Pro`), so a red run does not block a merge. Nothing deploys either — the
  VPS pulls by hand. See **CI → Where it runs** in `HANDOFF.md`.
- ~~**Reconnect backoff is untested.**~~ Closed 2026-11-17. The ladder is now
  driven for real against a backend that keeps failing: three attempts in 4.5 s
  with gaps of about 1 s then 2 s, which is the arithmetic actually wired up
  rather than asserted in isolation. Writing it found a bug — `reconnect()` did
  not cancel a retry that was already waiting out its backoff, so a token
  refresh could be followed by a stale job that cancelled the fresh socket and
  dialled a third time. Fixed, with a test that fails without the fix.
- ~~**The proximity alert goes stale when one of several walkers leaves.**~~
  Closed: the alert is rebuilt from the whole set inside the warning distance on
  every change, not just arrivals. One `ProximityEvent.Changed` carries everyone
  still inside, the text names the most urgent walker present, and
  `ProximityAlertPolicy` owns the sound — re-sound only when the most urgent
  walker present is more urgent than the one the user was last told about. That
  is also what stops a friend arriving next to a warning from replacing it with a
  chime. A quiet rebuild returns early when the slot is empty, so it cannot bring
  back an alert the user dismissed.
- ~~**Battery.** `PRIORITY_HIGH_ACCURACY` every 5 s for the length of a walk is
  heavy.~~ Closed: `MovementDetector` classifies each fix as moving or standing
  still, and the service swaps cadence on the transition — continuous
  high-accuracy updates every 5 s while moving, one `getCurrentLocation` probe
  every 10 s while stationary, so the radio sleeps between probes. `BALANCED` is
  deliberately not used: a balanced fix is accurate to roughly 100 m, which is the
  whole warning distance. The 10 s probe is a third of the server's 30 s
  `WALKER_TTL_SECONDS`, so a stationary walker still refreshes their position and
  stays on everyone else's map. Not yet measured on a device.
- **No accessibility pass.** Content descriptions, touch target sizes and
  TalkBack have not been checked.
- **No `/me` endpoint.** The app trusts the email it stored at login. If the
  account changes server-side, the UI shows stale data until the next sign-in.
- **`POST /auth/register` still answers 409 for a taken email**, but only after a
  valid invite code has been spent, so an uninvited caller learns nothing. Anyone
  holding a code can still probe, and a leaked code is a leaked invitation. Email
  verification is the general fix and would let the gate come off.
- ~~**Reset codes still go to the log on the VPS.**~~ Closed: SMTP is configured on
  the box — Gmail over port 465 SSL, the only outbound SMTP port the VPS opens —
  in both places the design supports: the panel's Redis slot and the `.env`
  (`EMAIL_SENDER=smtp`, `ALLOW_LOGGING_EMAIL` removed, so `validate()` now refuses
  a flip back to the logging sender). Verified end to end: the test message
  arrived, and a real reset request produced a code that appears nowhere in
  `docker compose logs backend`. See **Session log — 2026-11-16** in `HANDOFF.md`.
- **`POST /blocks/{id}` is capped and throttled, not validated.** The route takes
  any 32-hex id and writes two keys, and an id that never belonged to an account
  is cleaned up by nothing except an unblock — so the list is capped at
  `blocks.MAX_BLOCKS` (200, answered as 409) and the prefix is throttled at
  60/minute. The cap is deliberately not an existence check: a 404 would be an
  account oracle, and skipping the write for an unknown id would leak the same
  thing through `GET /blocks`. The ceiling check is not atomic with the write, so
  two concurrent blocks can overshoot by one.

## P3 — product ideas, not commitments

- ~~Walk history and stats (distance, duration, dogs met).~~ **Done** — client-side
  only: `WalkStatsRecorder` accumulates the walk from the app's own GPS fixes and
  the arrivals it already receives, and `PrefsWalkHistoryStore` keeps the finished
  summaries (aggregates, never a route) in SharedPreferences, capped at 100. The
  history dialog sits behind the clock icon on the map screen. See **Walk history**
  in `HANDOFF.md`.
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
`deploy/verify.sh` passes 16/16 on the box.

**The docs were resynced twice on 2026-11-14.** The first pass fixed seven claims
that had drifted from the code: production TLS was recorded as still on the local
CA, blocking was recorded as not existing, the admin panel was recorded as having
no second gate, and four test counts were stale. The second pass followed the
security review and added the blocks API to `README.md`, which had never
documented it, plus the two email test files. Current gate: 235 backend tests,
56/56 e2e, 66 Android tests, 0 lint errors, 16/16 on the VPS.

**Walk history and stats landed** (P3): distance, duration and dogs met, all
derived client-side from what the app already receives — the first feature that
gives a walk a reason to end.

**Report a walker — decided against, 2026-11-17.** It was the proposed next
visible thing: the other half of the safety story, reusing the block plumbing as
a block plus a row an operator can read. It is not being built. A report is a
claim, not evidence — the server keeps no coordinates and no history, so an
operator could never verify one. Making reports actionable would mean retaining
evidence, and that is the privacy design the app is built on. Blocking stays the
whole safety story: you can make someone invisible to you, and that is all the
server will ever be asked to do about a person.

## Decisions I would like from you

All three from last session are answered and implemented:

1. ~~Coordinate precision~~ → **distance and bearing only**, quantised to 25 m / 30°.
2. ~~Email sending~~ → **build the flow, log the email in development**. Swap in a
   provider when you have one.
3. ~~Launch scope~~ → **friends and family on a trusted network**. P1.8 deferred
   with an explicit trigger above; P1.7 is now done.

New ones, none of which block anything today:

1. ~~**Email provider**~~ → **Gmail, configured.** An app password is live on the
   VPS over port 465 SSL, verified end to end. The sender stays
   provider-agnostic: if Gmail's relay limits ever bite, Resend, Postmark,
   Mailgun or Fastmail are a paste away with no code change.
2. **Do you want email verification?** The enumeration gap is closed by invite
   codes, so this is no longer a security fix — it is what would let the gate come
   off and the app open to strangers. It adds a second email flow and a "not yet
   verified" state to the UI.
