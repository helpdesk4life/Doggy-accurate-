# Security Policy

DogWalk is a dog-walking app that tells walkers when another walker comes within
a configurable distance: an Android app (`android/`), a FastAPI + WebSocket
backend with an admin panel (`backend/`), an iOS port in progress (`ios/`), and
the deployment config for the production VPS (`deploy/`).

## Supported Versions

DogWalk has no versioned releases; it is deployed from source. Only the latest
commit on `main` receives security fixes.

| Version        | Supported          |
| -------------- | ------------------ |
| latest `main`  | :white_check_mark: |
| older commits  | :x:                |

## Reporting a Vulnerability

Please report vulnerabilities **privately**, never in a public issue, pull
request, or discussion.

1. Go to the [Security tab](https://github.com/helpdesk4life/doggy/security)
   of this repository and choose **"Report a vulnerability"** (GitHub private
   vulnerability reporting). This creates a private advisory visible only to
   the maintainers.
2. If that button is unavailable, open a draft security advisory directly at
   `https://github.com/helpdesk4life/doggy/security/advisories/new` and keep
   it draft until a maintainer responds.

Include whatever you have: affected component, reproduction steps, the
request/frame involved, impact, and any proof of concept. If you found a
secret or credential in the wild, do not post it — describe where it is
leaked and revoke/rotate it first.

What to expect:

- **Acknowledgement** within 7 days.
- **Triage and a severity assessment** within 14 days, communicated in the
  private advisory.
- A fix on `main` and, where practical, a note in the advisory when it lands.
  We will credit reporters in the advisory unless you prefer not to be named.
- Please give us a reasonable window to fix before any public disclosure;
  we will ask for an extension rather than surprise you if a fix needs more
  time.

## Scope

In scope:

- The backend HTTP/WebSocket API, authentication and session handling, the
  admin API and admin panel (`backend/app/`).
- The Android app and its build config (`android/`), and the iOS port
  (`ios/`).
- The deployment configuration (`deploy/`, `backend/Caddyfile`,
  `backend/docker-compose.yml`), including TLS and the admin-panel gate.
- Proximity/privacy logic: anything that could leak a walker's absolute
  position (`app/privacy.py`, `app/proximity.py`, `app/blocks.py`).

Out of scope:

- Denial of service against a single-instance development deployment.
- Vulnerabilities in third-party dependencies that have no demonstrated
  exploit path in this app (report those upstream; Dependabot tracks our
  GitHub Actions).
- Social engineering, or attacks against the operator's personal accounts or
  machines.

## Known, documented non-issues

Do not report these — they are deliberate and already gated:

- The built-in development `JWT_SECRET` in `backend/app/config.py`. It is
  public on purpose so an unconfigured deployment is easy to spot, and the
  backend **refuses to boot in production** with it (verified:
  `DOGWALK_ENV=production` raises at startup).
- `EMAIL_SENDER=logging` in development, which writes password-reset codes to
  the log. Also refused in production unless explicitly acknowledged.
- The debug-only network security config that trusts the local Caddy CA
  (`android/app/src/debug/`). Release builds use the system store only.
- Redis and the plaintext backend published on `127.0.0.1` only in
  `docker-compose.yml`; Caddy on 8443 is the only external entry point.
- Live GPS positions never reach disk: the geo index lives on `redis-geo`
  (`GEO_REDIS_URL` in `backend/docker-compose.yml`), which runs with persistence
  off and no published port. The persistent Redis AOF does hold accounts, the
  SMTP password and the audit log — protect the disk (LUKS) accordingly; see
  `deploy/VPS.md`.
- `/ws/walk` is throttled in code, not configuration: 20 handshakes/min per IP
  before the handshake is accepted (close 4429), frames capped at 4 KiB (close
  1009), one evaluated location fix per second per connection.
- Admin sessions are revocable server-side: every admin token carries a `jti`
  registered in Redis at login; logout (`/admin/ui/logout`, `POST
  /admin/auth/logout`) deletes it. HSTS is set in `deploy/Caddyfile`.
