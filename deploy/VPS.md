# Deploying DogWalk to the VPS

Target: `207.180.240.29`, Ubuntu, root only.
Result: a non-root sudo user, root SSH disabled, two HTTPS hostnames.

Every command below is run as `root` until step 1 says otherwise.

## Read this before you type anything

Three ways this procedure can lock you out or expose you, in the order they are
likely to bite:

1. **Disabling root SSH before you have proven key login as the new user.** Step 2
   is irreversible from the outside. Do not run it until step 1's check passes in
   a *new* terminal.
2. **Docker bypasses ufw.** Docker writes its own iptables rules, so a container
   published as `0.0.0.0:6379:6379` is reachable from the internet even with ufw
   denying everything. Every published port in this project is bound to
   `127.0.0.1` for exactly that reason. If you ever add one that is not, ufw will
   not save you.
3. **The `docker` group is root.** Membership means you can mount the host
   filesystem into a container. It is a convenience, not a privilege boundary.

Keep your provider's web console open in a second tab for the whole of steps 1-3.
It is the only way back in if SSH stops working.

## 0. Before you start

- Your SSH public key is already in `/root/.ssh/authorized_keys` (you are logging
  in as root, so it must be).
- Both DNS records point at `207.180.240.29`:
  `backend.thomasfeyens.be` and `maps.thomasfeyens.be`. Caddy cannot issue a
  certificate until they resolve.

## 1. Create the non-root user

```bash
adduser thomas
usermod -aG sudo thomas

install -d -m 700 -o thomas -g thomas /home/thomas/.ssh
cp /root/.ssh/authorized_keys /home/thomas/.ssh/authorized_keys
chown thomas:thomas /home/thomas/.ssh/authorized_keys
chmod 600 /home/thomas/.ssh/authorized_keys
```

**Check it before going on.** From your laptop, in a new terminal:

```bash
ssh thomas@207.180.240.29
sudo -v          # must accept your password
```

If that does not work, fix it now. Everything after this assumes it does.

## 2. Harden SSH

Only once step 1's check has passed:

```bash
cat > /etc/ssh/sshd_config.d/99-dogwalk.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF

sshd -t && systemctl reload ssh
```

`sshd -t` validates before you reload. If it prints nothing, the config is good.

Verify, again in a new terminal:

```bash
ssh thomas@207.180.240.29     # must still work
ssh root@207.180.240.29       # must be refused
```

Root still exists locally and `su -` still works with the root password. What is
gone is remote root login, which is what gets brute-forced.

## 3. Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
ufw status verbose
```

80 and 443 are for Caddy: 80 for the ACME challenge and the HTTP→HTTPS redirect,
443 for everything else. Nothing else is open.

## 4. Automatic security updates

```bash
apt update && apt upgrade -y
apt install -y unattended-upgrades

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
```

## 5. Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker thomas
```

Log out and back in so the group takes effect, then confirm:

```bash
docker run --rm hello-world
```

## 6. Caddy

Official repository, from `caddyserver.com/docs/install`:

```bash
apt install --yes debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Certificates are issued and renewed by Caddy itself. There is nothing to schedule
and no certbot.

## 7. Get the code onto the box

**Your Gitea is at `192.168.3.70:2222`, which is a private address.** The VPS
cannot reach it, so `git clone` from the server will hang and then fail. Ship a
bundle instead.

On your laptop:

```bash
cd /c/Users/thoma/Downloads/dog
git bundle create /tmp/dog.bundle --all
scp /tmp/dog.bundle thomas@207.180.240.29:~/
```

On the VPS:

```bash
git clone ~/dog.bundle ~/dog
cd ~/dog && git log --oneline -1
```

The bundle is ~19 MB and carries full history, so `git log` and `git diff` work
normally on the server. To update later, repeat the bundle and `git pull ~/dog.bundle`.

The durable fix is a remote both machines can reach — a public GitHub mirror, or
exposing Gitea. Until then the bundle is the transport.

## 8. Backend

```bash
cd ~/dog/backend

cat > .env <<EOF
DOGWALK_ENV=production
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
PUBLIC_BASE_URL=https://backend.thomasfeyens.be
ALLOW_LOGGING_EMAIL=1
EOF
chmod 600 .env

docker compose up -d redis backend
docker compose ps
```

Note `redis backend` — deliberately not `up -d`. The compose `caddy` service is
the *local* one with `tls internal`; on this box the host Caddy does TLS.

`DOGWALK_ENV=production` turns on the strict startup checks. The backend will
refuse to boot if `JWT_SECRET` is still the public development value, or if
`ALLOW_LOGGING_EMAIL` is missing — both are deliberate. `ALLOW_LOGGING_EMAIL=1`
is you stating that this box's logs are readable only by you; delete it the day
you wire up a real email provider.

## 9. Maps

```bash
mkdir -p /srv/maps && cd /srv/maps

curl -L https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz \
  | tar -xz -C /usr/local/bin pmtiles

# Check maps.protomaps.com/builds for the newest date first.
pmtiles extract https://build.protomaps.com/20261112.pmtiles benelux.pmtiles \
  --bbox=2.40,49.35,7.35,53.65 --maxzoom=15

git clone --depth 1 https://github.com/protomaps/basemaps-assets /tmp/assets
cp -r /tmp/assets/fonts /tmp/assets/sprites /srv/maps/

chmod -R a+rX /srv/maps
pmtiles show /srv/maps/benelux.pmtiles | head -20
```

Benelux rather than Belgium because the emulator's reference point is the
Amsterdam Dam; a Belgium-only extract leaves your own test device on a blank map.

`pmtiles extract` reads only the byte ranges it needs from the remote planet
build, so it never downloads the 120 GB file.

## 10. Caddy config

```bash
cp ~/dog/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

`caddy validate` prints one warning about `header_up X-Forwarded-For` being
"unnecessary". It is not: `app/ratelimit.py` bills `split(",")[0]`, and Caddy's
default appends the real peer to whatever the client sent, which would let a
caller choose its own rate-limit bucket. Ignore the warning.

## 11. Verify

```bash
curl -s https://backend.thomasfeyens.be/health
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-16383" \
  https://maps.thomasfeyens.be/benelux.pmtiles
```

Must print `206`. A `200` means range requests are broken and the map will not
load.

```bash
curl -sI "https://maps.thomasfeyens.be/fonts/Noto%20Sans%20Regular/0-255.pbf" | grep -i etag
```

Must print **nothing**. An `ETag` here means the glyph fix is not in effect and
the map will render without street names.

Then check the certificate is real and not Caddy's internal CA:

```bash
echo | openssl s_client -connect backend.thomasfeyens.be:443 -servername backend.thomasfeyens.be 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

The issuer should be Let's Encrypt (or ZeroSSL), not "Caddy Local Authority".

## 12. Ongoing

```bash
# logs
cd ~/dog/backend && docker compose logs -f backend

# update the code
#   on the laptop: git bundle create /tmp/dog.bundle --all && scp ...
#   on the VPS:
cd ~/dog && git pull ~/dog.bundle main && cd backend && docker compose up -d --build backend

# back up the accounts. The geo index is disposable; the user store is not.
docker run --rm -v backend_redis-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/redis-$(date +%F).tgz -C /data .
```

Caddy renews certificates on its own. `apt` upgrades itself via
`unattended-upgrades`. Nothing else is scheduled — **there is no backup cron**,
so the command above is manual until you add one.

## What this does not do

- **No real email sender.** Reset codes go to the backend log, which is why
  `ALLOW_LOGGING_EMAIL=1` is in the `.env`. Anyone who can read
  `docker compose logs backend` can reset any account.
- **No second gate on the admin panel.** `https://backend.thomasfeyens.be/admin/ui`
  is reachable from the internet and protected by the operator password alone.
  Caddy `basic_auth` on `/admin*` is the cheap fix.
- **No backups.** See step 12.
- **No monitoring.** If the container dies, nothing tells you.
