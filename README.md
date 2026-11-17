# DogWalk

A dog-walking app that tells you when another walker comes within 500 m.

Two halves:

- `android/` — Kotlin, Jetpack Compose, Hilt, MVVM, MapLibre, a foreground GPS service and an OkHttp WebSocket client.
- `backend/` — FastAPI over WebSockets, Redis GEO for the proximity maths, Dockerised, with Caddy terminating TLS.

See `HANDOFF.md` for current state, verification commands and known gaps,
`ROADMAP.md` for what to build next, `IOS-PORT.md` for the plan to bring this
to iPhone, and `UPGRADING.md` for how to bump dependencies safely. `ios/` holds the first slice of that port — the background location
and socket layer, which is the part that decides whether the product works.

```
┌──────────────────────────┐      wss://…/ws/walk        ┌────────────────────┐
│  Android                 │  ── location frames ──────▶  │  Caddy (TLS)       │
│                          │                              │        │           │
│  LoginScreen → AuthStore │  ◀── PROXIMITY_ALERT ─────   │        ▼           │
│  LocationTrackingService │      PROXIMITY_CLEAR         │  FastAPI           │
│    FusedLocation 5 s     │                              │    Hub             │
│         │                │                              │    proximity.py    │
│         ▼                │                              │        │           │
│  WalkRepository ─────────┼── WalkSocketClient           │        ▼           │
│         │                │   (Bearer token)             │  Redis GEOADD      │
│         ▼                │                              │  GEOSEARCH 500 m   │
│  MapViewModel → MapScreen│                              │  sweeper 30 s TTL  │
│    MapLibre + markers    │                              └────────────────────┘
└──────────────────────────┘
```

## Backend

### With Docker (Redis and Caddy included)

```bash
cd backend
docker compose up --build
```

Backend on `http://127.0.0.1:8000` (loopback only), Redis on `localhost:6379`,
TLS on `https://localhost:8443`.

### Without Docker

Needs a Redis reachable at `REDIS_URL` (default `redis://localhost:6379/0`).

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`GET /health` returns the active radius and TTL.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOGWALK_ENV` | `development` | Deployment name. Anything outside `development`/`dev`/`local`/`test` gets the strict startup checks |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `PROXIMITY_RADIUS_M` | `500` | Alert radius in metres |
| `WALKER_TTL_SECONDS` | `30` | A walker with no fix for this long is dropped |
| `SWEEP_INTERVAL_SECONDS` | `5` | How often the sweeper runs |
| `DISTANCE_QUANTUM_M` | `25` | Grid that `distance_m` is snapped to before it leaves the server |
| `BEARING_QUANTUM_DEG` | `30` | Grid that `bearing_deg` is snapped to |
| `JWT_SECRET` | dev value | Signing key for access and refresh tokens |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | Access token lifetime |
| `REFRESH_TOKEN_TTL_SECONDS` | `2592000` | Refresh token lifetime (30 days) |
| `ADMIN_TOKEN_TTL_SECONDS` | `28800` | Admin session lifetime (8 hours, no refresh) |
| `PASSWORD_CHANGE_TOKEN_TTL_SECONDS` | `900` | Lifetime of the ticket that forces a password change |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Base URL used to build the link in a password-reset email |
| `RATE_LIMIT_ENABLED` | `1` | Per-IP throttling on the auth endpoints and `/blocks/{id}` |
| `INVITE_REQUIRED` | `1` | Registration needs a single-use code |
| `EMAIL_SENDER` | `logging` | Which `EmailSender` to install: `logging` or `smtp` |
| `ALLOW_LOGGING_EMAIL` | `0` | Acknowledges that reset codes go to the log, outside development |
| `SMTP_HOST` | — | SMTP server. Required when `EMAIL_SENDER=smtp` |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USERNAME` | — | SMTP login. Leave empty for a relay that needs no auth |
| `SMTP_PASSWORD` | — | SMTP password or provider API key |
| `SMTP_FROM` | — | Envelope sender. Required when `EMAIL_SENDER=smtp` |
| `SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (465) or `none` (a relay on this host) |

`JWT_SECRET` has a built-in development default, and `Settings.validate()` runs at
`create_app` time. Outside development the backend **refuses to boot** if the
secret is still the built-in one or is shorter than 32 bytes — a warning is not
enough, because a deployment that logs a warning and carries on is a deployment
where anyone who has read this repository can mint a token for any account. Set
`DOGWALK_ENV=development` if the machine really is a development machine.

`DISTANCE_QUANTUM_M` and `BEARING_QUANTUM_DEG` are the privacy budget for
proximity alerts; see [Privacy](#privacy). Setting either to `0` disables
quantisation and broadcasts positions to within a few metres — development only.

`EMAIL_SENDER` picks the sender. `logging` writes the whole message — including a
password-reset code — to the backend log, and outside development the backend
**refuses to boot** with it, for the same reason as the secret: a reset code in a
log is a working account takeover for anyone who can read that log. Set
`ALLOW_LOGGING_EMAIL=1` to accept that on a box whose logs nobody else can read.

`smtp` is the real one, and it works with any provider that speaks SMTP — Resend,
Postmark, Mailgun, Fastmail, a local relay — so there is no provider-specific
code to write:

```bash
EMAIL_SENDER=smtp
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USERNAME=resend
SMTP_PASSWORD=re_your_api_key
SMTP_FROM=DogWalk <no-reply@yourdomain.example>
SMTP_SECURITY=starttls
```

`Settings.validate()` refuses to boot if `SMTP_HOST` or `SMTP_FROM` is empty, if
`SMTP_SECURITY` is not one of the three modes, or if a password is set while
`SMTP_SECURITY=none` — that would put the password on the wire in the clear. The
send itself runs on a worker thread, so a slow provider cannot stall the event
loop, and a failed send is logged rather than raised: a 500 for a known address
and a 202 for an unknown one would be the account enumeration oracle the endpoint
is built to avoid.

**Or configure it in the panel instead.** `/admin/ui/smtp` has the same fields
plus a "Send test" button that posts one message through the values on screen
without saving them. Saving stores the configuration in Redis and rebuilds the
sender in place, so the next reset email goes through the new server with no
restart and no `.env` edit. A saved configuration wins over `EMAIL_SENDER`, which
means the panel works on a deployment that still says `EMAIL_SENDER=logging`.
Keep `ALLOW_LOGGING_EMAIL=1` there: `Settings.validate()` reads only the
environment, so it cannot see a configuration saved in the panel and would still
refuse to boot without the acknowledgement. The password is write-only: it is
never returned by the API, never rendered into the page, and never written to the
audit log. "Forget this configuration" drops it and falls back to the
environment.

### Tests

```bash
cd backend
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
```

235 tests. They run against `fakeredis`, so no Redis server is needed.

| File | Covers |
| --- | --- |
| `test_auth.py` | Registration, duplicate email, password rules, login, lockout, refresh rotation, revocation, token tampering |
| `test_proximity.py` | Alert round trip, leaving the radius, disconnect cleanup, frame validation, the 30 s expiry sweep, the GEO query, and that alerts carry no coordinates |
| `test_privacy.py` | Bearing cardinal directions, quantisation snapping, the error bound, the zero-quantum escape hatch |
| `test_security.py` | Password reset (send, single use, wrong code, attempt cap, no enumeration), account deletion, refresh replay grace vs. revocation, config validation, cache headers |
| `test_email.py` | The SMTP sender: STARTTLS before login, implicit TLS, a relay with no credentials, the headers a provider needs, a failed send still closing the connection, and the boot guards that refuse an incomplete or unencrypted configuration |
| `test_smtp_settings.py` | The panel's configuration store: round trip, a saved configuration beating `EMAIL_SENDER`, the password never coming back out, the test send not saving, the page never rendering the password, and the reset-code index drained on use and on account deletion |
| `test_ratelimit.py` | Per-address throttling, per-address isolation, health exempt, limiter can be disabled, and the `/blocks/` prefix bucket shared across ids |
| `test_invites.py` | The gate, single use, refund on a taken email, and that a taken and a free address are indistinguishable without a code |
| `test_account_requests.py` | The request endpoint: 202 for a free, taken, duplicate or backlogged address alike, the cap, the rate limit, approval creating an account with no usable password, the reset journey that follows, rejection, and the audit entries |
| `test_admin.py` | The token boundary in both directions, admin lockout, user listing and paging, account creation and the forced password change it triggers, revoke and delete, invite minting, the audit log, and that no admin response carries a position |
| `test_admin_ui.py` | The cookie (HttpOnly, SameSite, path), redirects without one, the add-account form and its errors, the one-shot password notice, the confirmation page, the one-shot mint flash, and that no page mentions a coordinate |
| `test_blocks.py` | Blocking a walker: symmetric invisibility in both directions, unblock, the name kept for an offline walker, blocking yourself, a block surviving a new session, the 200-entry ceiling and its 409, and that deleting an account clears it from other block lists |

`scripts/e2e.py` runs 56 checks against the live stack, including token
rejection, the full proximity cycle, the coordinate-free alert shape, the
password-reset and account-deletion surfaces, the invite gate, the admin API's
token boundary in both directions, and the full handed-over-password cycle —
create, sign in, change, and the old password failing afterwards.

`.github/workflows/ci.yml` runs all of it on every push: `pytest`, the Android
unit tests, `lintDebug`, and `scripts/e2e.py` against real Redis. See **CI** in
`HANDOFF.md` for what the runner needs.

## Authentication

There is no anonymous mode. The app opens on a login screen and the map is
unreachable until you sign in.

| Endpoint | Purpose |
| --- | --- |
| `POST /auth/register` | Create an account with an invite code, returns a token pair (201) |
| `POST /auth/account-request` | Ask an operator for an account; always 202 (see [Account requests](#account-requests)) |
| `POST /auth/login` | Exchange email and password for a token pair, or a password-change ticket |
| `POST /auth/refresh` | Exchange a refresh token for a new pair |
| `POST /auth/logout` | Revoke one refresh token (204) |
| `POST /auth/logout-all` | Revoke every refresh token for the account (204) |
| `POST /auth/password/change` | Replace a handed-over password; returns a token pair |
| `POST /auth/password-reset/request` | Email a 6-digit reset code (always 202) |
| `POST /auth/password-reset/confirm` | Set a new password with a code (204) |
| `DELETE /auth/account` | Delete the account and its data (204, needs the password) |

- Access tokens live 15 minutes, refresh tokens 30 days. Refresh tokens are
  indexed in `dogwalk:users:<id>:refresh` so they can be revoked individually,
  and a used refresh token is marked for 60 s so a retried request on a flaky
  connection does not look like a replay.
- Passwords are hashed with stdlib `hashlib.scrypt` (N=2^14, r=8, p=1) — no
  third-party crypto dependency. `authenticate()` compares against a dummy hash
  for unknown emails so a missing account and a wrong password take the same time.
- Ten failed logins for one email locks it for 15 minutes.
- Reset codes are 6 digits, stored as a sha256 key, valid 15 minutes, single use,
  and capped at 10 attempts. `request` returns 202 whether or not the address
  exists, so it cannot be used to enumerate accounts.
- Changing or resetting a password bumps `token_version`, which invalidates every
  access token already in flight. Deleting an account also closes any live socket.
- An account an operator created still has the password they were handed. Signing
  in with it returns `must_change_password: true` and a `pwchange` token instead
  of a pair. That token is a fourth `typ`, so `decode_token` refuses it everywhere
  except `/auth/password/change` — the account cannot walk, refresh or sign out
  until it has picked a password of its own. See the Admin section.
- Per-IP rate limits apply to the auth endpoints (`ratelimit.py`), keyed on
  `X-Forwarded-For`, which Caddy **overwrites** rather than appends so a client
  cannot spoof its own address:

  | Route | Limit |
  | --- | --- |
  | `/auth/login` | 10 / min |
  | `/auth/register` | 20 / hour |
  | `/auth/refresh`, `/auth/logout` | 30 / min |
  | `/auth/logout-all` | 10 / min |
  | `/auth/password-reset/request` | 3 / hour |
  | `/auth/password-reset/confirm` | 10 / hour |
  | `/auth/account` | 5 / hour |
  | `/blocks/{id}` | 60 / min |

  Fixed windows, so a burst can straddle a boundary and get two windows' worth.
  That is the right cost for a control that exists to blunt automated abuse
  rather than to meter traffic. `RATE_LIMIT_ENABLED=0` turns it off.

  `/blocks/{id}` is the one prefix bucket (`PREFIX_LIMITS`): the counter is keyed
  on `/blocks/`, not on the full path, because a bucket per id would give every
  id its own counter and amount to no limit at all. `GET /blocks` is not
  throttled — it reads the caller's own setting.
- **The walker id is the account id, taken from the token.** A client cannot
  claim to be someone else by editing a query string.

### Invite codes

Registration needs a single-use code. Mint one with:

```bash
cd backend
.venv/Scripts/python scripts/mint_invites.py            # one code
.venv/Scripts/python scripts/mint_invites.py --count 5  # a batch
.venv/Scripts/python scripts/mint_invites.py --list     # how many are unused
```

Codes are 10 characters from an alphabet with no `I`, `O`, `0` or `1`, shown as
`XXXXX-XXXXX`. Typing is forgiving: case, dashes and spaces are all ignored.

The reason this exists is narrower than "invite-only is nice". `POST
/auth/register` has to answer one question about an email address — is it taken?
— and answering it truthfully is what makes the endpoint an enumeration oracle.
Changing the status code does not fix that: if register always answered 201, an
attacker would register with a password of their choosing and then try to log in
with it, and success or failure would tell them the same thing. The oracle moves;
it does not close.

The code is checked **before** the email is looked up, so an uninvited caller gets
the same 403 whether or not the address exists. That is the property
`test_invites.py` and `scripts/e2e.py` both assert. Someone holding a code can
still probe, but holding a code means they were invited, which is the trust
boundary we want.

A code is consumed before the email is checked, and refunded if the address turns
out to be taken — a mistyped email should not burn an invitation.

Set `INVITE_REQUIRED=0` to turn the gate off. Do that only once there is email
verification to replace it, because that is the only thing that closes the oracle
properly.

### Account requests

The other way in, for someone who was not handed a code: the app's login screen
has **Request an account**, which posts an address to
`POST /auth/account-request`. Nothing is created. The request waits in the panel
at `/admin/ui/requests`, where an operator approves or rejects it.

The endpoint answers **202 for every address** — free, already registered,
already requested, or dropped because the backlog is full. That is the same
reasoning as the invite gate: an answer that differed would say whether an
address has an account. It is throttled at 5 per hour per IP, the queue is
capped at 100, and a request for an address that already has an account is never
queued.

Approving creates the account with a generated password that is **thrown away**.
The operator never sees it, so it cannot become a shared secret, and the only
way in is the reset flow — the approval email says so and carries no code. The
requester opens the app, taps **Forgot password?**, and sets a password of their
own. That clears the handed-over flag, so the first sign-in is a normal one.

A failed send does not fail the approval: the account exists, and "Forgot
password" works whether or not the notice arrived. The panel says which
happened.

When the access token expires mid-walk the backend closes the socket with
application code `4401`. The app exchanges its refresh token, reconnects, and
carries on without user action. If the refresh token is dead too, the session is
cleared and the UI falls back to the login screen.

## Admin

A web panel for accounts, invite codes and outbound email, at `/admin/ui`. It is
deliberately narrow: it can see who is registered, create an account, sign one
out, delete one, approve or reject an account request, mint invite codes, and
point the deployment at an SMTP server.
It cannot see where anyone is — nothing in `app/routes_admin.py` or
`app/admin_ui.py` reads the geo index, and `test_admin.py` asserts that no admin
response contains a position field.

### Creating an account

The Accounts page has an "Add an account" form: an email, and that is it. The
server generates a 16-character password and shows it once, on the page the form
redirects to. Only a hash is stored, so it cannot be recovered — if the operator
loses it before handing it over, delete the account and make another.

No invite code is spent, because the code exists to gate *self*-registration by
a stranger and an operator is not one.

**The first sign-in is a password change.** Signing in with the handed-over
password returns `must_change_password: true` and a `pwchange` token instead of
a session. That token is a separate JWT `typ`, so every other route refuses it
on the type check alone: the account cannot walk, refresh or sign out until it
has picked a password of its own. `POST /auth/password/change` spends the
ticket, sets the password, bumps `token_version` and returns a normal pair, so
the user is signed in the moment they finish.

Generating rather than choosing is the point. The operator knows this password,
so a copy that stayed valid would not be a password, it would be a backdoor. The
action is audited (`create_user`, with the address and the new `user_id`); the
password never reaches the log.

The same thing over the API:

```bash
curl -X POST http://127.0.0.1:8000/admin/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"friend@example.com"}'
```

201 with `user_id`, `email`, `created_at` and `temporary_password`; 409 if the
address is taken.

### Creating an operator

Admins are a separate store from walkers and are created by script, not by an
endpoint — an endpoint that creates admins has to be protected by an admin,
which is a bootstrapping problem.

```bash
cd backend
.venv/Scripts/python scripts/create_admin.py --email you@example.com
.venv/Scripts/python scripts/create_admin.py --list
.venv/Scripts/python scripts/create_admin.py --backfill   # index pre-existing accounts
```

`--backfill` is needed once, for accounts registered before
`dogwalk:users:created` existed. Without it those accounts do not appear in the
list, because the list pages over that index rather than scanning the keyspace.

### The boundary

Admin tokens are a third JWT type (`typ: "admin"`), not a role claim on a user
token. `decode_token` refuses a token whose type is not the one asked for, so:

- a walker's access token gets 401 from every `/admin` route;
- an admin token gets 401 from `/auth/*` and close code `4401` from `/ws/walk`.

There is no refresh token for admins. The session lasts `ADMIN_TOKEN_TTL_SECONDS`
(eight hours) and then the operator signs in again.

The HTML pages authenticate with an HttpOnly, `SameSite=Strict` cookie scoped to
`/admin`, because a browser form cannot set an `Authorization` header.
`SameSite=Strict` is what stands in for a CSRF token here.

### Configuring email

The **Email** page is the shortest path from "reset codes go to the log" to
"reset codes arrive by mail". Fill in the server, port, security, username,
password and sender address, put your own address in "Send a test to", and press
**Send test** — that posts one message through the values on screen without
saving them, so a wrong port costs nothing. Press **Save** when it arrives.

Saving writes the configuration to `dogwalk:settings:smtp` and rebuilds
`app.state.email_sender` in place, so the next reset email uses the new server
with no restart. A saved configuration wins over `EMAIL_SENDER`, which is why the
page works on a deployment that still says `EMAIL_SENDER=logging`. **Forget this
configuration** deletes the key and falls back to the environment.

The password is write-only. It is never returned by `GET /admin/smtp` (which
reports `has_password` instead), never placed in the template context, and never
written to the audit log — which records `smtp_saved` with the host and sender,
and `smtp_test` with the recipient and whether it was delivered.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /admin/auth/login` | Exchange email and password for an admin token |
| `GET /admin/users` | Accounts, newest first, `offset`/`limit` (max 100) |
| `POST /admin/users` | Create an account with a generated password, without spending an invite code (201) |
| `GET /admin/users/{id}` | One account, with its live session count |
| `POST /admin/users/{id}/revoke-sessions` | Bump `token_version` and close any live socket (204) |
| `DELETE /admin/users/{id}` | Delete the account and everything attached to it (204) |
| `GET /admin/invites` | Unused codes, and how many have been used |
| `POST /admin/invites` | Mint 1–50 codes (201) |
| `GET /admin/requests` | Accounts strangers asked for, newest first |
| `POST /admin/requests/{id}/approve` | Create the account and email the requester (201) |
| `DELETE /admin/requests/{id}` | Reject a request (204) |
| `GET /admin/stats` | Account and invite counts |
| `GET /admin/smtp` | The SMTP configuration in use, with `has_password` rather than the password |
| `PUT /admin/smtp` | Save a configuration and rebuild the running sender (400 if it is unusable) |
| `DELETE /admin/smtp` | Forget it and fall back to `EMAIL_SENDER` (204) |
| `POST /admin/smtp/test` | Send one message through the submitted values, without saving them |
| `GET /admin/audit` | Recent operator actions, newest first |

Every mutating route writes to `dogwalk:admin:audit`, a capped list of 1000
entries. Minted codes are recorded as a count, never as codes: the log is
readable by every admin, so it must not be a second copy of the secrets it
audits.

### Serving it

The panel is served by the same FastAPI process, so it is reachable wherever the
backend is. Through Caddy that means adding the admin hostname to the site line
in `backend/Caddyfile` — the same `default_sni` caveat as the app applies. On the
VPS this is already done: `deploy/Caddyfile` puts `basic_auth` on `/admin*` ahead
of the app's own login, so an unauthenticated request gets 401. For a deployment
of your own, do the same — `basic_auth` or an IP allowlist as a second gate.

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

Two things in `backend/Caddyfile` are load-bearing:

1. **The site address needs explicit hostnames** — `localhost:8443, 10.0.2.2:8443`.
   A bare `:8443` gives `alert internal error` on every handshake because Caddy
   has no name to put in a certificate.
2. **`default_sni 10.0.2.2` is required.** TLS clients must omit SNI when the
   host is an IP literal (RFC 6066), and OkHttp does exactly that. With no SNI
   Caddy falls back to matching against its own container address, finds no
   certificate, and aborts the handshake. The client only sees a bare
   `SSLHandshakeException`, which looks identical to a wrong CA.

The debug build trusts the CA through
`android/app/src/debug/res/raw/caddy_root.crt`. Re-export it after recreating the
`caddy-data` volume:

```bash
cd backend
./scripts/export_ca.sh
```

The release build has no network security config and no `usesCleartextTraffic`:
it uses the system trust store and refuses cleartext. The dev CA is debug-only on
purpose.

## Android

```bash
cd android
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
./gradlew :app:assembleDebug
./gradlew :app:installDebug          # with a device or emulator attached
```

The APK lands in `app/build/outputs/apk/debug/app-debug.apk`.

### Pointing the app at the backend

The default is `wss://backend.thomasfeyens.be/ws/walk` — the VPS, where Caddy
terminates TLS with a Let's Encrypt certificate. Release builds trust that with
the system store, so no custom CA is needed.

For local development, open the settings sheet in the app (gear icon) and set
the URL to `wss://10.0.2.2:8443/ws/walk` — the host machine as seen from the
Android emulator, through the Caddy TLS terminator in `backend/docker-compose.yml`.
Only the debug build trusts Caddy's local CA, so this works out of the box in a
debug APK. On a physical phone against your own machine, use the machine's LAN
address instead, e.g. `wss://192.168.1.20:8443/ws/walk`, add that address to the
Caddyfile site line and to `default_sni`, and re-export the CA. The value is
persisted.

### Release signing

`app/build.gradle.kts` reads `android/keystore.properties`, which is gitignored:

```properties
storeFile=/absolute/path/to/release.jks
storePassword=…
keyAlias=…
keyPassword=…
```

Generate the keystore yourself:

```bash
keytool -genkeypair -v -keystore release.jks -keyalg RSA -keysize 4096 \
  -validity 10000 -alias dogwalk
```

With the file present the release build produces a signed `app-release.apk`.
Without it the build still succeeds and produces `app-release-unsigned.apk`, so a
fresh clone can build without a keystore.

### Permissions

The app asks for precise location and notifications. Location is required to start
a walk; notifications are optional but without them proximity alerts stay silent.

## Protocol

Client connects to `wss://<host>:8443/ws/walk?name=<name>` with an
`Authorization: Bearer <access token>` header. The token is deliberately not a
query parameter, so it never lands in an access log.

Client → server, every 5 s:

```json
{"type": "location", "lat": 52.3702, "lon": 4.8952, "accuracy": 5.0, "ts": 1762764000000}
```

Server → client:

```json
{"type": "welcome", "walker_id": "…", "radius_m": 500}
{"type": "PROXIMITY_ALERT", "radius_m": 500, "walkers": [
  {"id": "…", "name": "Bob", "distance_m": 100.0, "bearing_deg": 0.0}
]}
{"type": "PROXIMITY_CLEAR"}
{"type": "error", "message": "…"}
```

`PROXIMITY_ALERT` always carries the complete neighbour list rather than a delta, so the
client just replaces its marker set. `PROXIMITY_CLEAR` empties it.

**Alerts carry no coordinates.** Each neighbour is a distance and a bearing from
you, both snapped to a grid, and the client places the marker with
`GeoMath.destination(self, bearing, distance)`. There is no field in the protocol
that can leak an absolute position, so a modified client cannot ask for one.

Close codes: `4401` means the token is missing, expired or revoked — refresh and
reconnect, do not treat it as a network failure.

## Privacy

The product needs to tell you that someone is 100 m north of you. It does not
need to tell you where they are, and it does not need to tell them where you are.

`app/privacy.py` is the whole policy:

- `bearing_degrees(from, to)` — the compass direction from one point to another.
- `quantise_distance(m)` — snaps to `DISTANCE_QUANTUM_M` (25 m).
- `quantise_bearing(deg)` — snaps to `BEARING_QUANTUM_DEG` (30°).

`proximity.py` builds every `NearbyWalker` through those three functions, so the
worst case a client can learn about a neighbour is a 25 m band and a 30° wedge —
an area of roughly 200 m² at 100 m, and it is the *same* band and wedge for
everyone in it. The exact coordinates stay in Redis and never appear in a frame.

Two residual risks worth naming:

- **Trilateration by moving.** A determined client that walks around and collects
  many bearings to the same walker can intersect them. This is inherent to any
  proximity feature — the alternative is not showing neighbours at all. The
  quanta raise the cost; they do not remove it.
- **Account enumeration is closed by the invite gate**, not by the status code.
  `POST /auth/register` still answers 409 for a taken email, but only after a
  valid code has been spent, so an uninvited caller learns nothing. See
  [Invite codes](#invite-codes).

## How the proximity maths works

1. Every fix is written with `GEOADD` into `dogwalk:walkers:geo`, with the exact
   coordinates in `dogwalk:walkers:pos` and a timestamp in `dogwalk:walkers:seen`.
2. `GEOSEARCH … BYRADIUS 500 m` finds everyone inside the radius, nearest first.
3. The walker gets their own alert list, and everyone in it is re-evaluated so their
   maps include the walker who just moved.
4. Redis has no per-member TTL inside a sorted set, so a sweeper removes walkers whose
   last fix is older than 30 s and re-evaluates whoever was watching them. That is what
   makes a stale marker disappear instead of lingering.

## Warning distance

The 500 m radius is the *discovery* radius: everyone inside it shows on the map. The
notification is governed by a separate, smaller **warning distance** picked in the app's
settings — 30 m, 50 m or 100 m, default 100 m.

It is stored in `SharedPreferences` and applied on the client, because every
`PROXIMITY_ALERT` frame already carries `distance_m` for each walker. Changing it takes
effect on the next fix, with no reconnect and no backend change.

The map draws both rings: green for the discovery radius, amber for the warning distance.
A walker buzzes your phone once when they cross into the amber ring, and not again until
they leave and come back.

## Names on the map

Settings has a **Your dog's name** field. It defaults to a random dog name on first launch
(`Biscuit`, `Pepper`, `Mochi`, …) and is stored in `SharedPreferences` alongside the
warning distance. The name is sent to the backend as the `name` query parameter on the
WebSocket URL, so other walkers receive it in their `PROXIMITY_ALERT` frames.

Every marker on the map carries its name: yours in green, each nearby walker's in the
colour of your dog's relationship to them (see below). Changing the name in settings
relabels your marker immediately, without restarting the walk.

The tags are Compose `Text` composables layered over the `MapView`, positioned from
`Projection.toScreenLocation` and refreshed on every camera move. They are deliberately
*not* a MapLibre symbol layer: symbol layers need a glyph server, and the remote glyph
endpoint answers `304` in a way MapLibre's HTTP stack does not turn into a usable body, so
the text silently never renders. Compose text has no glyph dependency, works offline, and
keeps the styling in one place.

## Friend groups: dogs yours likes and does not like

Tap a nearby dog's name tag on the map to say how your dog feels about them. Three
states, each with its own marker colour, notification channel and sound:

| State | Marker | Channel | Sound | Vibration |
|---|---|---|---|---|
| Not marked | amber `#E0A030` | `dogwalk_proximity` | system notification tone | original pattern |
| Likes | green `#2E9E5B` | `dogwalk_proximity_friend` | bundled rising two-note chime | short double tap |
| Does not like | red `#C62828` | `dogwalk_proximity_disliked` | bundled falling two-note alarm | long triple buzz |

The relationship is stored on the device only, in `SharedPreferences` under
`marked_dogs`, as a JSON list of `{id, name, relationship}`. **Nothing about it is ever
sent to the server.** The backend has no idea who likes whom, so the social graph cannot
leak from Redis even if the store were dumped. This is why the feature needed no backend
change at all. Blocking is the one exception — it has to be server-side to work; see
[Blocking a walker](#blocking-a-walker).

Names are persisted alongside the ids so the settings list still reads correctly when the
dog is not nearby. Settings has a **Marked dogs** section listing both groups, each row
with a remove button, so a mis-tap is not permanent.

Three separate GeoJSON sources and layers (`friend`, `disliked`, `unknown`) drive the
markers rather than one source with a data-driven `match` colour expression. More code,
but a subtly-wrong expression fails silently, and this project has already lost time to
MapLibre features that do not behave as documented.

Android fixes a notification channel's sound at creation and ignores later changes, which
is why there are three proximity channels rather than one.

The friend and disliked tones are two small WAVs in `res/raw` — `friend_chime.wav`
(28 KB, two rising notes, odd harmonics only so it reads as a bell) and
`disliked_alarm.wav` (43 KB, two falling notes, all harmonics so it reads as a warning).
They are generated, not sampled, so there is no licence to track. The unmarked case keeps
the system notification tone, which is what the app used before this feature.

They are bundled rather than pulled from `RingtoneManager` because the system only
exposes one notification tone and one alarm tone: using those would have given friends
the same sound as an unmarked dog, which is not what "another sound" means.

When several relationships are present in one alert, the most urgent wins:
**disliked > unmarked > friend**. Otherwise a friend arriving would replace a warning with
a chime. This is an explicit `urgency()` function, not enum ordinal order.

`WalkRepository` emits only the walkers that *just* arrived inside the warning distance,
not the whole set. With per-relationship sounds, re-emitting the full set would replay the
disliked alarm every time an unrelated friend appeared.

## Blocking a walker

Marking is local and cosmetic; blocking is not. A block has to stop the other
walker receiving your bearing at all, and only the server can do that, so this is
the one piece of the social graph that lives in Redis.

| Endpoint | Purpose |
| --- | --- |
| `GET /blocks` | Who you have blocked, with the name captured at block time |
| `POST /blocks/{id}` | Block a walker (204) |
| `DELETE /blocks/{id}` | Unblock (204, harmless when there was none) |

**Invisibility is symmetric.** `app/blocks.py` keeps two keys per walker — a hash
of who you blocked (`dogwalk:blocks:<id>`) and a set of who blocked you
(`dogwalk:blocked_by:<id>`) — and `refresh_neighborhood` drops the union before
building the frame. A one-way filter would leave the blocker on the blocked
walker's map, which is the exact thing they blocked them to prevent. Both maps
are refreshed by the request itself, so the marker goes away on the tap rather
than on the next GPS fix.

**Blocking never reports whether the id exists.** A 404 for an unknown walker
would make the route an account-existence oracle, and the client only ever blocks
someone it just saw on the map. The list is capped at 200 entries instead, which
bounds the storage without answering that question: the 201st distinct id gets
409 `block list is full`, a statement about your own list rather than about the
id. Re-blocking an existing entry writes no new key and is still allowed at the
ceiling. `POST /blocks/{id}` is also throttled at 60/minute on the `/blocks/`
prefix — see [Authentication](#authentication).

The name is stored because the settings list has to name someone who is not
walking right now, and the hub only knows the names of live connections. It is
the same display name the server already broadcasts to everyone in range.

Deleting an account calls `blocks.forget()`, so everyone who blocked it does not
keep a stale id in their settings forever.

## Testing proximity by hand

Sign in, start a walk in the app, then simulate a second walker from a terminal:

```bash
cd backend
.venv/Scripts/python scripts/second_walker.py
```

It connects as `Buddy`, sends a fix 100 m north of the reference point, then one 800 m
north, printing every server frame. Expect `PROXIMITY_ALERT` with `distance_m` around
100 and `bearing_deg` around 0, then `PROXIMITY_CLEAR`. On the phone you should see the
amber marker appear to the north, then go away.

`scripts/hold_walker.py --meters 60 --name Buddy` does the same but stays put, which is
easier to watch while you tap around the app. Both print the raw frames, so you can
confirm for yourself that no `lat`/`lon` appears in them.

Whether the phone also buzzes depends on the warning distance: at the default 100 m the
100 m fix is right on the boundary and does alert; at 30 m or 50 m it stays silent. To
exercise the alert, lower the script's first distance below your setting.

The reference point is Amsterdam Dam (`52.3702, 4.8952`). If the phone is somewhere
else, move it there first with `adb emu geo fix 4.8952 52.3702` (longitude first), or
edit the constants at the top of the script.

The scripts register their own throwaway accounts (`<walker-id>@example.com`) and talk to
the backend directly on port 8000, so they do not need the Caddy CA.

## Notes

- The map style is `android/app/src/main/assets/style.json`: a vector style over the
  self-hosted PMTiles archive at `pmtiles://https://maps.thomasfeyens.be/benelux.pmtiles`.
  MapLibre Native reads PMTiles directly, so no tile server runs — Caddy serves the
  archive and the client range-requests it. The style deliberately has no symbol
  layers, so it needs no glyph server; name labels are Compose overlays.
- Tokens are stored in `SecurePrefs`, which encrypts each value with a
  Keystore-backed AES-GCM key (`AES/GCM/NoPadding`, 256-bit, alias
  `dogwalk_auth_key`) and writes `iv:ciphertext` base64. A value that fails to
  decrypt is dropped rather than crashing the app. `EncryptedSharedPreferences`
  was the obvious choice but is deprecated in `androidx.security`.
- `android:allowBackup="false"` and `fullBackupContent="false"` are set, so the
  encrypted token blob is not swept into a cloud backup that would restore it onto
  a device without the Keystore key.
- The backend keeps connections in process memory, so it is single-instance. Running
  more than one replica would need Redis pub/sub to fan out between them.
- `default_sni` holds a single value, so the local TLS setup works for one
  IP-or-hostname at a time.
- `docker-compose.yml` publishes the backend on `127.0.0.1:8000`, not `0.0.0.0`, so
  the plaintext port is not reachable from the LAN. Caddy on `8443` is the only
  way in from outside.
