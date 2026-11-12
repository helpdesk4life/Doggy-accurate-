#!/usr/bin/env bash
# Re-export Caddy's local root CA into the Android debug source set.
#
# The debug build trusts this CA so it can reach wss://10.0.2.2:8443. Caddy
# generates the CA on first start and keeps it in the caddy-data volume, so the
# file only needs regenerating if that volume is wiped (docker compose down -v)
# or you move to a different machine.
set -euo pipefail

cd "$(dirname "$0")/.."

dest="../android/app/src/debug/res/raw/caddy_root.crt"
mkdir -p "$(dirname "$dest")"

# MSYS_NO_PATHCONV stops Git Bash from rewriting the container path.
MSYS_NO_PATHCONV=1 docker compose exec -T caddy \
    cat /data/caddy/pki/authorities/local/root.crt > "$dest"

echo "wrote $dest ($(wc -c < "$dest") bytes)"
echo "rebuild the app: cd ../android && ./gradlew :app:assembleDebug"
