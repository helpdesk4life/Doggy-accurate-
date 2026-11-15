# Upgrading dependencies

One place to follow when any dependency moves. The rule: **one dependency group
per pull request**, run the repo's own gates, and update the "current" column
here plus the environment facts in `HANDOFF.md` so the next person does not
rediscover versions.

Dependabot opens a weekly PR per ecosystem (`pip` in `backend/`, `gradle` in
`android/`, `github-actions` at the root — see `.github/dependabot.yml`).
Review and merge those like any other PR: the gates below still apply.

## Inventory

| Group | Pinned in | Current | Check latest |
| --- | --- | --- | --- |
| Backend runtime deps | `backend/requirements.txt` | floors: fastapi>=0.115, uvicorn[standard]>=0.54.0, redis>=5.2, pydantic>=2.14.3, pyjwt>=2.9, jinja2>=3.1.6, python-multipart>=0.0.9 | PyPI per package |
| Backend dev/test deps | `backend/requirements-dev.txt` | adds pytest>=9.2.0, httpx>=0.28.1, fakeredis>=2.26 | PyPI per package |
| Python interpreter | `backend/Dockerfile` `FROM`, `ci.yml` `python-version` | 3.12 (`python:3.12-slim`) | Docker Hub / python.org |
| Android libraries | `android/gradle/libs.versions.toml` | AGP 9.5.0, Kotlin 2.4.21, KSP 2.3.14, Hilt 2.60.1, play-services-location 21.4.0, coroutines 1.11.0, Compose BOM 2025.09.00, MapLibre 12.0.0, OkHttp 4.12.0, … | Maven Central / developer.android.com |
| Android SDK levels | `android/app/build.gradle.kts` (`compileSdk`/`targetSdk` 36, `minSdk` 26), `ci.yml` `packages:` | 36 / 26 | developer.android.com |
| Gradle wrapper | `android/gradle/wrapper/gradle-wrapper.properties` | 9.9.1 | services.gradle.org |
| Docker images | `backend/docker-compose.yml`, `backend/Dockerfile` | redis:7-alpine, caddy:2-alpine, python:3.12-slim | Docker Hub |
| GitHub Actions | `.github/workflows/ci.yml` `uses:` | checkout@v7, setup-python@v7, setup-java@v6, setup-android@v4, setup-gradle@v6, cache@v6, upload-artifact@v7 | GitHub Marketplace; Dependabot already bumps these |
| Vulnerability scanners | `ci.yml` audit steps, `app/build.gradle.kts` `dependencyCheck` | pip-audit (installed fresh in CI), OWASP dependency-check 13.0.0, `failBuildOnCVSS` 7.0 | PyPI / Gradle plugin portal |

## Runbooks

### Backend Python packages (`requirements*.txt`)

```bash
cd backend
.venv/bin/pip install -U -r requirements-dev.txt   # preview what moves
.venv/bin/python -m pytest -q                       # gate: 207 tests
.venv/bin/python -m ruff check --select F,E9 app/ tests/ scripts/
docker compose up -d --build                        # then:
.venv/bin/python scripts/e2e.py                     # gate: 56/56 against real Redis
```

Keep the floors (`>=`) as floors: raise them only when a feature or fix is
actually needed, so Dependabot can move the installed versions freely in
between. CI runs the same pytest on Python 3.12 — the interpreter version is
the one thing to change consciously (below).

### Python interpreter (3.12)

Change three places together: `FROM python:3.12-slim` in `backend/Dockerfile`,
`python-version: "3.12"` in the `backend` and `e2e` jobs of
`.github/workflows/ci.yml`, and the backend section of `README.md`. Then let CI
prove it: the backend job comment says testing the shipping interpreter is the
point. Do not move the interpreter and the libraries in the same PR.

### Android libraries (`libs.versions.toml`)

Bump versions in the catalog only — the build files reference
`version.ref = …`, so one edit updates every module.

Compatibility to check before building (in this order):

- AGP requires a minimum Gradle — `gradle-wrapper.properties` must satisfy it.
  Gradle's own compatibility matrix caps what it has been tested with
  (AGP 9.0–9.5.0-alpha at Gradle 9.9), so check both directions.
- AGP 9 has built-in Kotlin support: `org.jetbrains.kotlin.android` no longer
  applies and fails the build — remove it from the root and app `plugins {}`
  blocks and from the catalog. The Compose and serialization plugins stay.
- KSP is no longer version-locked to Kotlin since 2.3 (`2.3.14` style); use the
  latest 2.x from Maven Central and let the build prove the pair.
- Compose BOM, Hilt and `hiltCompose` move on their own calendars; the BOM
  overrides the individual Compose artifacts.
- MapLibre needs Android 11.7+ for PMTiles — we are on 12.0.0, do not go below.

```bash
cd android
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"   # Windows; JDK 17+ required
./gradlew :app:testDebugUnitTest :app:lintDebug --continue --console=plain
```

Gates: unit tests + lint 0 errors (CI runs exactly this). If `compileSdk`
moves, also update `packages: "platforms;android-XX build-tools;XX.0.0"` in
`ci.yml` in the same PR.

### Gradle wrapper

```bash
cd android
./gradlew wrapper --gradle-version 9.5.1   # put the new version here
```

Then run the Android gates above. AGP has a maximum supported Gradle — check
the AGP release notes before going up.

### Docker images

Bump the tags in `backend/docker-compose.yml` (`redis:7-alpine`,
`caddy:2-alpine`) and the `FROM` in `backend/Dockerfile`. Redis major bumps
can change persistence format — snapshot the `redis-data` volume before
upgrading the VPS. Caddy 2.x → keep `basic_auth` (2.8+); a Caddy 3 bump is a
bigger conversation, see `deploy/verify.sh` section 1. Verify locally with
`docker compose up -d --build` + `scripts/e2e.py`, then on the VPS with
`deploy/verify.sh` (16/16 expected).

### GitHub Actions

Already automated: Dependabot bumps the `uses:` majors weekly
(`package-ecosystem: github-actions`). Just review the PR and let CI run —
the three jobs (backend pytest, Android tests+lint, e2e) are the check.

### Vulnerability scanning

Both scanners are gates in `ci.yml`, so every PR and every run on `main` is
audited — including Dependabot's weekly bumps.

- Backend: a `pip-audit` step in the `backend` job audits the environment
  pytest just installed — transitive dependencies included. pip is upgraded
  first so the runner's own pip cannot produce findings that are not project
  dependencies; `--strict` fails if dependency collection breaks. Reproduce
  locally with `pip install -U pip pip-audit && pip-audit --strict` inside
  the backend venv.
- Android: the `org.owasp.dependencycheck` Gradle plugin (version catalog
  entry `owaspDependencyCheck`, applied in `app/build.gradle.kts`) runs as
  `:app:dependencyCheckAnalyze` in the `android` job. The report lands in
  `app/build/reports/dependency-check/` and is uploaded with the other
  Android reports when the job fails.
- `failBuildOnCVSS` is 7.0: findings at CVSS >= 7 fail CI, softer ones are
  report-only. A true finding is fixed by bumping the dependency in the same
  PR (this file's runbooks); a false positive (CPE mis-matches happen on
  Android) goes into a suppressions file wired up via `suppressionFile` in
  the `dependencyCheck` block — never silence a whole dependency.
- The NVD database is cached between runs via `actions/cache` on
  `~/.dependency-check`.
- **An `NVD_API_KEY` repo secret is required, not optional.** Request one at
  <https://nvd.nist.gov/developers/request-an-api-key> (Developers → Request
  an API Key; the form asks for an organisation name, an email and an
  organisation type, and the key arrives by mail). NIST answers `403` to
  non-browser clients, so a `curl` against that URL looks like a dead link —
  open it in a browser. Without the secret the scan step is skipped rather
  than failed, so CI stays green but the Android dependency gate is off. With
  it, the limit is 50 requests per 30s instead of 5.
- dependency-check 13.0.0 cannot run keyless: it ships `nvd.api.key=` as an
  empty default in `dependencycheck.properties`, and `NvdApiDataSource` only
  null-checks it, so the empty string reaches the NVD client and aborts the
  update with `Invalid API Key, length of 0 too short to provided a masked
  partial key`. 12.2.2 has no such default and does start the update, but at
  5 requests per 30s the 425,838-record cold download does not fit the job
  timeout. Do not "fix" this by downgrading — add the key.

## Universal checklist (every dependency PR)

1. One group per PR; say what moved and why in the PR body.
2. Local gates green: `pytest -q`, ruff `F,E9`, `scripts/e2e.py`,
   `./gradlew :app:testDebugUnitTest :app:lintDebug` as applicable.
   The vulnerability scanners (`pip-audit`, `:app:dependencyCheckAnalyze`)
   run in CI as part of the same gate — see "Vulnerability scanning" above.
3. CI green on the PR: backend, android, e2e jobs.
4. Deploy to the VPS, then `sudo bash deploy/verify.sh` → 16/16 PASS.
5. Update this file's "current" column and `HANDOFF.md`'s environment facts.

## Known gap

Closed 2026-11-15 (TEST-REPORT recommendation 4): both scanners described
under "Vulnerability scanning" above are wired into CI as gates. Dependabot's
`security-updates` ride along with the weekly PRs and are audited by the same
gates before merge.
