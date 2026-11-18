#!/usr/bin/env bash
#
# Verify a Mikywalk VPS deployment, layer by layer.
#
#   sudo bash deploy/verify.sh
#
# Every check prints PASS, FAIL or SKIP, and the script exits non-zero if any
# check failed. Run it as the last step of a deploy and as the first step of a
# diagnosis: the first FAIL is the layer that is broken, and everything below it
# is a consequence.
#
# It is deliberately read-only. It changes nothing, so it is safe to run on a box
# you are still unsure about.
#
# Override the defaults from the environment if the names ever change:
#   BACKEND_HOST=... MAPS_HOST=... MAPS_FILE=... bash deploy/verify.sh

set -uo pipefail

BACKEND_HOST="${BACKEND_HOST:-backend.thomasfeyens.be}"
MAPS_HOST="${MAPS_HOST:-maps.thomasfeyens.be}"
MAPS_FILE="${MAPS_FILE:-benelux.pmtiles}"
MAPS_ROOT="${MAPS_ROOT:-/srv/maps}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$REPO/backend"

fails=0
skips=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; skips=$((skips + 1)); }
note() { printf '      %s\n' "$1"; }

section() { printf '\n== %s ==\n' "$1"; }

# --- 1. Caddy binary -------------------------------------------------------
# basic_auth was introduced in Caddy 2.8; before that the directive was
# basicauth. On an older binary the whole config is rejected, the reload fails,
# and Caddy keeps serving whatever it was already serving — which on a fresh
# install is the default :80 hello page. Nothing listens on 443 and every HTTPS
# connection is refused.
section "Caddy binary"

if ! command -v caddy >/dev/null 2>&1; then
	fail "caddy is not installed"
else
	version="$(caddy version 2>/dev/null | awk '{print $1}' | sed 's/^v//')"
	if [ -z "$version" ]; then
		fail "caddy version printed nothing"
	elif [ "$(printf '%s\n2.8.0\n' "$version" | sort -V | head -1)" = "2.8.0" ]; then
		pass "caddy $version (>= 2.8, so basic_auth is understood)"
	else
		fail "caddy $version is older than 2.8; basic_auth in $CADDYFILE will be rejected"
		note "either upgrade Caddy, or change basic_auth to basicauth in the Caddyfile"
	fi
fi

# --- 2. Caddy config -------------------------------------------------------
section "Caddy config"

if [ ! -r "$CADDYFILE" ]; then
	fail "$CADDYFILE is missing or unreadable"
elif caddy validate --config "$CADDYFILE" >/tmp/mikywalk-caddy-validate.log 2>&1; then
	pass "$CADDYFILE validates"
else
	fail "$CADDYFILE does not validate"
	note "the reason is in /tmp/mikywalk-caddy-validate.log:"
	tail -5 /tmp/mikywalk-caddy-validate.log | sed 's/^/      /'
fi

# --- 3. Caddy service ------------------------------------------------------
section "Caddy service"

if systemctl is-active --quiet caddy; then
	pass "caddy is running"
else
	fail "caddy is not running"
	note "systemctl status caddy; journalctl -u caddy -n 50 --no-pager"
fi

# --- 4. Listening sockets --------------------------------------------------
# A config that validates but was never loaded looks exactly like a firewall
# problem from outside. This is the check that tells them apart.
section "Listening sockets"

for port in 80 443; do
	if ss -ltnH 2>/dev/null | grep -q ":$port "; then
		pass "something is listening on :$port"
	else
		fail "nothing is listening on :$port"
	fi
done

# --- 5. DNS ----------------------------------------------------------------
section "DNS"

for host in "$BACKEND_HOST" "$MAPS_HOST"; do
	if getent hosts "$host" >/dev/null 2>&1; then
		pass "$host resolves to $(getent hosts "$host" | awk '{print $1}' | head -1)"
	else
		fail "$host does not resolve"
	fi
done

# --- 6. Map files ----------------------------------------------------------
# Caddy runs as the caddy user. A file that root can read and caddy cannot is a
# 403 that looks like a broken map, not a permissions problem.
section "Map files"

if [ ! -d "$MAPS_ROOT" ]; then
	fail "$MAPS_ROOT does not exist"
elif [ ! -f "$MAPS_ROOT/$MAPS_FILE" ]; then
	fail "$MAPS_ROOT/$MAPS_FILE does not exist"
	note "step 9 of deploy/VPS.md builds it with pmtiles extract"
elif ! sudo -u caddy test -r "$MAPS_ROOT/$MAPS_FILE" 2>/dev/null; then
	fail "the caddy user cannot read $MAPS_ROOT/$MAPS_FILE"
	note "chmod -R a+rX $MAPS_ROOT"
else
	pass "the caddy user can read $MAPS_ROOT/$MAPS_FILE"
fi

# --- 7. Containers ---------------------------------------------------------
section "Containers"

if ! command -v docker >/dev/null 2>&1; then
	fail "docker is not installed"
else
	running="$(cd "$COMPOSE_DIR" && docker compose ps --services --status running 2>/dev/null | sort | tr '\n' ' ')"
	for service in redis backend; do
		case " $running " in
		*" $service "*) pass "$service is running" ;;
		*) fail "$service is not running (running: ${running:-none})" ;;
		esac
	done

	container="$(cd "$COMPOSE_DIR" && docker compose ps -q backend 2>/dev/null)"
	if [ -n "$container" ]; then
		health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null)"
		case "$health" in
		healthy) pass "backend container is healthy" ;;
		none) skip "backend container has no healthcheck" ;;
		*) fail "backend container is $health" ;;
		esac
	fi
fi

# --- 8. Backend over loopback ---------------------------------------------
# Before the proxy, so a 502 from Caddy can be attributed to the app rather than
# to TLS.
section "Backend over loopback"

if curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
	pass "http://127.0.0.1:8000/health answers"
else
	fail "http://127.0.0.1:8000/health does not answer"
	note "cd $COMPOSE_DIR && docker compose logs --tail 50 backend"
fi

# --- 9. Backend over HTTPS -------------------------------------------------
section "Backend over HTTPS"

body="$(curl -fsS --max-time 10 "https://$BACKEND_HOST/health" 2>/dev/null)"
if [ -n "$body" ]; then
	pass "https://$BACKEND_HOST/health answers"
else
	fail "https://$BACKEND_HOST/health does not answer"
	note "journalctl -u caddy -n 50 --no-pager"
fi

# --- 10. Maps over HTTPS ---------------------------------------------------
# 206 is the whole point: a 200 means range requests are broken and the map will
# not load, even though the file is being served.
section "Maps over HTTPS"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
	-H 'Range: bytes=0-16383' "https://$MAPS_HOST/$MAPS_FILE" 2>/dev/null)"
case "$code" in
206) pass "range request on $MAPS_FILE answers 206" ;;
200) fail "range request on $MAPS_FILE answers 200; range support is broken" ;;
000) fail "https://$MAPS_HOST/$MAPS_FILE did not connect at all" ;;
*) fail "https://$MAPS_HOST/$MAPS_FILE answered $code" ;;
esac

# --- 11. Glyph cache validators -------------------------------------------
# MapLibre Native mishandles a 304 on glyphs and renders no text at all, so the
# font and sprite responses must carry no ETag and no Last-Modified.
section "Glyph cache validators"

font_url="https://$MAPS_HOST/fonts/Noto%20Sans%20Regular/0-255.pbf"
headers="$(curl -sI --max-time 10 "$font_url" 2>/dev/null)"
font_status="$(printf '%s' "$headers" | head -1 | awk '{print $2}')"

if [ "$font_status" = "200" ]; then
	if printf '%s' "$headers" | grep -qi '^etag:'; then
		fail "the font response carries an ETag; street names will not render"
	else
		pass "the font response carries no ETag"
	fi
elif [ "$font_status" = "404" ]; then
	skip "no font at $font_url (different basemaps-assets layout?)"
else
	fail "the font request answered ${font_status:-nothing}"
fi

# --- 12. Certificate -------------------------------------------------------
# "Caddy Local Authority" means the certificate is the internal CA, which no
# browser or phone will trust. That is the local Caddyfile, not this one.
section "Certificate"

issuer="$(echo | openssl s_client -connect "$BACKEND_HOST:443" -servername "$BACKEND_HOST" 2>/dev/null \
	| openssl x509 -noout -issuer 2>/dev/null)"

if [ -z "$issuer" ]; then
	fail "could not complete a TLS handshake with $BACKEND_HOST:443"
	note "journalctl -u caddy -n 50 --no-pager"
elif printf '%s' "$issuer" | grep -qi 'Caddy Local Authority'; then
	fail "$BACKEND_HOST is serving Caddy's internal CA, not a public certificate"
	note "the local backend/Caddyfile is loaded instead of deploy/Caddyfile"
else
	pass "$BACKEND_HOST certificate issuer: ${issuer#issuer=}"
fi

# --- Summary ---------------------------------------------------------------
printf '\n'
if [ "$fails" -eq 0 ]; then
	printf 'OK: every check passed'
	[ "$skips" -gt 0 ] && printf ' (%d skipped)' "$skips"
	printf '.\n'
	exit 0
fi

printf 'FAILED: %d check(s) failed' "$fails"
[ "$skips" -gt 0 ] && printf ', %d skipped' "$skips"
printf '.\n'
printf 'Start at the first FAIL above; everything after it is a consequence.\n'
exit 1
