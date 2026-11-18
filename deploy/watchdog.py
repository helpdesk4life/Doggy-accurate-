#!/usr/bin/env python3
"""Watch the Mikywalk deployment and email when something breaks.

    python3 deploy/watchdog.py              # one pass; alerts on change
    python3 deploy/watchdog.py --dry-run    # print the checks, change nothing
    python3 deploy/watchdog.py --test-email # prove the SMTP path works

Run from cron every five minutes as `thomas`. It is the unattended counterpart
to `deploy/verify.sh`: verify.sh is sixteen checks you read, this is nine checks
nobody reads until one of them fails.

**What this cannot do.** It runs on the box it watches, so if the box dies it
dies too and the silence is indistinguishable from health. That is what
`HEARTBEAT_URL` is for: every run pings an external service, and the *absence*
of the ping is the alert. Without it, monitoring covers a dead container but not
a dead machine. Set it up — healthchecks.io's free tier is enough — and paste
the URL into `/etc/default/mikywalk-watchdog`.

**Why email and not a dashboard.** The SMTP credentials are already on the box
and already proven, so alerting costs no new dependency and no new public
surface. A dashboard is a fine thing to add later; it is not what wakes you up.

**Why it only mails on change.** A check that has been failing for a week should
not send 2016 emails. Each check keeps its last state in `STATE_FILE`; a
transition sends one message, a recovery sends one, and a failure that persists
re-sends every `REALERT_HOURS` so it cannot be quietly forgotten.

Every check is wrapped: an exception inside one becomes a failed check, not a
dead watchdog. A monitor that stops monitoring is worse than no monitor.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

HINT_HEALTH = "cd /home/thomas/dog/backend && docker compose logs --tail 50 backend"
HINT_MAPS = "check /srv/maps and the maps site in /etc/caddy/Caddyfile"
HINT_CADDY = "systemctl status caddy; journalctl -u caddy -n 50 --no-pager"
HINT_CONTAINERS = "cd /home/thomas/dog/backend && docker compose ps && docker compose logs --tail 50"
HINT_REDIS = "docker exec backend-redis-1 redis-cli INFO memory; check maxmemory and the volume"
HINT_DISK = "du -xh / --max-depth=1 | sort -h | tail; docker system df"
HINT_MEMORY = "docker stats --no-stream; look for a runaway process"
HINT_CERT = "systemctl reload caddy; check the box can still reach the ACME endpoint"
HINT_BACKUP = "bash /home/thomas/dog/deploy/backup-redis.sh; tail /home/thomas/backups/backup.log"


@dataclass
class Result:
    """One check. ``ok is None`` means skipped, which is never an alert."""

    name: str
    ok: bool | None
    detail: str
    hint: str = ""


@dataclass
class Config:
    backend_host: str = "backend.thomasfeyens.be"
    maps_host: str = "maps.thomasfeyens.be"
    maps_file: str = "benelux.pmtiles"
    compose_file: Path = REPO / "backend" / "docker-compose.yml"
    env_file: Path = REPO / "backend" / ".env"
    state_file: Path = Path("/home/thomas/monitoring/state.json")
    backup_dir: Path = Path("/home/thomas/backups")
    services: tuple[str, ...] = ("backend", "redis", "redis-geo")
    alert_to: str = ""
    heartbeat_url: str = ""
    timeout: float = 15.0
    disk_max_pct: float = 85.0
    mem_min_pct: float = 10.0
    cert_min_days: float = 14.0
    backup_max_hours: float = 36.0
    realert_hours: float = 6.0
    redis_min_keys: int = 1

    @classmethod
    def from_env(cls) -> "Config":
        # /etc/default/mikywalk-watchdog is read first so that a manual run and a
        # cron run behave identically. Cron does not source it, and a monitor
        # that only works when you run it by hand is not a monitor.
        defaults = read_env_file(Path(os.environ.get("DEFAULTS_FILE", "/etc/default/mikywalk-watchdog")))

        def lookup(name: str, fallback):
            return os.environ.get(name, defaults.get(name, fallback))

        cfg = cls()
        cfg.backend_host = lookup("BACKEND_HOST", cfg.backend_host)
        cfg.maps_host = lookup("MAPS_HOST", cfg.maps_host)
        cfg.maps_file = lookup("MAPS_FILE", cfg.maps_file)
        cfg.compose_file = Path(lookup("COMPOSE_FILE", cfg.compose_file))
        cfg.env_file = Path(lookup("ENV_FILE", cfg.env_file))
        cfg.state_file = Path(lookup("STATE_FILE", cfg.state_file))
        cfg.backup_dir = Path(lookup("BACKUP_DIR", cfg.backup_dir))
        cfg.services = tuple(str(lookup("SERVICES", " ".join(cfg.services))).split())
        cfg.alert_to = lookup("ALERT_TO", cfg.alert_to)
        cfg.heartbeat_url = lookup("HEARTBEAT_URL", cfg.heartbeat_url)
        cfg.timeout = float(lookup("TIMEOUT_SECONDS", cfg.timeout))
        cfg.disk_max_pct = float(lookup("DISK_MAX_PCT", cfg.disk_max_pct))
        cfg.mem_min_pct = float(lookup("MEM_MIN_PCT", cfg.mem_min_pct))
        cfg.cert_min_days = float(lookup("CERT_MIN_DAYS", cfg.cert_min_days))
        cfg.backup_max_hours = float(lookup("BACKUP_MAX_HOURS", cfg.backup_max_hours))
        cfg.realert_hours = float(lookup("REALERT_HOURS", cfg.realert_hours))
        cfg.redis_min_keys = int(lookup("REDIS_MIN_KEYS", cfg.redis_min_keys))
        return cfg


# --- checks ----------------------------------------------------------------


def check_health(cfg: Config) -> Result:
    """One request that crosses DNS, TLS, Caddy, the proxy and the app."""
    url = f"https://{cfg.backend_host}/health"
    try:
        with urllib.request.urlopen(url, timeout=cfg.timeout) as response:
            body = response.read(200).decode("utf-8", "replace").strip()
            if response.status != 200:
                return Result("health", False, f"{url} answered {response.status}", HINT_HEALTH)
            if '"ok"' not in body:
                return Result("health", False, f"{url} answered 200 with {body!r}", HINT_HEALTH)
            return Result("health", True, f"{url} 200 {body}")
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        return Result("health", False, f"{url} failed: {exc}", HINT_HEALTH)


def check_maps(cfg: Config) -> Result:
    """206, not 200: a 200 means range requests broke and the map will not load."""
    url = f"https://{cfg.maps_host}/{cfg.maps_file}"
    request = urllib.request.Request(url, headers={"Range": "bytes=0-16383"})
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            if response.status == 206:
                return Result("maps", True, f"{url} 206")
            return Result(
                "maps",
                False,
                f"{url} answered {response.status}; range support is broken",
                HINT_MAPS,
            )
    except Exception as exc:  # noqa: BLE001
        return Result("maps", False, f"{url} failed: {exc}", HINT_MAPS)


def check_caddy(cfg: Config) -> Result:
    """The systemd Caddy is the one on :443; the compose one only publishes 8443."""
    if shutil.which("systemctl") is None:
        return Result("caddy", None, "systemctl not available; skipped")
    try:
        done = subprocess.run(
            ["systemctl", "is-active", "caddy"],
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return Result("caddy", False, f"systemctl failed: {exc}", HINT_CADDY)
    state = done.stdout.strip()
    if state == "active":
        return Result("caddy", True, "systemd caddy is active")
    return Result("caddy", False, f"systemd caddy is {state or 'unknown'}", HINT_CADDY)


def compose_ps(cfg: Config) -> list[dict]:
    # -a, so a stopped container reports as "exited" rather than "missing".
    # "missing" sends you looking for a deleted service; "exited" tells you what
    # happened.
    done = subprocess.run(
        ["docker", "compose", "-f", str(cfg.compose_file), "ps", "-a", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=cfg.timeout,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"docker compose ps exited {done.returncode}")
    text = done.stdout.strip()
    if not text:
        return []
    # Compose emits one object per line; some versions emit a single array.
    if text.startswith("["):
        return json.loads(text)
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def check_containers(cfg: Config) -> Result:
    try:
        rows = compose_ps(cfg)
    except Exception as exc:  # noqa: BLE001
        return Result("containers", False, f"docker compose ps failed: {exc}", HINT_CONTAINERS)

    by_service = {row.get("Service"): row for row in rows}
    problems = []
    for service in cfg.services:
        row = by_service.get(service)
        if row is None:
            problems.append(f"{service} is missing")
            continue
        state = row.get("State", "unknown")
        health = row.get("Health", "")
        if state != "running":
            problems.append(f"{service} is {state}")
        elif health and health != "healthy":
            problems.append(f"{service} is running but {health}")

    if problems:
        return Result("containers", False, "; ".join(problems), HINT_CONTAINERS)
    return Result("containers", True, f"{', '.join(cfg.services)} running and healthy")


def check_redis(cfg: Config) -> Result:
    """A Redis that restarted empty passes every other check and has lost the accounts."""
    try:
        rows = compose_ps(cfg)
    except Exception as exc:  # noqa: BLE001
        return Result("redis", False, f"docker compose ps failed: {exc}", HINT_REDIS)

    container = next((row.get("Name") for row in rows if row.get("Service") == "redis"), None)
    if not container:
        return Result("redis", False, "no redis container in the compose project", HINT_REDIS)

    try:
        ping = subprocess.run(
            ["docker", "exec", container, "redis-cli", "PING"],
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
        )
        if ping.stdout.strip() != "PONG":
            return Result("redis", False, f"PING answered {ping.stdout.strip()!r}", HINT_REDIS)

        size = subprocess.run(
            ["docker", "exec", container, "redis-cli", "DBSIZE"],
            capture_output=True,
            text=True,
            timeout=cfg.timeout,
        )
        keys = int(size.stdout.strip() or 0)
    except Exception as exc:  # noqa: BLE001
        return Result("redis", False, f"redis-cli failed: {exc}", HINT_REDIS)

    if keys < cfg.redis_min_keys:
        return Result(
            "redis",
            False,
            f"PONG but only {keys} keys; the account store looks empty",
            HINT_REDIS,
        )
    return Result("redis", True, f"PONG, {keys} keys")


def check_disk(cfg: Config) -> Result:
    try:
        usage = shutil.disk_usage("/")
    except Exception as exc:  # noqa: BLE001
        return Result("disk", None, f"disk usage unavailable: {exc}")
    pct = 100.0 * usage.used / usage.total
    detail = f"{pct:.0f}% used, {usage.free // 2**30} GiB free"
    return Result("disk", pct < cfg.disk_max_pct, detail, HINT_DISK)


def check_memory(cfg: Config) -> Result:
    path = Path("/proc/meminfo")
    if not path.exists():
        return Result("memory", None, "/proc/meminfo not available; skipped")
    values = {}
    for line in path.read_text().splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            values[key] = int(fields[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total == 0:
        return Result("memory", None, "MemTotal missing; skipped")
    pct = 100.0 * available / total
    detail = f"{pct:.0f}% available, {available // 1024} MiB of {total // 1024} MiB"
    return Result("memory", pct >= cfg.mem_min_pct, detail, HINT_MEMORY)


def check_certificate(cfg: Config) -> Result:
    """The default context verifies the chain, so a broken chain fails here too."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((cfg.backend_host, 443), timeout=cfg.timeout) as sock:
            with context.wrap_socket(sock, server_hostname=cfg.backend_host) as tls:
                cert = tls.getpeercert()
    except Exception as exc:  # noqa: BLE001
        return Result(
            "certificate",
            False,
            f"TLS handshake with {cfg.backend_host}:443 failed: {exc}",
            HINT_CERT,
        )

    not_after = cert.get("notAfter")
    if not not_after:
        return Result("certificate", False, "the certificate has no notAfter", HINT_CERT)
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
    days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    detail = f"expires {expires:%Y-%m-%d} ({days:.0f} days)"
    return Result("certificate", days >= cfg.cert_min_days, detail, HINT_CERT)


def check_backup(cfg: Config) -> Result:
    """A backup that stopped running leaves yesterday's file looking fine by name."""
    if not cfg.backup_dir.is_dir():
        return Result("backup", False, f"{cfg.backup_dir} does not exist", HINT_BACKUP)
    files = list(cfg.backup_dir.glob("redis-*.rdb.gz"))
    if not files:
        return Result("backup", False, f"no redis-*.rdb.gz in {cfg.backup_dir}", HINT_BACKUP)
    newest = max(files, key=lambda path: path.stat().st_mtime)
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    detail = f"{newest.name} is {age_hours:.1f}h old"
    return Result("backup", age_hours <= cfg.backup_max_hours, detail, HINT_BACKUP)


CHECKS = (
    check_health,
    check_maps,
    check_caddy,
    check_containers,
    check_redis,
    check_disk,
    check_memory,
    check_certificate,
    check_backup,
)


def run_checks(cfg: Config) -> list[Result]:
    results = []
    for check in CHECKS:
        name = check.__name__.removeprefix("check_")
        try:
            results.append(check(cfg))
        except Exception as exc:  # noqa: BLE001 - a broken check is a finding
            results.append(Result(name, False, f"the check itself raised: {exc}"))
    return results


# --- email -----------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def send_email(cfg: Config, subject: str, body: str) -> None:
    settings = read_env_file(cfg.env_file)
    host = settings.get("SMTP_HOST", "")
    sender = settings.get("SMTP_FROM", "") or settings.get("SMTP_USERNAME", "")
    recipient = cfg.alert_to or sender
    if not host or not sender or not recipient:
        raise RuntimeError(
            f"no SMTP configuration in {cfg.env_file} (need SMTP_HOST and SMTP_FROM) "
            "and ALERT_TO is unset"
        )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    port = int(settings.get("SMTP_PORT", "587"))
    security = settings.get("SMTP_SECURITY", "starttls")
    username = settings.get("SMTP_USERNAME", "")
    password = settings.get("SMTP_PASSWORD", "")

    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=cfg.timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=cfg.timeout)
    try:
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
        if username:
            client.login(username, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            client.close()


def format_alert(results: list[Result], hostname: str, now: datetime) -> tuple[str, str]:
    bad = [result for result in results if result.ok is False]
    good = [result for result in results if result.ok is True]
    skipped = [result for result in results if result.ok is None]

    names = ", ".join(result.name for result in bad)
    subject = f"[Mikywalk] {len(bad)} check(s) failing: {names}"

    lines = [
        f"Mikywalk watchdog on {hostname}",
        f"{now:%Y-%m-%d %H:%M:%S %Z}",
        "",
        f"FAILING ({len(bad)})",
    ]
    for result in bad:
        lines.append(f"  {result.name:<12} {result.detail}")
        if result.hint:
            lines.append(f"  {'':<12} -> {result.hint}")
    lines.append("")
    lines.append(f"PASSING ({len(good)}): {', '.join(r.name for r in good) or 'none'}")
    if skipped:
        lines.append(f"SKIPPED ({len(skipped)}): {', '.join(r.name for r in skipped)}")
    lines.append("")
    lines.append("This message repeats every few hours while the failure lasts.")
    return subject, "\n".join(lines)


def format_recovery(results: list[Result], hostname: str, now: datetime) -> tuple[str, str]:
    subject = "[Mikywalk] recovered: every check passes"
    lines = [
        f"Mikywalk watchdog on {hostname}",
        f"{now:%Y-%m-%d %H:%M:%S %Z}",
        "",
        f"All {len([r for r in results if r.ok is True])} checks pass again.",
        "",
    ]
    for result in results:
        mark = "ok  " if result.ok else ("skip" if result.ok is None else "BAD ")
        lines.append(f"  {mark} {result.name:<12} {result.detail}")
    return subject, "\n".join(lines)


# --- state -----------------------------------------------------------------


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"checks": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def ping_heartbeat(url: str, healthy: bool, timeout: float) -> str:
    """Tell an external service we are alive. Its silence is the real alert."""
    if not url:
        return "no HEARTBEAT_URL set, so a dead box stays silent"
    target = url if healthy else url.rstrip("/") + "/fail"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            return f"heartbeat {response.status}"
    except Exception as exc:  # noqa: BLE001
        return f"heartbeat failed: {exc}"


# --- main ------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Watch the Mikywalk deployment.")
    parser.add_argument("--dry-run", action="store_true", help="print the checks, change nothing")
    parser.add_argument("--test-email", action="store_true", help="send one test message and exit")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    hostname = socket.gethostname()
    now = datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")

    if args.test_email:
        send_email(
            cfg,
            "[Mikywalk] watchdog test message",
            f"If you are reading this, the watchdog can reach {cfg.env_file} and send mail.\n"
            f"Sent from {hostname} at {stamp}.\n",
        )
        print(f"{stamp} test email sent")
        return 0

    results = run_checks(cfg)
    bad = [result for result in results if result.ok is False]

    if args.dry_run:
        for result in results:
            mark = "ok  " if result.ok else ("skip" if result.ok is None else "BAD ")
            print(f"{mark} {result.name:<12} {result.detail}")
            if result.ok is False and result.hint:
                print(f"     {'':<12} -> {result.hint}")
        print(f"\n{len(bad)} of {len(results)} checks failing")
        return 1 if bad else 0

    state = load_state(cfg.state_file)
    previous = state.get("checks", {})

    newly_bad = []
    for result in bad:
        before = previous.get(result.name, {})
        if before.get("ok") is not False:
            newly_bad.append(result)
            continue
        alerted_at = before.get("alerted_at")
        if not alerted_at:
            newly_bad.append(result)
            continue
        try:
            age = now.timestamp() - datetime.fromisoformat(alerted_at).timestamp()
        except ValueError:
            age = cfg.realert_hours * 3600
        if age >= cfg.realert_hours * 3600:
            newly_bad.append(result)

    recovered = [
        result
        for result in results
        if result.ok is True and previous.get(result.name, {}).get("ok") is False
    ]

    sent = "nothing to send"
    if newly_bad:
        subject, body = format_alert(results, hostname, now)
        try:
            send_email(cfg, subject, body)
            sent = f"alert sent for {', '.join(r.name for r in newly_bad)}"
        except Exception as exc:  # noqa: BLE001
            sent = f"ALERT NOT SENT: {exc}"
    elif recovered and not bad:
        subject, body = format_recovery(results, hostname, now)
        try:
            send_email(cfg, subject, body)
            sent = f"recovery sent for {', '.join(r.name for r in recovered)}"
        except Exception as exc:  # noqa: BLE001
            sent = f"RECOVERY NOT SENT: {exc}"

    checks = {}
    for result in results:
        before = previous.get(result.name, {})
        changed = before.get("ok") is not result.ok
        checks[result.name] = {
            "ok": result.ok,
            "detail": result.detail,
            "since": stamp if changed else before.get("since", stamp),
            "alerted_at": stamp if result in newly_bad else before.get("alerted_at"),
        }
    save_state(cfg.state_file, {"checks": checks, "last_run": stamp})

    heartbeat = ping_heartbeat(cfg.heartbeat_url, not bad, cfg.timeout)

    summary = "ok" if not bad else f"BAD {', '.join(r.name for r in bad)}"
    print(f"{stamp} {summary} ({len(results)} checks) | {sent} | {heartbeat}")
    for result in bad:
        print(f"    {result.name}: {result.detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
