# Handoff — Mikywalk

State captured at the end of the security-review session (2026-11-14). Read this
first when picking the project back up. For what to build next, see
`ROADMAP.md`; for the iOS plan in full, `IOS-PORT.md`; for the iOS code that
exists, `ios/README.md`.

**`main` is clean at `af8a9cc`**, the backup commit; the only thing on top
of it is this note. Since the security-review session: PR #13
(`0906161`, dependency scanners), the three Android PRs, the hardening pass
(PRs #17-#19), the docs follow-up (#20), the backend efficiency pass (#21) and
the DogWalk -> Mikywalk rebrand (#22). The backend image changed with #17-#19,
#21 and #22, and **the VPS was deployed to `6e38b89` on 2026-11-18** — see
**Session log — 2026-11-18** at the top of the log for that and the two bugs
found on resume.
The admin panel
landed earlier as `e5ed2ad admin pagina en account creation`, on top of
`7d26413 Eerste versie`; see **What landed in `e5ed2ad`** at the bottom for
what that commit contains.

**All three Android PRs are merged.** On 2026-11-16, in this order: PR #14
(`7a2803f`) stops the proximity alert keeping a walker that had left; PR #16
(`f6ca3d8`) makes the urgency ordering rank who is present rather than who just
arrived, superseding #14's `silent` parameter; PR #15 stops holding the GPS open
while the walker stands still. All three are Android-side, so the backend image
is unchanged and the VPS was not redeployed. The test counts inside each session
log below were each true on its own branch; the current gate is under
**Android tests**. PR #13 (the dependency-vulnerability scanners) merged as
`0906161`, with docs follow-ups on top. Read **Session log — 2026-11-16** first.

**The security hardening pass is merged.** On 2026-11-17, PRs #17, #18 and #19
landed the review fixes: HSTS on the Caddy sites, rate/size limits on `/ws/walk`,
atomic refresh-token consumption, CSP on the admin panel, a production check on
`PUBLIC_BASE_URL`, duplicate-socket replacement, live geo data moved to a
non-persistent `redis-geo` (`GEO_REDIS_URL`), per-address login lockouts, a
capped used-invite set, server-side admin logout revocation, and the GEOSEARCH /
sweeper performance fixes. See **Session log — 2026-11-17 — security hardening
pass** at the top of the log. The backend image changes, so redeploy the VPS with
`docker compose up -d redis redis-geo backend`.

## Session log — 2026-11-18 — resume: the rebrand, and two bugs CI could not see

Picked the project back up, pulled `main` from `github` (20 commits), rebuilt the
local stack, and ran the gate. Two things were broken that the CI runs on those
PRs were structurally unable to catch.

### What was already on `main` when this session started

- **PR #20** (`56a4d08`) — docs for the hardening pass.
- **PR #21** (`d4abdc0`) — backend efficiency: scrypt moved off the event loop
  behind a `semaphore(8)`, pipelined geo reads, and a sweeper that costs
  O(expired) instead of O(connected).
- **PR #22** (`4a8e6dc`) — the rebrand: DogWalk -> Mikywalk across backend,
  Android, iOS and docs. `com.dogwalk` -> `com.mikywalk`, so **devices must
  un/reinstall the app**. The Redis prefix stays `dogwalk:` on purpose —
  renaming it would orphan every account, invite and block on the VPS — and
  `MIKYWALK_ENV` falls back to `DOGWALK_ENV` so an existing `.env` still boots.

### Bug 1 — registration answered 500 on any database older than PR #17

`dogwalk:invites:used` changed from a set to a sorted set in the hardening pass,
so the cap could evict with `ZREMRANGEBYRANK`. Redis refuses `ZADD` on a set with
`WRONGTYPE` rather than converting, and `invites.consume()` writes that key on
every registration. So on the local volume (62 used codes) and on the VPS (46)
`POST /auth/register` returned 500. `scripts/e2e.py` died on its first
registration with a JSON decode error, which is how it surfaced.

CI could not catch it: it starts against an empty Redis, where the first write
creates the key with the new type. The failure exists only where the old key
already exists, which is every deployment that has been running.

Fixed with `app/migrations.py`, called from the lifespan before the sweeper: it
reads the type and converts in place, so the documented deploy command needs no
extra step. `dogwalk:walkers:seen` (hash -> sorted set, PR #18) is repaired the
same way, keeping its scores because the sweeper reads them as last-seen times.
Verified live: the local key went `set` -> `zset` with all 62 members kept, and
`scripts/e2e.py` returned to 56/56.

### Bug 2 — the suite hung instead of failing

`test_proximity.py::test_disconnect_clears_the_watcher` hung on 8 of 10 runs.
Not a product bug: starlette's `WebSocketTestSession.__exit__` sends the
disconnect and then cancels the server task, so the cleanup that sends
`PROXIMITY_CLEAR` was killed mid-flight. Whether it finished first was the coin
flip. The test now calls `bob.close()` inside the context — which sends the
disconnect without cancelling — and asserts there. 10/10 after the change.

Worth knowing: a missing frame shows up as a hang, not a failure, so this cost
far more to find than it should have. Any future test that waits on a frame the
server may never send has the same shape.

### Verification

`python -m pytest -q` in `backend/`: **287 passed**, five consecutive runs, ~45 s
each (280 before this session's seven migration tests). `ruff check --select
F,E9 app/ tests/ scripts/` clean. `scripts/e2e.py` 56/56 against the local stack.
`docker compose up -d --build` brings up `redis`, `redis-geo`, `backend` and
`caddy`; `/health` answers on both `:8000` and `:8443`.

Android: `:app:testDebugUnitTest` 68 tests, 0 failures, and `:app:lintDebug`
clean — but only after `./gradlew clean`. The first build failed with
`Could not find class file for 'com.dogwalk.DogWalkApp'`: Hilt's generated
sources under `app/build/` still named the pre-rebrand class, and no source file
does. A clean build is the fix, and it is a one-off — nothing to change in the
repo.

### Deployment note

Deployed to the VPS on 2026-11-18: the box went from `5df6b4e` (30 commits
behind, no `redis-geo`) to `6e38b89`. `deploy/verify.sh` passes 16/16 as root,
`/health` answers over HTTPS, and the admin panel still answers 401 at the Caddy
gate. The migration ran on the first boot and logged
`converted legacy key types: dogwalk:invites:used`; the key is now a `zset` with
all 7 members kept. Registration was proven on the box with a minted invite and
an already-taken address: **409, not 500**, and the refund put the invite back,
so the used set is still 7.

A Redis backup was taken first, at `/home/thomas/redis-2026-11-18-predeploy.tgz`
(1.6 KB `dump.rdb` plus a 49 KB AOF). Admin sessions issued before PR #19 are
invalidated by this deploy — sign in to the panel again.

## Session log — 2026-11-18 — monitoring, and the half a script cannot do

The last open gap from the "what else should I do" list. `deploy/watchdog.py`
runs every five minutes from `/etc/cron.d/mikywalk-watchdog` as `thomas` and
emails when a check changes state.

### What it checks, and why those nine

`health` (the public `/health` over HTTPS — one request that crosses DNS, TLS,
Caddy, the proxy and the app), `maps` (the range request, because a 200 there
means the map will not load), `caddy` (the *systemd* one: the compose `caddy`
only publishes 8443 and is not what serves users), `containers` (backend, redis,
redis-geo — running and healthy), `redis` (`PING` **and** a non-empty keyspace),
`disk`, `memory`, `certificate` (days to expiry), `backup` (age of the newest
snapshot, which is what makes yesterday's silent failure visible).

The Redis keyspace check is the one worth arguing for. A Redis that restarted
empty answers `PING`, reports healthy, and passes every other check on this list
while every account, invite and block is gone. `DBSIZE` is the only thing that
notices.

### Why email, and why only on change

The SMTP credentials were already on the box and already proven, so alerting
costs no new dependency and no new public surface. A dashboard is a fine thing
to add later; it is not what wakes you up.

Each check keeps its last state in `/home/thomas/monitoring/state.json`. A
transition sends one message, a recovery sends one, and a failure that persists
re-sends every six hours (`REALERT_HOURS`) so it cannot be quietly forgotten. A
check that has been failing for a week sends 28 messages, not 2016.

### The half a script on this box cannot do

If the machine dies, the watchdog dies with it, and the silence is
indistinguishable from health. `HEARTBEAT_URL` closes that hole: every run pings
an external service and the *absence* of the ping is the alert. It is unset
right now, so **a dead box is still invisible** — that is the one manual step
left, and the script says so in every log line until it is done.

### Verified

All nine checks pass on the box. The state machine was driven for real: a forced
disk failure sent one alert, an immediate re-run sent nothing, and returning to
normal sent one recovery. A stopped container reports `redis-geo is exited` and a
starting one `redis-geo is running but starting`.

Two things the testing changed. `docker compose ps` without `-a` omits stopped
containers, so a dead service reported as *missing* — which sends you looking for
a deleted service instead of a crashed one; it now passes `-a`. And the first
local dry run failed `health`, `maps` and `certificate` with "certificate has
expired" while the same checks passed on the box: the Windows Python CA store
does not have the current Let's Encrypt chain (`YE2` / `Root YE`). The certificate
is valid to 2027-02-12. Worth knowing before trusting a laptop's verdict on TLS.

### Deployed

`/etc/cron.d/mikywalk-watchdog` (`*/5`, as `thomas`, log to
`/home/thomas/monitoring/watchdog.log`) and `/etc/default/mikywalk-watchdog`
(settings, no secrets — the SMTP password is read from `backend/.env`). The
script reads that defaults file itself, so a manual run and a cron run behave
the same; cron does not source it, and a monitor that only works by hand is not
a monitor.

Proven from cron, not just by hand: a temporary every-minute entry with
`DISK_MAX_PCT=1` sent one alert at 09:17 and then stayed silent at 09:18 and
09:19, which is the anti-spam rule working in cron's own environment. The probe
was removed and the real entry has run since — 09:20:01, nine checks, nothing to
send, state written.

**Still to do by hand:** set `HEARTBEAT_URL` in `/etc/default/mikywalk-watchdog`.
Until then a dead machine is silent, and every log line says so.

## Session log — 2026-11-18 — backups, and the `.env` files one `git add` away

Two gaps from the "what else should I do" list, both closed. Neither touches the
backend image, so nothing needs redeploying.

### The account store now backs itself up

`deploy/backup-redis.sh` (new) snapshots the persistent Redis volume and keeps 14
days. It runs from `/etc/cron.d/mikywalk-backup` at 03:17 as `thomas`, who is in
the `docker` group; output goes to `/home/thomas/backups/backup.log`.

It uses `BGSAVE` and then `docker cp`s `/data/dump.rdb`, rather than tarring the
volume. A tar of a live volume can capture the AOF mid-append, which restores as
a torn command and leaves Redis refusing to start — the backup would look fine
and be worthless. `BGSAVE` forks, so the script polls `rdb_bgsave_in_progress`
for up to 60 s before copying; without that wait it would copy the previous
snapshot and report success. The copied file is checked for the `REDIS` magic
before it is gzipped, which catches the case that matters most: a snapshot of an
empty dataset, kept as a backup that restores to nothing.

Restore is deliberately still manual and is written out in `deploy/VPS.md` step
12. The step that is easy to miss: `appendonlydir` must be removed, because with
AOF enabled Redis prefers it over the RDB, so a stale AOF restores the old data
and makes the backup look broken.

Tested locally against the running stack: success path (10074 B, valid header),
retention prune (a 30-day-old file deleted), and the missing-container path
(exit 1, no partial file left behind).

Two things the box taught it that the local run could not. The script was
committed 644 — `core.fileMode` is false on the Windows checkout, so the local
`chmod +x` never reached the repository, and cron cannot execute a 644 file; it
is now `100755` in the index via `git update-index --chmod=+x`. And `find`
chdirs back to its starting directory before exiting, so under `sudo -u thomas`
from a root shell (cwd `/root`) the prune step exited 1 and reported a failed
backup that had in fact written a valid snapshot. The script now cds into the
backup directory first.

Proven on the box, not just locally: a temporary every-minute cron entry ran the
real script twice as `thomas`, both exit 0, both lines in `backup.log`; the probe
was then removed and only the 03:17 entry remains. And the snapshot was restored
into a throwaway `redis:7-alpine` container — 10 keys, the admin account, the
walker account, `dogwalk:settings:smtp` and `dogwalk:invites:used` as a ZSET with
all 7 members. A backup nobody has restored is a guess; this one has been.

### `.env.*` was committable

`.gitignore` had a bare `.env`, which matches that exact name and nothing else.
The `.env.bak-*` copies made during the 2026-11-16 and 2026-11-17 rotations were
therefore untracked-but-committable, and three of them were sitting in the VPS
working tree holding `JWT_SECRET` and `SMTP_PASSWORD` — one `git add -A` from the
repository. Added `.env.*` with `!.env.example` so the template stays tracked.
Verified with `git check-ignore --no-index`: every `.env` variant ignored,
`.env.example` still committable.

The files themselves are still on the box. Deleting them is a separate call —
they are the only record of the pre-rotation values, and the rotation is already
proven live, so they can go.

## Session log — 2026-11-17 — security hardening pass (PRs #17, #18, #19)

A full read-only review of the repo (backend, Android, iOS slice, deploy config),
then fixes — each verified by unit tests, ruff, and the CI run on its PR. All
three PRs are merged to `main`, and the CI runs on `main` after each merge are
green (backend tests, Android tests and lint, e2e against real Redis).

### What landed

- **HSTS** on both production Caddy sites (`deploy/Caddyfile`):
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
- **`/ws/walk` limits** (`app/ws.py`, `app/ratelimit.py`, `app/main.py`): per-IP
  handshake bucket (20/min, checked before `accept()`, close 4429), 4 KiB frame
  cap (close 1009), at most one evaluated location fix per second per connection,
  rapid fixes coalesced newest-wins. The first version dropped rapid fixes and
  the e2e suite caught it (`B receives PROXIMITY_CLEAR at 800 m`); the follow-up
  commit coalesces instead.
- **Atomic refresh consumption** (`app/users.py`): `GETDEL` instead of
  GET-then-MULTI/EXEC-DELETE, so two concurrent refreshes with the same `jti`
  cannot both win.
- **CSP + `X-Frame-Options: DENY`** on `/admin/ui` (`app/main.py`) — strict,
  because the panel has no JavaScript; API responses unchanged.
- **`PUBLIC_BASE_URL` startup check** (`app/config.py`): outside development,
  boot is refused unless it is the deployment's own `https://` URL.
- **Duplicate walk sockets** (`app/ws.py`): the old socket is closed (4409), and
  disconnect cleanup removes the hub entry / geo position only when the hub still
  points at its own connection.
- **Live positions off disk**: new `redis-geo` compose service with persistence
  off and no published port; `GEO_REDIS_URL` points the backend at it via
  `app.state.geo_redis`. Without `GEO_REDIS_URL` both clients are the same, so
  dev and tests are unchanged. `deploy/VPS.md` documents the LUKS recommendation
  for the persistent volume (password hashes, SMTP password, audit log) and the
  new `docker compose up -d redis redis-geo backend` command.
- **Lockouts per client address**: the login lockout (walker + admin, API +
  panel) and the reset-attempt counter are hashes keyed by `X-Forwarded-For`, so
  an attacker can only lock their own address, not the account owner's.
- **Used-invite set capped** at 1000 (`app/invites.py`, ZSET + `ZREMRANGEBYRANK`).
- **Admin logout revokes server-side** (PR #19): admin tokens carry a `jti`
  registered at login (`dogwalk:admin:session:<jti>`) and checked on every admin
  request; `/admin/ui/logout` and the new `POST /admin/auth/logout` revoke it.
- **Perf** (PR #18): `GEOSEARCH ... WITHCOORD` (one round trip per proximity
  evaluation instead of 1+N) and `SEEN_KEY` as a ZSET, so the sweeper is one
  `ZRANGEBYSCORE` instead of `HGETALL` over every walker.

### Verification

`python -m pytest -q` in `backend/`: 276 passed on the last branch (253 before
the pass). `ruff check --select F,E9 app/ tests/ scripts/` clean. CI green on
PR #17, #18, #19 and on `main` after each merge.

### Deployment note

On the VPS, once: `docker compose up -d redis redis-geo backend` (was
`redis backend`). No `.env` change needed — `GEO_REDIS_URL` is set by the compose
file itself. Admin sessions issued before PR #19 are invalidated once; sign in
again. `sudo caddy validate --config /etc/caddy/Caddyfile` before
`systemctl reload caddy` (Caddy keeps the previous config on an invalid file).

## Session log — 2026-11-16 — the day in one place

Five threads, all closed on the same day. Each has its own detailed log below
this entry, in this order.

1. **PR #13 merged** (`0906161`) — the dependency scanners; OSV-Scanner had
   replaced OWASP dependency-check after its NVD rate-limit failure, docs
   follow-ups on top (`63a472f`, `2f2b72d`).
2. **Three Android PRs merged, in that order** — #14 (`7a2803f`, the alert
   refreshes when a walker leaves), #16 (`f6ca3d8`, urgency ranks who is
   *present*, superseding #14's `silent` flag), #15 (`1ea8141`, the GPS sleeps
   while the walker stands still). All Android-side; the backend image is
   unchanged throughout.
3. **SMTP went live on the VPS** (`5b36cde`) — Gmail app password over 465/SSL,
   stored in both slots the design supports; reset codes leave the box in a
   mail body or nowhere. Priority #1 closed.
4. **Secrets reconciliation** — `JWT_SECRET` (`60d0fee`) and the admin password
   (`ae48aa6`) leaked into this session's transcript and were rotated the same
   day; `CADDY_BASIC_AUTH_PASSWORD` (`02d7e20`) never matched the deployment
   and was rotated too. All three verified live on the box, and the local
   `.secrets/mikywalk-secrets.env` now matches the deployment everywhere —
   the panel login is recorded there as well (`ADMIN_PANEL_EMAIL`,
   `CADDY_BASIC_AUTH_USER`, `99b95bc`).
5. **The panel is reachable again** — the admin gate turned out to be the
   *host's* systemd Caddy, not the compose container: Caddy user `thomas`, then
   the app login `thomas.feyens@gmail.com`, both beside their passwords in the
   secrets file.

**End-of-day box state**: `deploy/verify.sh` passes 16/16 **when run as root**;
as `thomas` two of its checks fail for permission reasons, not real ones
(`caddy validate` as a non-root user, and the maps check that must read as the
`caddy` user). The checkout is synced with `main` by `git pull` as `thomas`;
the deployed backend image did not change all day — every box-side change was
`.env`, Redis, or `/etc/caddy/Caddyfile`, each proven live in place, and only
`.env` needed a container recreate.

**Carried forward**: ~~`THOMAS_USER_PASSWORD` matches no known account~~ —
resolved 2026-11-17: it is the VPS *Linux* account `thomas`, not an app account,
and it has been rotated (see **Session log — 2026-11-17**); ~~still no operator
password-change route (rotation is delete + `create_admin.py`)~~ — closed
2026-11-17, see **Session log — 2026-11-17 — the operator password-change
route**; the email-verification decision; ~~a reconnect backoff pass~~ — closed
2026-11-17, see **Session log — 2026-11-17 — the reconnect backoff pass**;
accessibility; `/me`; blocks-cap atomicity; ~~backups~~ — closed 2026-11-18, see
**Session log — 2026-11-18 — backups, and the `.env` files one `git add` away**;
~~monitoring on the box~~ — closed 2026-11-18, see **Session log — 2026-11-18
— monitoring, and the half a script cannot do**.

## Session log — 2026-11-17 — the reconnect backoff pass

The ladder arithmetic was already tested; what was not was the wiring, and the
roadmap's complaint was that nobody had run the client against a backend that
stays down. Both are now covered, and the pass found a bug.

**The ladder, driven for real.** Six plain `500` responses, which OkHttp reports
as failed upgrades, and the request timestamps recorded as they arrive. Three
attempts land in 4.5 s with gaps of about 1 s then 2 s. That is the property the
arithmetic test could not reach: a client that stalled after the first retry, or
retried at a fixed 1 s, looks identical from the UI, which only ever shows
"Offline". The tolerances are wide (700–1800 ms, 1500–3200 ms) because the
timestamps are taken by polling, not by the socket.

**The bug: `reconnect()` did not cancel a pending retry.** A failure schedules a
retry one second out. `reconnect()` — called by `renewSession()` after a token
refresh — opened a socket immediately but left that job running, so it fired
afterwards and called `openLocked()` again, which cancels the socket it just
opened and dials a third time. A healthy connection replaced for no reason, and
one more attempt on a ladder that had just been reset to zero.

It is a narrow window in practice: `reconnect()` is only reached from
`authExpired`, which is emitted on `4401` and on a missing token, and neither of
those schedules a retry. So this was latent rather than live. The fix is two
lines — cancel and null the job before opening — and the test fails without it
(`expected:<2> but was:<3>` requests).

**Gate**: 68 Android tests (was 66), 0 failures, 0 lint errors. The two new ones
are the sustained-failure ladder and the stale-retry regression.

## Session log — 2026-11-17 — the operator password-change route

The last hole in the admin panel. Rotating an operator password used to mean
deleting both admin keys and re-running `create_admin.py`, which changed the
admin id and reset `token_version` — a workaround standing in for a missing
feature, and one that had already been used twice in two days.

**`admin.change_password`** verifies the current password through
`authenticate`, so a wrong guess counts toward the same five-failure lockout as
a wrong login, then rewrites `password_hash` and bumps `token_version` in one
transaction. Every token minted before the call is dead, including the one that
authorised it.

**Both surfaces reissue.** `POST /admin/auth/password` returns a fresh
`AdminTokenResponse` and the panel sets a new cookie, so the session that made
the change stays signed in while every other device drops. Returning 204 would
have signed the operator out of the browser they had just used, which reads as
a failure.

**The current password is required, not optional.** An admin token alone must
not be enough: a stolen cookie would otherwise be a permanent takeover, and the
real operator would be locked out of their own panel. A wrong current password
answers 400 rather than 401 — the caller *is* authenticated, it is the
confirmation that failed, and a 401 would tell a browser to sign out.

**The panel** has `/admin/ui/password`, a nav entry, and a confirmation field
checked in the route rather than in the store function, because it is a typing
mistake rather than a rule about passwords and the JSON API has no second field.

Test count went from 235 to 253: nine on the API (the replacement token works,
the old one dies, other sessions die, the new password is the one that logs in,
wrong current password, same password, short password, no token, and an audit
entry carrying neither password) and nine on the panel (the page renders, needs
a cookie, keeps this browser signed in, signs out another, the new password
opens the login page, mismatched confirmation, wrong current, same password,
short password). `ruff` reports nothing new in the changed files beyond the
`Depends`-in-defaults pattern every route in `routes_admin.py` already uses.

## Session log — 2026-11-17 — the `thomas` password in the secrets file never matched the box

The user could not log in to the VPS. The recorded `THOMAS_USER_PASSWORD` was
the problem, and it was not a stale rotation — it was a value that had never
been applied.

**Diagnosis.** The account was healthy: `passwd -S thomas` → `P` (usable),
`lastchg` = 20774 = today, shell `/bin/bash`, uid 1001. But a `crypt(3)`
comparison of the file's value against `/etc/shadow` returned **false**, and a
real `ssh thomas@207.180.240.29` returned `Permission denied`. The file was
written at 12:55, after the 10:54 rotation, so the value in it was generated
during the secrets-reconciliation pass and never pushed to the box.

**The other three were fine**, verified live rather than assumed:

| Secret | Check | Result |
| --- | --- | --- |
| `JWT_SECRET` | sha256 of the value vs `/home/thomas/dog/backend/.env` | `9ba88ebed0cf4de6` both sides |
| `CADDY_BASIC_AUTH_PASSWORD` | plaintext vs the deployed bcrypt hash | gate `401` without, `303` with |
| `ADMIN_PANEL_PASSWORD` | `POST /admin/auth/login` | `200` |

**Fixed** by setting a fresh 32-character alphanumeric value with `chpasswd`
over SSH, the password on stdin and never on a command line. Verified three
ways before the file was touched: `crypt` against the shadow hash → `true`,
a fresh `ssh thomas@…` → `thomas`, and `sudo -S -k id -un` → `root`. The file
was then rewritten in place, CRLF preserved, 13 keys intact, and a login driven
purely by reading the file succeeded.

**Root password rotated too.** Reading `.secrets/askpass.sh` to understand the
SSH path printed the root password into this session's transcript — the same
exposure class as the five secrets above. It was live (`crypt` against root's
shadow hash → `true`), so it was rotated: fresh 32-character value via
`chpasswd`, new login → `root`, old password → `Permission denied`, and
`askpass.sh` rewritten so `vps.sh` keeps working. `thomas` + `sudo` was the
fallback throughout, which is why the rotation was safe to do.

**Still open**: `~/.ssh/authorized_keys` on the box is still empty, so the
`thomas` password remains the only way in from the dev machine. `askpass.sh`
holds the root password in plaintext beside it.

## Session log — 2026-11-17 — the five leaked secrets: three rotated, logs scrubbed

The `sed` that printed five secrets into the transcript was closed out. Three of
the five are rotated and proven dead; the logs that held them are scrubbed. The
other two are issued by Google and NIST and cannot be rotated from here — see
**Still open**.

**Where the values actually were.** Not just "the transcript": six files under
`~/.vibe/logs/session/unified/`, in three session directories (`66df7ad0…` held
all five, `c25619b5…` the NVD key, `84d366f7…` the SMTP password). **Zero
occurrences in git history** — `git log --all -S` over every commit found
nothing, so nothing reached GitHub. The exposure was local to this machine.

**`JWT_SECRET`** — a fresh 48-byte urlsafe value generated on the box, written
into `/home/thomas/dog/backend/.env` (backup `.env.bak-jwt-rot-20261117`), baked
in with `docker compose up -d`. Proven by A/B: a token minted with the leaked
value gets **401** from `GET /blocks`, one minted with the new value gets **200**.
Container env and `.env` fingerprints match (`b21f2de7ec133ca1`). Every session
issued before the rotation — the admin panel included — is now signed with a dead
key.

**`ADMIN_PANEL_PASSWORD`** — there is still no operator password-change route, so
the rotation is delete-then-recreate: both admin keys deleted, `create_admin.py`
re-run inside the container with the new password piped through `getpass` on
stdin (never on a command line, never in shell history). New admin id
`5ef53f83d44c4503a21fb05ad767576a`, `token_version` back to 1, audit log
preserved. Verified two ways: scrypt against the Redis hash (`True`) and a real
`POST /admin/auth/login` returning 200.

**`CADDY_BASIC_AUTH_PASSWORD`** — hashed on the box with `caddy hash-password
--bcrypt-cost 14` fed over stdin, swapped into `/etc/caddy/Caddyfile` (backup
`Caddyfile.bak-auth-20261117`), `caddy validate` → *Valid configuration*,
`systemctl reload caddy`. Verified over the public internet: no credentials →
**401**, wrong password → **401**, new password → **303** (Caddy passes, FastAPI
redirects), following the redirect → **200**. The repo's `deploy/Caddyfile` hash
line was updated to match the deployment.

**The scrub.** All six leaked values (two distinct `JWT_SECRET`s — the 11-16 one
and the 11-17 one) replaced with `[REDACTED]` at byte level across the three
session directories: 36 replacements in 7 files, 0 remaining, every JSON file
still parses. A pattern sweep for `KEY=<16+ char token>` over the whole `~/.vibe`
tree and the repo now returns **0**. One extra find: `~/.vibe/vibehistory` line
55 held the NVD key from a message the user wrote in an earlier session —
scrubbed too.

**Still open.** `SMTP_PASSWORD` (a Gmail app password for
`thomas.feyens@gmail.com`) and `NVD_API_KEY` are issued by Google and NIST, so
they cannot be rotated from this machine: generate new ones and hand them over to
install. The SMTP one matters more — app passwords work over IMAP as well as
SMTP, and reset codes arrive in that mailbox. Both are scrubbed from disk now,
but the values themselves are unchanged and were exposed for the length of this
session.

**Also still open**: `~/.ssh/authorized_keys` on the box is empty, so the Linux
`thomas` password remains the only way in from the dev machine.

## Session log — 2026-11-17 — account requests: ask in the app, approve in the panel

The invite gate answers "may a stranger register?" with a code. This adds the
other answer: a stranger asks, and an operator decides. Three layers.

**The endpoint.** `POST /auth/account-request` takes an address and answers
**202 for every one** — free, already registered, already requested, or dropped
because the backlog is full. A different answer would be a second enumeration
oracle, which is the exact thing the invite gate exists to close. Throttled at
5/hour per IP (`ratelimit.LIMITS`), queue capped at `MAX_PENDING = 100`, and an
address that already has an account is never queued.

**The store.** `app/account_requests.py`: a sorted set for ordering, a hash per
request, and an email index written with `SET NX` so two simultaneous requests
for one address cannot both win. Nothing here touches `dogwalk:users:*` — a
request is not an account.

**The approval.** `admin.approve_request` is shared by the JSON API and the
panel, like the other account mutations. It creates the account through
`create_account` and **throws the generated password away**: the operator never
sees it, so it cannot become a shared secret, and the only way in is the reset
flow. The approval email says so and carries no code — a password in a mail body
would outlive the fifteen minutes a reset code is good for. A failed send does
not fail the approval; the panel reports `email_sent: false` and "Forgot
password" works regardless.

**The panel.** `/admin/ui/requests`, a nav entry, and a dashboard card. Approve
and reject are form posts with a redirect, like every other page.

**The app.** A "Request an account" button on the login screen opens a one-field
dialog. `AuthApi` now takes `WalkSettings` rather than `SettingsStore`, which is
what lets the call be tested on the JVM — the interface already existed for
exactly that reason.

Gates: `pytest -q` → **235 passed** (28 new in `test_account_requests.py`),
`testDebugUnitTest` → **66 passed** (2 new in `AuthApiTest`), `lintDebug` → 0
errors. `scripts/e2e.py` was **not** re-run: it needs the Docker stack, and
nothing it exercises changed shape. Adding request checks to it is the obvious
follow-up.

One thing the tests caught: `create` originally queued a request for an address
that already had an account, which an operator could only ever answer with a
409. Fixed in the module, not the test.

## Session log — 2026-11-17 — `THOMAS_USER_PASSWORD` explained and rotated

The carried-forward oddity was a misdiagnosis, not a stale secret. The entry was
being compared against *app* accounts; it belongs to the *Linux* account. Three
different identities in this deployment are called `thomas`, which is what made
the entry look orphaned:

| Identity | Where it is used | Credential |
| --- | --- | --- |
| Linux account `thomas` | `ssh thomas@207.180.240.29`, and `sudo` | `THOMAS_USER_PASSWORD` |
| Caddy gate user `thomas` | `basic_auth` on `/admin*` | `CADDY_BASIC_AUTH_PASSWORD` |
| Panel operator | the `/admin/ui` login form | `ADMIN_PANEL_EMAIL` + `ADMIN_PANEL_PASSWORD` |

The Linux account is uid 1001, groups `sudo` and `docker`, on host `vmi1524334`
(`207.180.240.29`). It is **not** the GitHub deploy key
(`~/.ssh/github_mikywalk` on the box) and not any app account.

**Rotated** with `passwd` over SSH — current password, then the new one twice —
using a 32-character urlsafe value generated locally and piped, never printed.
Verified three ways: a fresh SSH login as `thomas`, `sudo -S -k id -un`
returning `root`, and the old password rejected (`Access denied`). The new value
sits in `.secrets/mikywalk-secrets.env` under a comment block naming the host, the
username, and the three identities it is *not*.

**Found while doing it: `~/.ssh/authorized_keys` on the box is empty** (0 bytes,
dated 2026-11-14 11:05). Key login as `thomas` does not work, so the password is
the only way in from the dev machine — the `ssh thomas@207.180.240.29` check in
`deploy/VPS.md` no longer holds. That makes this password a single point of
failure: lose it and the only way back is the provider's console. Restoring the
key is the obvious next step; it was left alone here because it is a separate
access change.

**Also: five secrets were printed into this session's transcript** by a `sed`
whose redaction covered only the `THOMAS_USER_PASSWORD` line — `JWT_SECRET`,
`ADMIN_PANEL_PASSWORD`, `CADDY_BASIC_AUTH_PASSWORD`, `NVD_API_KEY` and
`SMTP_PASSWORD`. Same failure mode as 2026-11-16 and the same local-only
exposure (the Vibe session log on this machine). **Resolved later the same day**:
three rotated and the logs scrubbed — see **Session log — 2026-11-17 — the five
leaked secrets**; the SMTP and NVD values are still the originals.

## Session log — 2026-11-16 — SMTP live on the VPS, priority #1 closed

Gmail as the provider, over the only port the box opens (465 SSL; 587 is blocked —
the earlier finding that shaped this).

### What is configured

| Where | Value |
| --- | --- |
| Redis `dogwalk:settings:smtp` | host `smtp.gmail.com`, port `465`, `ssl`, sender+username `thomas.feyens@gmail.com`, app password |
| `.env` on the box | `EMAIL_SENDER=smtp` + the full `SMTP_*` set; `ALLOW_LOGGING_EMAIL` removed |
| Container env after recreate | `EMAIL_SENDER=smtp`, `ALLOW_LOGGING_EMAIL=0`, SMTP forward all correct |

Two configuration paths live on purpose: a panel-saved config wins over the
environment, so the Redis slot makes the next send work without a restart, and the
`.env` makes it durable if Redis is ever wiped. Removing the acknowledgement is
what makes the fallback safe: `Settings.validate()` now refuses to boot before it
would ever log a code again.

The backend container was recreated with `docker compose up -d` in
`/home/thomas/dog/backend` to bake the new env in — the image itself is unchanged.
A backup of the old `.env` sits next to it as `.env.bak-smtp-20261116`, mode 600,
owned by `thomas`.

### Verification

- **Test before save**: one message through the app's own `SmtpEmailSender` and
  `test_email_body`, executed inside the backend container before anything was
  written — Gmail accepted it, and it landed in the mailbox (subject
  "Mikywalk SMTP test").
- **End to end after the switch**: `POST /auth/password-reset/request` for the one
  account on the box → 202. A reset code was generated (Redis index exists), and
  the logs show zero `email to ` lines (the logging sender's signature, which
  printed the code) and zero `could not send the reset email` lines (an SMTP
  failure). The code left the backend in a mail body or nowhere.
- Container env checked with `printenv`: `ALLOW_LOGGING_EMAIL` now bakes as `0`.

### Discovered: the secrets file is stale

- Every entry in the file is now reconciled. The `SMTP_*` set, including the
  Gmail app password, is recorded in `.secrets/mikywalk-secrets.env` (see above).
  `JWT_SECRET` and `ADMIN_PANEL_PASSWORD` were rotated the same day they leaked
  (next section), and `CADDY_BASIC_AUTH_PASSWORD` — the one entry that never
  matched the deployment — was rotated and re-recorded the same day.
- **`CADDY_BASIC_AUTH_PASSWORD` was rotated the same day.** The admin gate turned
  out to run on the *host's* systemd Caddy (`/etc/caddy/Caddyfile`), not the
  compose container — the `backend/Caddyfile` mounted into `backend-caddy-1` has
  no gate at all, which is why the container's config had no `basic_auth` line
  and the deployed hash hunt kept coming up empty. A fresh password was generated
  locally (never printed), hashed on the box with `caddy hash-password` fed over
  SSH stdin (bcrypt cost 14), swapped into `/etc/caddy/Caddyfile` (backup
  `Caddyfile.bak-auth-20261116` beside it), run through `caddy validate`, then
  applied with `systemctl reload caddy`. Verified over the public internet: no
  or wrong credentials → 401 from Caddy itself
  (`WWW-Authenticate: Basic realm="restricted"`); the new password → Caddy
  passes and FastAPI answers its own 303 to the login route. The local
  `.secrets/mikywalk-secrets.env` records the new value — sync proven by matching
  SHA-256 fingerprints, never by printing — `.secrets/bcrypt.txt` follows the
  new hash, and the repo's `deploy/Caddyfile` hash line was updated to match the
  deployment and committed.
- **`THOMAS_USER_PASSWORD` is the one remaining oddity**: it matches no known
  account (the only walker on the box is the emulator test account) and may
  belong to an account that no longer exists.

### Two secrets appeared in this session's transcript

The admin panel password leaked via a Python traceback from a too-clever
substitution (my error), and `JWT_SECRET` was printed by an `.env` dump whose mask
covered only the SMTP line. Both live in the local Vibe session log on this
machine only.

**`JWT_SECRET` was rotated the same day.** A fresh 48-byte urlsafe secret was
generated on the box, installed into the `.env`, and baked in with a container
recreate — health 200, no boot failure. The local
`.secrets/mikywalk-secrets.env` was updated from the box through a capture that
never printed the value, and sync was proven by matching SHA-256 fingerprints
(`10d65302dc6eefc5`) rather than by printing either side. Every session that
existed before the rotation — the admin panel included — is now signed with a
dead key and must sign in again. The SMTP configuration survived the restart
(Email page slot and env both intact).

**The admin panel password was rotated the same day.** `create_admin.py` creates
but refuses an existing email (`admin.EmailTaken`), and there is no password-change
route for operators — the gap the Known-gaps list already records — so the rotation
is: delete the admin record, then re-run the app's own script inside the container
to create a fresh one. Fresh scrypt hash, new admin id
(`1aa429b76d814631afd2f5ca5eac7c10`), `token_version` back to 1. Because the whole
record was replaced, every admin token ever issued — including anything minted
during the leak window — is dead: `resolve_admin_token` looks the old id up and
finds nothing. The stale login-failure counter went with the deleted keys. The new
password (24-char urlsafe, generated locally, piped over SSH, never printed) is
recorded in `.secrets/mikywalk-secrets.env`, and was verified two ways: scrypt
against the Redis hash (`True`), and a real `POST /admin/auth/login` on the
loopback returning 200 with the token written to a deleted temp file and never
displayed. The admin login is an **email, not a name**:
`thomas.feyens@gmail.com` (the panel's login form takes it; the Caddy popup
in front of it takes user `thomas`). Neither is secret — the address is already
the ACME contact in `deploy/Caddyfile` — but both are also recorded in
`.secrets/mikywalk-secrets.env` (`ADMIN_PANEL_EMAIL`, `CADDY_BASIC_AUTH_USER`)
beside their passwords, so the panel is reachable without rediscovering them
via `scripts/create_admin.py --list` on the box.

## Session log — 2026-11-16 — the urgency ordering ranks who is present, not who arrived

Branch `fix/proximity-urgency`, HEAD `b0c2513`, pushed to both `github` and
`origin`, PR #16, stacked on PR #14.

### The bug

`NotificationHelper` documents its own intent: "a dog yours does not like
matters more than one it does... otherwise a friend arriving would replace the
warning with a chime, which is the wrong way round." That is exactly what
happened, and it is not a big bug to find:

- a disliked dog is 80 m away, the warning is showing, the alarm has sounded;
- a friend arrives;
- the warning is replaced by `Friend nearby` on the friend channel, with a chime.

`WalkRepository` emitted `Arrived` carrying only the walkers that just came in, so
`notifyProximity` ranked urgency across the arrivals alone. The ordering the helper
describes could only ever fire when walkers arrived in the same frame.

### The fix

The alert has two jobs and they are not the same job. The text says who is
nearby, so it is now built from everyone inside the warning distance and always
names the most urgent walker present. The sound says something changed, and
`ProximityAlertPolicy` owns that call — pure, in its own file, testable on the JVM:

> Sound when the most urgent walker present is more urgent than the one the user
> was last told about, or when nothing is.

That rule is airtight in both directions. If the most urgent walker present is now
more urgent than at the last post, then a walker of that relationship must have
arrived — otherwise the last post would already have been about them. And if a
less urgent walker arrives or leaves, the text changes but nothing sounds.

With the sound decided from what was last shown, #14's `silent` parameter is
subsumed: a refresh and an arrival are the same decision now. `Arrived` and
`Updated` therefore collapse into one `Changed` event carrying the whole set, which
is one rule where there were two mechanisms.

Two behaviours pinned deliberately, because they were verified on the emulator
before and must not regress:

- `cancelProximity()` forgets what was last shown, so a walker who leaves and
  comes back still alerts — re-entry is still news.
- A quiet rebuild returns early when the slot is empty, so it cannot bring back
  an alert the user tapped away.

### Two behaviour changes worth knowing

A second dog of the same standing no longer re-sounds: a second friend arriving
next to a friend is not more urgent news. And a friend arriving next to a
disliked-dog warning is silent. Both follow from the one rule — the alert tells
you about something worse than you already knew.

### Verification

- `./gradlew :app:testDebugUnitTest :app:lintDebug` → 58 tests, 0 failures, lint
  0 errors and no new warnings (39, all pre-existing).
- Seven tests on the policy: nothing showing always sounds, friend next to warning
  is silent, disliked next to friend sounds, stranger next to friend sounds, friend
  next to stranger is silent, a second disliked does not replay the alarm, and the
  refresh after the most urgent leaves is silent.
- One on the repository: a walker arriving while another is inside is carried with
  everyone else. The earlier `Updated` assertion became `Changed` carrying `rex`
  alone, because the event no longer knows or cares whether someone arrived.
- **Still not verified on a device.** The posting side needs the shade; the same
  manual pass as before covers it — hold one walker inside, bring in a second,
  confirm the alert keeps naming the more urgent one.

## Session log — 2026-11-16 — the proximity alert stops naming a dog that left

Branch `fix/proximity-alert-refresh`, HEAD `91625ab`, pushed to both `github`
and `origin`, PR #14. The second item on the priority list, after the SMTP
credentials that are still missing.

### The bug

The alert occupies a single notification slot (`PROXIMITY_NOTIFICATION_ID =
2001`) and was only ever posted on arrival. `WalkRepository` compared the set of
walkers inside the warning distance against `alertedIds` and emitted exactly two
things: `Arrived` for the new ids, and `Cleared` when the set went empty. A set
that shrank without emptying matched neither, so nothing was posted and the
notification kept the text it was built with — naming a dog that had walked
away.

### The fix

`ProximityEvent.Updated(walkers)` is the third case: the set changed without a
new arrival, so the alert still applies but its text is stale. The repository
emits it with `withinWarning`, the walkers still inside, and the service rebuilds
the notification from that set.

Two things make the refresh safe to post into the same slot:

- `setOnlyAlertOnce(true)` is set **only** on the refresh path. The ROADMAP entry
  said this flag could not be used, but that was about setting it on the shared
  builder, where it would also silence the arrival. Per-path it does exactly what
  is needed: an update of a notification that is already showing does not sound.
- The refresh returns early when `activeNotifications` no longer holds id 2001.
  `setAutoCancel(true)` makes a tap an acknowledgement, and a silent refresh must
  not undo it by re-posting an alert the user dismissed.

The `when` in `handleFrame` is ordered, so a frame where one walker leaves and
another arrives takes the `Arrived` branch: the arrival already carries the new
text, and a refresh on top of it would be a second post for the same frame.

### Verification

- `./gradlew :app:testDebugUnitTest :app:lintDebug` → 50 tests, 0 failures, lint
  0 errors and no new warnings (39, all pre-existing).
- CI 3/3 green on run `43827835515`.
- Two new tests: `one of several walkers leaving refreshes the alert instead of
  withdrawing it`, and `a walker leaving while another arrives alerts on the
  arrival and does not also refresh`.
- **Not verified on a device.** `NotificationHelper` needs a `Context` and the
  project has no Robolectric, so the notification side is untested. Worth a
  manual pass with two walkers on one socket, the way the withdrawal path was
  checked: hold both inside the warning distance, then move one out and confirm
  the header changes without a sound.

## Session log — 2026-11-16 — the GPS stops running while the walker stands still

Branch `perf/stationary-cadence`, HEAD `d32f168`, pushed to both `github` and
`origin`, PR #15. The third item on the priority list.

### The problem

`PRIORITY_HIGH_ACCURACY` every 5 s for the length of a walk keeps the GPS radio
on for a position that is not changing. Standing still is the common case on a
dog walk: the dog stops to sniff, the walker stops to talk.

### What was ruled out

`PRIORITY_BALANCED_POWER_ACCURACY` while stationary is the obvious fix and it is
wrong here. A balanced fix is accurate to roughly 100 m, which is the whole
warning distance, so it would replace a good position with a useless one. The
saving has to come from the rate, not the accuracy.

The other constraint is server-side: `WALKER_TTL_SECONDS` is 30 and the sweeper
runs every 5 s, so a walker who stops sending fixes is removed from Redis and
disappears from everyone else's map. Going quiet is not an option either.

### The fix

`MovementDetector` classifies each fix as moving or standing still, and the
service swaps cadence on the transition:

- **moving**: continuous `PRIORITY_HIGH_ACCURACY` every 5 s, unchanged;
- **stationary**: no continuous request, one `getCurrentLocation` probe every
  10 s, so the radio sleeps between probes.

Two thresholds rather than one, because a single one flaps: 12 m is the noise band
that counts as standing still, 30 m is how far the walker has to get from where
they stopped before the walk counts as moving again. The anchor is not moved while
the still count runs, so a slow walker is not mistaken for a stationary one —
three 4 m steps are 12 m of real movement, but no single step looks like it.

The 10 s probe is a third of the TTL. A probe that returns nothing re-sends the
last known fix for the same reason, and a probe that throws does not end the loop:
unlike a framework callback, an exception in a coroutine kills it for good, and the
walker would then stop refreshing silently with the walk still running.

### Verification

- `./gradlew :app:testDebugUnitTest :app:lintDebug` → 54 tests, 0 failures, lint
  0 errors and no new warnings (39, all pre-existing).
- Six tests on the detector: first fix is moving, three fixes inside the radius are
  stationary, a walker who keeps walking never goes stationary, a stationary walker
  who walks away is moving again, noise inside the moving radius does not flip the
  verdict, and `reset` returns to moving.
- **The service side is not verified.** It needs a device, and it is the one part
  of this the JVM suite cannot reach. Worth a manual pass: start a walk, stand
  still for a minute, confirm the fix rate drops and the walker stays on a second
  device's map, then walk 50 m and confirm the rate comes back.

## Session log — 2026-11-16 — PR #13 merged: OSV-Scanner replaces dependency-check

Branch `vibe/vuln-scanners-056e99`, HEAD `05eab15`, pushed to both `github`
and `origin`. PR #13 was ready for review, `mergeable_state: clean`, CI 3/3
green on run `43824637127`. **Merged as `0906161`** (squash); docs follow-ups
are on top.

### What changed

The 2026-11-15 evening session left two open problems: NVD was returning 503,
and dependency-check scanned the buildscript classpath instead of the app's
dependencies. Both were reproduced, and the second one is fatal to the
approach:

- `scanConfigurations.set(listOf("debugRuntimeClasspath"))` does fix the
  classpath — the log then says `Analyzing app:debugRuntimeClasspath` and the
  `commons-lang3` `IllegalAccessError` disappears (commons-lang3 is not in the
  app's runtime classpath).
- But dependency-check 13 treats an NVD update failure as fatal:
  `ExceptionCollection: UpdateException: Error updating the NVD Data`. NVD
  answered 503 to roughly one request in three, and the client exhausted all
  31 retries. The cold database is 343 MB. A gate that fails when NVD is
  having a bad day says nothing about the code.

So dependency-check is gone. OSV-Scanner 2.7.1 reads a Gradle lockfile, needs
no key and no database, and answers in seconds.

| Commit | What |
| --- | --- |
| `7a4ccc4` | dependency-check removed from the catalog and the app module; `debugRuntimeClasspath` + `releaseRuntimeClasspath` locked; `app/gradle.lockfile` committed; CI runs OSV-Scanner 2.7.1 (pinned, SHA-256 verified); `UPGRADING.md` rewritten. |

### Why the lockfile is narrow

`lockAllConfigurations()` produced 274 entries and 7 findings — all of them
from `androidLintTool`, `hiltAnnotationProcessor*`, `ksp*ProcessorClasspath`
and `_agp_internal_*kspClasspath`: guava, commons-lang3, httpclient,
bouncycastle. None of those ship in the APK. Locking only the two runtime
classpaths gives 136 entries and no findings, and the scan now covers
maplibre, okhttp, play-services-location, coroutines and Hilt.

`configurations.named("debugRuntimeClasspath")` fails at script evaluation
("Configuration with name ... not found") because AGP creates the
configuration later; `configurations.configureEach { ... }` is the working
form.

### Verification

- `./gradlew :app:testDebugUnitTest :app:lintDebug` → 48 tests, 0 failures,
  lint 0 errors.
- `osv-scanner scan --lockfile=app/gradle.lockfile` → 135 packages,
  "No issues found", exit 0. Adding `commons-lang3:3.16.0` to a copy makes it
  exit 1, so the gate does fail when it should.
- `pytest -q` → 207 passed; `pip-audit --strict` → no known vulnerabilities.
- `actionlint` 1.7.12 clean on `ci.yml`.
- CI run `43824637127`: backend, android and e2e all success.

### Environment facts added

- OSV-Scanner 2.7.1 Windows binary is at `/tmp/osv/osv-scanner.exe` for this
  session; the Linux asset's SHA-256 is
  `fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8`.
- `./gradlew :app:dependencies --write-locks` also writes
  `android/settings-gradle.lockfile` (version-catalog resolution only). It is
  gitignored; only `android/app/gradle.lockfile` is committed.
- The NVD key in `.secrets/mikywalk-secrets.env` is no longer read by CI. It
  can stay for a manual dependency-check run, but nothing uses it.

## Session log — 2026-11-15 (evening) — PR #13, vulnerability scanners

Work in progress on branch `vibe/vuln-scanners-056e99`, PR #13. **Not merged,
not pushed to `origin`.** `main` is untouched at `d2f5018`.

### What the CI failure was

The android job failed on run `43714875609` with:

```
Error updating the NVD Data
Caused by: io.github.jeremylong.openvulnerability.client.nvd.NvdApiException:
  Invalid API Key, length of 0 too short to provided a masked partial key
Unable to continue dependency-check analysis.
> Task :app:dependencyCheckAnalyze FAILED
```

Root cause: dependency-check **13.0.0** ships `nvd.api.key=` as an empty default
in `dependencycheck.properties`, and `NvdApiDataSource` only null-checks it
(`ifnull` in the bytecode), so `""` reaches `NvdCveClientBuilder.withApiKey("")`
and `NvdCveClient` rejects any key shorter than 5 chars. 12.2.2 has no such
default. The `nvd.apiKey` line in `app/build.gradle.kts` is correct — the plugin
uses `setStringIfNotEmpty`, so a blank env var is skipped.

### What landed on the branch

| Commit | What |
| --- | --- |
| `057161a` | Scan step skips when `NVD_API_KEY` is absent (`if: env.NVD_API_KEY != ''`); env moved to job level because the `secrets` context is not allowed in a condition. `UPGRADING.md` corrected: the key is required, not optional. |
| `2aa4479` | Cache path fixed: `~/.dependency-check` → `~/.gradle/dependency-check-data`. `DataExtension` defaults to `<gradleUserHome>/dependency-check-data/<version>`, so the old path was never created and the cache never hit. |

Both actionlint-clean. `057161a` is pushed to `github`; `2aa4479` is local only.
CI run `43743687642` is 3/3 green with the scan step skipped.

### The NVD key

Added to `/c/Users/thoma/Downloads/.secrets/mikywalk-secrets.env` as
`NVD_API_KEY`. **Still needs to be added as a GitHub repo secret** — Settings →
Secrets and variables → Actions → `NVD_API_KEY`. Until then the scan step stays
skipped and the Android dependency gate is off.

### Two open problems

1. **NVD's API was down** during the last run: `NvdApiException: NVD Returned
   Status Code: 503` after 31 retries. Verified independently — `curl` to
   `services.nvd.nist.gov/rest/json/cves/2.0` returns 503 in 0.4s with and
   without the key, and the body is a Cloudflare "No server is available to
   handle this request" page. Retry before concluding anything.
2. **The scan analyzes the wrong classpath.** It walked 20 jars —
   `google-java-format`, `jimfs`, `jna`, `groovy`, `commons-compress`,
   `httpclient`, `jaxb-runtime`, `txw2`, `protos`, `compose-group-mapping` — all
   Gradle/AGP/Kotlin plugin internals, and zero app dependencies (no maplibre,
   okhttp, kotlinx-coroutines, play-services). It also throws
   `IllegalAccessError: class org.apache.commons.lang3.Strings tried to access
   method CharSequenceUtils.regionMatches` from analyzing `commons-lang3-3.16.0.jar`
   against dependency-check's own newer copy in a different classloader. So the
   gate fails and measures the wrong thing even with NVD up.

### Next step — needs a decision

Either point the plugin at the app's classpath:

```kotlin
dependencyCheck {
    scanConfigurations.set(listOf("debugRuntimeClasspath"))
    ...
}
```

(`debugRuntimeClasspath` exists and resolves the real dependencies — checked with
`./gradlew :app:dependencies --configuration debugRuntimeClasspath`), or drop
dependency-check for Android and use OSV-Scanner or Trivy against the Gradle
dependency graph. dependency-check is a JVM-artifact scanner and Android ships
AARs, so it is an awkward fit either way. Neither alternative needs an NVD key.

### Environment facts added

- dependency-check's data directory is `~/.gradle/dependency-check-data/11.0`
  (343 MB after a full pull), **not** `~/.dependency-check`.
- `NVD_API_KEY` lives in `/c/Users/thoma/Downloads/.secrets/mikywalk-secrets.env`.
  Export it before a local scan:
  `export NVD_API_KEY=$(grep '^NVD_API_KEY=' /c/Users/thoma/Downloads/.secrets/mikywalk-secrets.env | cut -d= -f2-)`
- `actionlint` is not installed on this machine. The 1.7.12 Windows binary was
  downloaded to `/tmp/al/actionlint.exe` for this session; re-download it or
  install it properly before trusting a workflow edit.

## Session log — 2026-11-15

All of the below is committed, pushed to both remotes (`github` and `origin`),
and deployed to the VPS. HEAD is `c2c3af6`, tree clean, no open PRs.

### What landed

| Commit | What |
| --- | --- |
| `859b69e` | `SECURITY.md` replaced with a real security policy. |
| `f80362c` | `UPGRADING.md` dependency runbook (one group per PR); Dependabot extended to pip and gradle. |
| `c9313d1` | Android walk history & stats: client-side `WalkStatsRecorder` + `PrefsWalkHistoryStore` (capped at 100 walks) plus a history dialog. 48 unit tests, lint 0 errors, verified on the emulator. |
| `5ac6697` | `INSTALL.md` — backend + Android setup, every step audited against the repo. |
| `6f1e4f7` | CrowdSec install and its packaging gotchas documented in `deploy/VPS.md`. |
| `9702cf5`–`c2ef920` | Dependabot pip PRs #2–#6 merged: httpx >=0.28.1, uvicorn >=0.54.0, pydantic >=2.14.3, pytest >=9.2.0, jinja2 >=3.1.6. Local gates green with `-U`: pytest 207, ruff clean, e2e 56/56. |
| `c2c3af6` | PR #12 merged — the whole Android toolchain moved as one group: AGP 9.5.0, Gradle wrapper 9.9.1, Kotlin 2.4.21, KSP 2.3.14, Hilt 2.60.1, coroutines 1.11.0, play-services-location 21.4.0. `org.jetbrains.kotlin.android` removed (AGP 9 has built-in Kotlin and hard-fails otherwise); compileSdk stays 36. CI 3/3 green; Dependabot PRs #7–#11 closed as superseded. Local: 48 tests, lint 0 errors / 39 warnings. |

### VPS state (207.180.240.29)

- **CrowdSec v1.8.2 + `crowdsec-firewall-bouncer` (nftables mode)** installed,
  running, console-enrolled. Acquisitions: caddy via journalctl + auth.log.
  Collections: caddy, base-http-scenarios, http-cve, sshd, linux,
  whitelist-good-actors. Admin IP allowlisted in
  `/etc/crowdsec/parsers/s02-enrich/99-local-allowlist.yaml`. A live ban test
  passed: an http-probing self-ban landed in the nft set and was reversed;
  ssh-bf bans are active. Packaging gotchas (all in `deploy/VPS.md`): remove
  `/var/lib/crowdsec/pending-registration`, delete the stale
  `crowdsec-firewall-bouncer.yaml.local` (it overrides the api_key), systemd
  drop-in with `Type=simple`, `mode: nftables` (iptables mode needs ipset).
- Repo pulled to `c2c3af6` (git as `thomas`), backend rebuilt with
  `docker compose up -d --build backend`, container healthy. Floors in the
  container: pydantic 2.14.3, uvicorn 0.54.0, jinja2 3.1.6 (httpx/pytest are
  dev-only, not in the runtime image).
- `deploy/verify.sh`: all checks PASS ("OK: every check passed").
- The restart dropped live walkers — expected; the geo index rebuilds from the
  next fix.

### Conventions still active

- Push every commit to **both** `github` and `origin`.
- **No sshd hardening yet** — still developing, root password SSH stays enabled
  on the VPS.
- Secrets live in `/c/Users/thoma/Downloads/.secrets` (`vps.sh` for SSH,
  `mikywalk-secrets.env` for app secrets) — never print values into transcripts.
- On the VPS, git must run as `thomas`:
  `su - thomas -c "cd ~/dog && git ..."`.

### Next up (details in ROADMAP.md)

1. **Report a walker** — reuses the block plumbing: a report is a block plus a
   row an operator can read.
2. **Email verification** — the general fix for invite-gate probing
   (`POST /auth/register` still answers 409 for a taken email after a spent
   code). `SmtpEmailSender` already exists behind `EMAIL_SENDER=smtp`, and the
   admin panel can configure SMTP and send a test mail.

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
host Caddy. Its checkout tracks `main`, pulled as `thomas` from `github` (root
has no deploy key) and synced again at the end of 2026-11-16. The deployed
backend image did not change that day: every box-side change was the `.env`
(JWT, SMTP), Redis (SMTP slot, replaced admin record) or the host
`/etc/caddy/Caddyfile` (rotated gate hash), each proven live where it runs.
`deploy/verify.sh` passes 16/16 there **as root** — as `thomas` two checks
fail on permissions, not on reality. The panel is
`https://backend.thomasfeyens.be/admin/ui`: Caddy user `thomas`, then app
login `thomas.feyens@gmail.com`, both passwords in `.secrets/mikywalk-secrets.env`.
See `deploy/VPS.md`.

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
`adb shell pm clear com.mikywalk`, re-grant the permissions — clearing wipes them:

```bash
adb shell pm grant com.mikywalk android.permission.ACCESS_FINE_LOCATION
adb shell pm grant com.mikywalk android.permission.ACCESS_COARSE_LOCATION
adb shell pm grant com.mikywalk android.permission.POST_NOTIFICATIONS
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
| `ios/Mikywalk/Location/WalkLocationManager.swift` | The Core Location config that makes the strategy work. Read this first. |
| `ios/Mikywalk/Remote/WalkSocketClient.swift` | `URLSessionWebSocketTask`, 1 s → 30 s backoff, 4401 → refresh, 20 s ping. |
| `ios/Mikywalk/Remote/Protocol.swift` | Wire frames, field names unchanged from Android. |
| `ios/Mikywalk/Session/WalkSession.swift` | Orchestration, entry/exit alerting, session renewal. |
| `ios/Mikywalk/Notifications/ProximityNotifier.swift` | One sound per relationship, chosen per notification. |
| `ios/Mikywalk/Model/Models.swift` | `WalkerLocation`, `NearbyWalker`, `DogRelationship`, `MarkedDog`. |
| `ios/Mikywalk/Resources/Info.plist` | `UIBackgroundModes: location` and When-In-Use only. |

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
(then — 207 now).

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
   why there are three proximity channels (`mikywalk_proximity`,
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

## Walk history — client-side aggregates, never a route

Stopping a walk (or losing the session mid-walk) records a `WalkSummary`: when it
started and ended, how far it went, and which dogs came within the warning
distance. Everything is derived from what the app already receives — its own GPS
fixes and the `PROXIMITY_ALERT` arrivals — so the backend is untouched and the
protocol unchanged.

| Concern | File |
| --- | --- |
| `WalkSummary`, `MetDog` | `model/Models.kt` |
| `WalkStatsRecorder` (per-walk accumulation) | `data/history/WalkStatsRecorder.kt` |
| `WalkHistoryStore` interface + list rules | `data/history/WalkHistoryStore.kt` |
| `PrefsWalkHistoryStore` (SharedPreferences JSON, capped at 100) | `data/history/PrefsWalkHistoryStore.kt` |
| Feeding the recorder, persisting on stop/sign-out | `data/repository/WalkRepository.kt` |
| History dialog, formatting | `ui/map/MapScreen.kt`, `MapViewModel.kt` |

Three things worth not undoing:

1. **Aggregates only.** No route and no coordinates are stored — the history file
   is not a position log, matching the rule that keeps coordinates off the wire.
   Like the marked-dogs list it lives in SharedPreferences and does not survive a
   reinstall (`allowBackup="false"`); if it ever needs to sync, that is a server
   store plus a privacy decision, same as friend groups.
2. **The distance guards.** A step shorter than the better fix's accuracy radius is
   GPS jitter and is not counted (the reference point stays put, so slow real
   movement still accumulates across the deadband); a step over
   `MAX_STEP_METERS` (250 m per 5 s fix) is a glitch and is dropped without moving
   the reference, so the jump back is not counted either.
3. **Dogs met = distinct walkers that arrived inside the warning distance**, fed
   from the same `Arrived` events the notifications use, so the count matches what
   actually buzzed.

Verified on `emulator-5554` against the local stack: started a walk with
`hold_walker.py --meters 60 --name Buddy`, moved the emulator 100 m mid-walk, stopped;
the dialog showed `51 s · 96 m · 1 dog met · Buddy`, and the remove button emptied
the list (`mikywalk_history.xml` → `[]`).

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

**The list is capped at `blocks.MAX_BLOCKS` (200) and the route is throttled.**
`POST /blocks/{id}` accepts any 32-hex id and writes two keys, and an id that
never belonged to an account is cleaned up by nothing except an unblock — so
without a ceiling a signed-in account could mint keys until Redis ran out of
memory. The cap answers 409, which is a property of the caller's own list and
says nothing about the id, so it is not the oracle that a 404 would be. Skipping
the write for an unknown id was the other option and is worse: the entry would
be missing from `GET /blocks`, which leaks the same thing more quietly. The
check is not atomic with the write, so two concurrent blocks can overshoot by
one; a soft ceiling is enough, because the point is that the number is bounded
rather than exact. `ratelimit.PREFIX_LIMITS` adds a 60/minute bucket keyed on the
`/blocks/` prefix — keyed on the full path, every id would get its own counter,
which is the same as no limit at all.

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
  capped at 20/hour. `LIMITS` is keyed on exact paths; `PREFIX_LIMITS` covers the
  routes whose last segment is an id, and the counter is keyed on the prefix
  rather than the path so a fresh id does not buy a fresh counter.
  `RATE_LIMIT_ENABLED=0` disables the limiter.
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
secret is still `DEV_JWT_SECRET` and `MIKYWALK_ENV` is not a development
environment, or when the secret is shorter than 32 bytes. A warning is not enough:
a deployment that logs a warning and carries on is a deployment where anyone who
has read this repository can mint a token for any account.

`docker-compose.yml` sets `MIKYWALK_ENV` and a real `JWT_SECRET`. If you see the
`RuntimeError` on a dev box, set `MIKYWALK_ENV=development`.

### Token storage is encrypted

`SecurePrefs` encrypts each value with a Keystore-backed AES-GCM key
(`AES/GCM/NoPadding`, 256-bit, alias `mikywalk_auth_key`) and stores `iv:ciphertext`
base64. A value that fails to decrypt is dropped rather than crashing the app.
`EncryptedSharedPreferences` was the obvious choice but is deprecated in
`androidx.security`.

`android:allowBackup="false"` and `fullBackupContent="false"` are set, so the
encrypted blob is not swept into a cloud backup that would restore it onto a device
without the Keystore key.

Verify it is actually encrypted:

```bash
export MSYS_NO_PATHCONV=1
adb shell run-as com.mikywalk cat /data/data/com.mikywalk/shared_prefs/mikywalk_auth.xml
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
  -validity 10000 -alias mikywalk
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

`pytest -q` → **207 passed**. `scripts/e2e.py` → **56/56 passed**.
`./gradlew :app:testDebugUnitTest` → **48 passed**. `./gradlew :app:lintDebug` →
**0 errors**, 46 warnings.

| Test file | Covers |
| --- | --- |
| `test_auth.py` | Registration, duplicate email, password rules, login, lockout, refresh rotation, revocation, token tampering |
| `test_proximity.py` | Alert round trip, leaving the radius, disconnect cleanup, frame validation, the 30 s expiry sweep, the GEO query, and that alerts carry no coordinates |
| `test_privacy.py` | Bearing cardinal directions, quantisation snapping, the error bound, the zero-quantum escape hatch |
| `test_security.py` | Password reset (send, single use, wrong code, attempt cap, no enumeration), account deletion, refresh replay grace vs. revocation, config validation, cache headers |
| `test_ratelimit.py` | Per-address throttling, per-address isolation, health exempt, limiter can be disabled, and the `/blocks/` prefix bucket shared across ids |
| `test_invites.py` | The gate, single use, refund on a taken email, and that a taken and a free address are indistinguishable without a code |
| `test_admin.py` | The admin/walker token boundary in both directions, admin lockout, user listing and paging, account creation and the forced password change it triggers, revoke and delete, invite minting, the audit log, and that no admin response carries a position |
| `test_admin_ui.py` | The admin cookie (HttpOnly, SameSite, path), redirects without one, the add-account form and its errors, the one-shot password notice, the delete confirmation page, the one-shot mint flash, and that no page mentions a coordinate |
| `test_blocks.py` | Blocking a walker: symmetric invisibility in both directions, unblock, the name kept for an offline walker, blocking yourself, a block surviving a new session, the 200-entry ceiling and its 409, and that deleting an account clears it from other block lists |

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
- `pytest -q` → 207 passed.
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
  `Live · 1 nearby`, and the notification fired on channel `mikywalk_proximity`
  (id 2001) with `Buddy is nearby / Buddy is 50 m away`.
- **Token storage is encrypted**: `run-as com.mikywalk cat
  shared_prefs/mikywalk_auth.xml` shows all four values as `iv:ciphertext` base64.
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
  - unmarked → marker amber, notification on `mikywalk_proximity`;
  - tapped Buddy's name tag → mark dialog → **Likes** → marker turned green
    (`#2E9E5B`, sampled from the screenshot at the same pixel bounds the amber
    marker had occupied) and the next arrival fired on
    `mikywalk_proximity_friend` with `color=0xff2e9e5b`;
  - **Does not like** → marker red (`#C62828`, same bounds) and the next arrival
    fired on `mikywalk_proximity_disliked` with `color=0xffc62828` and title
    `Watch out - Buddy is nearby`;
  - `dumpsys notification` shows the channels carry three different sounds:
    `mikywalk_proximity` → `content://settings/system/notification_sound`,
    `mikywalk_proximity_friend` → `android.resource://com.mikywalk/2131427330`
    (`res/raw/friend_chime.wav`), `mikywalk_proximity_disliked` →
    `android.resource://com.mikywalk/2131427329` (`res/raw/disliked_alarm.wav`),
    with vibration patterns `[0,80,90,80]` and `[0,400,120,400,120,400]`.
    Cross-check the ids against
    `app/build/intermediates/runtime_symbol_list/debug/processDebugResources/R.txt`
    — `int raw disliked_alarm 0x7f0b0001`, `int raw friend_chime 0x7f0b0002`.
  - settings sheet listed Buddy under **Dogs yours does not like**; the remove
    button cleared it, `marked_dogs` went to `[]`, the empty-state hint appeared,
    the marker returned to amber and the next arrival fired on the neutral
    `mikywalk_proximity` channel again.
  - `marked_dogs` in `shared_prefs/mikywalk_settings.xml` round-trips as
    `[{"id":"…","name":"Buddy","relationship":"FRIEND"}]` and survives a restart.
    Nothing about it is sent to the server — the backend was not touched and
    `pytest -q` was still 71 passed at the time.
- **The proximity alert is withdrawn when the last walker leaves the warning
  distance**, on `emulator-5554` with a scratch script that moved one walker on a
  single socket (a disconnect would have sent `PROXIMITY_CLEAR` and exercised a
  different path). Holding at 60 m: notification `id=2001` present on
  `mikywalk_proximity`, header `1 nearby`. Moved to 300 m — outside the 100 m
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
- **`deploy/verify.sh` on the VPS**: 16/16 PASS, including the Let's Encrypt
  issuer check and a 206 range request against `benelux.pmtiles`.

## Android tests

64 JVM tests in `android/app/src/test/`, run with
`./gradlew :app:testDebugUnitTest`. No emulator, no backend, no Robolectric.

| File | Covers |
| --- | --- |
| `data/auth/AuthStoreTest.kt` | The sign-in state machine: save, updateTokens, clear, and the three restore cases |
| `data/repository/WalkRepositoryTest.kt` | Proximity bookkeeping (arrival once, not per fix; a refresh when one of several leaves; withdrawal on the last leaving and on a clear frame) and the three session-renewal branches |
| `data/remote/WalkSocketClientTest.kt` | Connect, bearer header, name query parameter, frame decoding, 4401, reconnect, disconnect, the backoff ladder |
| `data/remote/HttpBaseTest.kt` | `httpBaseOf()`: `ws` → `http`, `wss` → `https`, the query string dropped, and a URL without the socket path used as-is |
| `data/history/WalkStatsRecorderTest.kt` | Distance accumulation, the jitter deadband, the glitch cap, dogs-met union, `finish` stamping |
| `data/history/WalkHistoryStoreTest.kt` | Newest-first insert, the 100-entry cap, remove-by-id, the in-memory fake |
| `geo/MovementDetectorTest.kt` | The stationary/moving rules: three fixes inside the 12 m band, a walker who keeps walking never goes stationary, 30 m to exit, noise does not flip the verdict, `reset` |
| `notifications/ProximityAlertPolicyTest.kt` | The sound rule: sound only when the most urgent walker present is more urgent than what is showing — friend next to warning is silent, disliked next to friend sounds, a second disliked does not replay |
| `testing/InMemorySecureStore.kt`, `testing/InMemoryWalkHistoryStore.kt` | Shared fakes, not tests |

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
| `backend` | `pytest -q` on Python 3.12, then `pip-audit --strict` | Nothing — the tests inject fakeredis |
| `android` | `:app:testDebugUnitTest :app:lintDebug` on JDK 17, then `osv-scanner scan --lockfile=app/gradle.lockfile` | Android SDK 36, installed by the job |
| `e2e` | `scripts/e2e.py` | `docker compose up -d --build`, then a health poll |

The two vulnerability scanners are gates. `pip-audit` audits the environment
pytest just installed; OSV-Scanner audits the committed
`android/app/gradle.lockfile`, which is written from `debugRuntimeClasspath`
and `releaseRuntimeClasspath` only. Neither needs a secret. See
`UPGRADING.md`, "Vulnerability scanning".

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

The suite has grown since: 207 backend tests and 54 Android tests as of the
2026-11-14 doc sync. The GitHub connector does expose check runs after all —
`pull_request_read` with `method: get_check_runs` returns per-job status and
conclusion for a PR's head commit, which is how the #13 failure was found. It
does **not** expose job logs, so a red job still means reproducing locally or
reading the Actions tab.

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

- **The Android tests cover the transport, the session, the repository, the auth
  store, the walk history, the movement rules and the alert rule, not the rest.**
  66 tests: `WalkSocketClient` (connect, bearer header, frame decoding, 4401,
  reconnect, the backoff ladder), `WalkRepository` (proximity bookkeeping, the
  session-renewal branches and recording the finished walk), `AuthStore` (the
  sign-in state machine), `HttpBase` (the `ws` → `http` mapping), `AuthApi` (the
  account-request call and its refusal),
  `WalkStatsRecorder` and `WalkHistoryStore`, `MovementDetector`,
  `ProximityAlertPolicy`. `SettingsStore`, the notification *posting* code and
  the Compose UI still have none.
- No reconnect backoff tuning — the client reconnects, but the retry cadence has
  not been stress-tested against a backend that stays down.
- ~~**The proximity alert is withdrawn when the last walker leaves the warning
  distance, but not when one of several leaves.**~~ Closed, in two steps. The
  alert is now rebuilt from the whole set inside the warning distance on every
  change, so it can never keep naming a walker that left: one
  `ProximityEvent.Changed` carries everyone still inside, and the text names the
  most urgent walker present, not the ones that just arrived. The sound is a
  separate decision that `ProximityAlertPolicy` owns — re-sound only when the
  most urgent walker present is more urgent than the one the user was last told
  about — which is also what stops a friend arriving next to a warning from
  replacing it with a chime, and a second dog of the same standing from replaying
  its alarm. A quiet rebuild returns early when the slot is empty, so it cannot
  bring back an alert the user dismissed with `setAutoCancel(true)`. See
  **Session log — 2026-11-16** for how the two changes fit together.
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
- ~~**Reset emails need SMTP credentials to actually arrive.**~~ Closed: a Gmail
  app password is configured on the VPS, in both places the design supports — the
  panel's Redis slot (`dogwalk:settings:smtp`) and the `.env` (`EMAIL_SENDER=smtp`,
  `ALLOW_LOGGING_EMAIL` removed). Port 465 SSL, the only outbound SMTP port the box
  opens. Verified end to end: the test message arrived, and a real
  `password-reset/request` created a code that appears nowhere in
  `docker compose logs backend`. See **Session log — 2026-11-16**.
- ~~**The admin panel has no second gate.**~~ Closed: `deploy/Caddyfile` puts
  Caddy `basic_auth` on `/admin*` (user `thomas`) ahead of the app's own login, and
  an unauthenticated request gets 401. The hash is bcrypt — rotate it with
  `caddy hash-password` and reload. The app's own lockout is tighter than the
  walker one on purpose — 5 failures in 15 minutes, because a handful of operator
  accounts can each delete every account in the deployment.
- ~~**There is no operator password-change flow.**~~ Closed 2026-11-17:
  `POST /admin/auth/password` and `/admin/ui/password` change the operator's own
  password, and both require the current one. `scripts/create_admin.py` still
  creates but **refuses an existing email** (`admin.EmailTaken`), so it is now the
  recovery path for a lost password rather than the rotation path.
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
- **Flash ids land in the Caddy access log for up to 120 s.** The invite codes
  and one-time passwords themselves never do — only the single-use Redis key id
  that fetches them, valid for 120 s and deleted on read. Accepted: the log is
  readable only by the operator, and the alternative (POST-body round trips)
  costs more than it buys at this scale.
- **`dev_server.py` and the README's no-Docker instructions bind `0.0.0.0:8000`.**
  Development only: without Caddy there is no TLS and `X-Forwarded-For` is
  spoofable, so the rate limiter can be bypassed. Never expose that port outside
  a dev machine.

## Environment facts worth not rediscovering

- JDK: `C:\Program Files\Android\Android Studio\jbr` (JDK 25)
- **No Swift toolchain on this machine.** `swift` and `swiftc` are absent, and
  iOS targets cannot be built on Windows at all. The `ios/` directory can only be
  compiled on a Mac with Xcode 15 or later.
- Gradle 9.9.1, AGP 9.5.0, Kotlin 2.4.21, KSP 2.3.14, Hilt 2.60.1,
  play-services-location 21.4.0, coroutines 1.11.0,
  Compose BOM 2025.09.00, MapLibre 12.0.0, OkHttp 4.12.0.
  AGP 9 has built-in Kotlin support: `org.jetbrains.kotlin.android` is removed
  from the build files (it fails to apply under AGP 9); the Compose and
  serialization plugins stay.
- **The Android vulnerability gate reads `android/app/gradle.lockfile`**, not
  the buildscript classpath. Regenerate it with
  `./gradlew :app:dependencies --write-locks` after any dependency change;
  `--write-locks` also drops a `settings-gradle.lockfile` next to it, which is
  gitignored. OSV-Scanner 2.7.1 is pinned in `ci.yml` with its SHA-256.
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
- `adb shell pm clear com.mikywalk` resets the dog name to a new random one. If you
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

The counts in `README.md` and `ROADMAP.md` (207 tests, 56 e2e checks) describe
this state. Re-run the gates to confirm the baseline before starting new work —
`pytest` needs nothing, `e2e.py` needs the Docker stack up (see **Resuming**):

```bash
cd /c/Users/thoma/Downloads/dog/backend
.venv/Scripts/python.exe -m pytest -q                    # expect 207 passed
.venv/Scripts/python.exe scripts/e2e.py                  # expect 56/56
.venv/Scripts/python.exe -m ruff check --select F,E9 app/ tests/ scripts/
```
