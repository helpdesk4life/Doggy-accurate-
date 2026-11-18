# Deploying Mikywalk to the VPS

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
4. **The disk is not encrypted unless you make it so.** The persistent Redis runs
   with AOF, so its volume on disk holds password hashes, the SMTP password and
   the audit log. Live GPS positions do not land there: the geo index runs on
   `redis-geo`, which has persistence off. Everything else does - enable LUKS on
   the data disk at provisioning time, or accept that a stolen disk is a stolen
   user database.

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

Only once step 1's check has passed.

**The filename matters.** `sshd` uses the *first* value it sees for each keyword,
and it reads `sshd_config.d/*.conf` in lexical order. Cloud images ship
`50-cloud-init.conf`, which on many providers contains `PasswordAuthentication yes`.
A file named `99-mikywalk.conf` sorts after it and is therefore **silently ignored**:
you reload, see no error, and are still accepting passwords. `00-` sorts first.

```bash
ls /etc/ssh/sshd_config.d/          # expect 50-cloud-init.conf on a cloud image

cat > /etc/ssh/sshd_config.d/00-mikywalk.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF

sshd -t && systemctl reload ssh
```

`sshd -t` validates the syntax before you reload; silence means it parsed.

**Do not trust the file — check the effective config:**

```bash
sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication)'
```

All three must read `no`. `sshd -T` prints what sshd will actually use after every
drop-in has been merged, so it is the only check that catches the ordering trap.
If a line says `yes`, a file earlier in the directory is winning — delete or edit
that file and run it again.

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

Order matters: `allow 22/tcp` must come **before** `ufw enable`, or you cut the
connection you are typing over. `ufw enable` asks *"Command may disrupt existing
ssh connections. Proceed with operation (y|n)?"* — answer `y`, the rule is already
in place.

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

**Check the version before loading any config.** `basic_auth` in
`deploy/Caddyfile` only exists from Caddy 2.8 onwards. On an older binary the
whole config is rejected, `systemctl reload caddy` fails, and Caddy keeps serving
whatever it was already serving — on a fresh install that is the default `:80`
hello page, so nothing ever listens on 443 and every HTTPS connection is
refused. That is exactly how the first deployment failed.

```bash
caddy version                 # must print v2.8.0 or newer
apt policy caddy | head -5    # the candidate must come from dl.cloudsmith.io,
                              # not from Ubuntu's own repository (2.6.x on 24.04)
```

Certificates are issued and renewed by Caddy itself. There is nothing to schedule
and no certbot.

## 7. Get the code onto the box

The repository is private (`github.com/helpdesk4life/doggy`), so the VPS needs
its own key. Use a **deploy key**: it is scoped to that one repository and
read-only, so a compromised server cannot reach anything else in the account.

On the VPS, as `thomas`:

```bash
ssh-keygen -t ed25519 -C "mikywalk-vps" -f ~/.ssh/github_mikywalk -N ""
cat ~/.ssh/github_mikywalk.pub
```

Copy that line. In GitHub: **repo → Settings → Deploy keys → Add deploy key**,
title `mikywalk-vps`, paste the key, leave **Allow write access** unchecked.

Then, still on the VPS:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_mikywalk
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

ssh -T git@github.com
git clone git@github.com:helpdesk4life/doggy.git ~/dog
cd ~/dog && git log --oneline -1
```

On a fresh box the first `ssh -T` fails with "Host key verification failed",
because `~/.ssh/known_hosts` does not know github.com yet and nothing is there to
accept the key interactively. Scan it in first, then check the fingerprint against
the ones GitHub publishes
([docs.github.com … githubs-ssh-key-fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)):

```bash
ssh-keyscan -t ed25519,ecdsa,rsa github.com >> ~/.ssh/known_hosts 2>/dev/null
ssh-keygen -lf ~/.ssh/known_hosts
# ED25519 must read SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
```

`ssh -T` answers `Hi helpdesk4life/doggy! You've successfully authenticated, but
GitHub does not provide shell access.` — that is success.

`IdentitiesOnly yes` is not optional. Without it SSH offers every key it holds,
GitHub rejects the first few, and the clone dies with `Too many authentication
failures`.

Updates are then a plain `git pull`. No bundle, no `scp`.

## 8. Backend

```bash
cd ~/dog/backend

cat > .env <<EOF
MIKYWALK_ENV=production
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
PUBLIC_BASE_URL=https://backend.thomasfeyens.be
ALLOW_LOGGING_EMAIL=1
EOF
chmod 600 .env

docker compose up -d redis redis-geo backend
docker compose ps
```

Note `redis redis-geo backend` — deliberately not `up -d`. The compose `caddy` service is
the *local* one with `tls internal`; on this box the host Caddy does TLS. `redis-geo`
is the volatile geo index (`GEO_REDIS_URL` points the backend at it): no AOF, no
volume, so live positions never touch disk. `redis` keeps AOF, because accounts
must survive a restart.

`MIKYWALK_ENV=production` turns on the strict startup checks. The backend will
refuse to boot if `JWT_SECRET` is still the public development value, or if
`ALLOW_LOGGING_EMAIL` is missing — both are deliberate. `ALLOW_LOGGING_EMAIL=1`
is you stating that this box's logs are readable only by you; delete it the day
you wire up a real email provider.

### Switching reset emails to SMTP

`app/email.py` has a real `SmtpEmailSender`, so this is credentials rather than
code. Any provider that speaks SMTP works.

**The easy way: the panel.** Sign in at `https://backend.thomasfeyens.be/admin/ui`,
open **Email**, fill in the server, port, security, username, password and sender
address, put your own address in "Send a test to", and press **Send test**. The
message goes out through the values on screen without saving them, so a wrong
port costs you nothing. When it arrives, press **Save** — the running sender is
rebuilt in place and the next reset email uses it. No restart, no `.env` edit.

Leave `ALLOW_LOGGING_EMAIL=1` in the `.env`. `Settings.validate()` reads only the
environment, so it cannot see a configuration saved in the panel and would still
refuse to boot without the acknowledgement — even though the panel's sender is
the one actually in use.

**The other way: environment variables.** Replace the `ALLOW_LOGGING_EMAIL` line
in `~/dog/backend/.env` with:

```bash
EMAIL_SENDER=smtp
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USERNAME=resend
SMTP_PASSWORD=re_your_api_key
SMTP_FROM=Mikywalk <no-reply@yourdomain.example>
SMTP_SECURITY=starttls
```

then `docker compose up -d backend`. **Do not set `EMAIL_SENDER=smtp` without
`SMTP_HOST` and `SMTP_FROM`** — `Settings.validate()` refuses to boot, so the
container restart-loops instead of quietly falling back to the log.

Either way, check with a real reset request: the code should arrive by mail and
*not* appear in `docker compose logs backend`.

### First operator account and the first invite code

`INVITE_REQUIRED=1` is on, so until a code is minted nobody can register — and
until an admin exists nobody can open the panel. Both are operator scripts, and
on this box they run **inside the backend container**, because the host has no
virtualenv and the scripts import `app`:

```bash
cd ~/dog/backend

docker compose exec backend python scripts/create_admin.py --email you@example.com
# prompts for a password; there is no recovery flow, so keep it somewhere safe

docker compose exec backend python scripts/mint_invites.py --count 3
```

The container already carries `REDIS_URL=redis://redis:6379/0` in its
environment, so the scripts find Redis with no extra arguments, and
`docker compose exec` allocates the TTY that the password prompt needs.

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

# A browser visiting https://maps.thomasfeyens.be/ would otherwise get a 404,
# because /srv/maps has no index file. This page says what is served where.
cp ~/dog/deploy/maps-index.html /srv/maps/index.html

chmod -R a+rX /srv/maps
pmtiles show /srv/maps/benelux.pmtiles | head -20
```

Benelux rather than Belgium because the emulator's reference point is the
Amsterdam Dam; a Belgium-only extract leaves your own test device on a blank map.

`pmtiles extract` reads only the byte ranges it needs from the remote planet
build, so it never downloads the 120 GB file.

Nothing else runs here. The app reads the archive directly: `assets/style.json`
declares `pmtiles://https://maps.thomasfeyens.be/benelux.pmtiles`, and MapLibre
Native (11.7.0 and later) turns that into range requests against Caddy. There is
no `pmtiles serve`, no tile server and no systemd unit to keep alive — which is
why the range-request check in **11. Verify** matters: a 200 instead of a 206
means the map will not load at all.

## 10. Caddy config

```bash
# Keep the config that is currently working, so a bad one is reversible.
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak 2>/dev/null || true
cp ~/dog/deploy/Caddyfile /etc/caddy/Caddyfile

caddy validate --config /etc/caddy/Caddyfile || {
  echo "config rejected — the running config is untouched; fix and retry"
  exit 1
}
systemctl reload caddy

# A failed reload leaves the old config in place, and reloading a service that
# is not running is a silent no-op — so check the service, not just the exit code.
systemctl is-active caddy
```

Rollback, if a reload ever breaks a working box:

```bash
cp /etc/caddy/Caddyfile.bak /etc/caddy/Caddyfile && systemctl reload caddy
```

`caddy validate` prints one warning about `header_up X-Forwarded-For` being
"unnecessary". It is not: `app/ratelimit.py` bills `split(",")[0]`, and Caddy's
default appends the real peer to whatever the client sent, which would let a
caller choose its own rate-limit bucket. Ignore the warning.

## 11. Verify

```bash
sudo bash ~/dog/deploy/verify.sh
```

Twelve checks, ordered from the Caddy version down to the certificate issuer.
The first FAIL is the layer that is broken; everything below it is a consequence
of that one. The script exits non-zero if anything failed, so it is safe to use
as the last step of a deploy and the first step of a diagnosis, and it changes
nothing.

The individual checks, if you want to run one by hand:

```bash
curl -s https://backend.thomasfeyens.be/health
```

```bash
# Must print 206. A 200 means range requests are broken and the map will not load.
curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-16383" \
  https://maps.thomasfeyens.be/benelux.pmtiles
```

```bash
# Must print nothing. An ETag here means the glyph fix is not in effect and the
# map will render without street names.
curl -sI "https://maps.thomasfeyens.be/fonts/Noto%20Sans%20Regular/0-255.pbf" | grep -i etag
```

Then check the certificate is real and not Caddy's internal CA:

```bash
echo | openssl s_client -connect backend.thomasfeyens.be:443 -servername backend.thomasfeyens.be 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

The issuer should be Let's Encrypt (or ZeroSSL), not "Caddy Local Authority".
That name means the *local* `backend/Caddyfile` is loaded instead of
`deploy/Caddyfile` — the two are easy to confuse because both live in the repo —
and no browser or phone will trust a certificate from the internal CA.

## 12. Ongoing

```bash
# logs
cd ~/dog/backend && docker compose logs -f backend

# update the code (the deploy key is pull-only, so this is the whole procedure)
cd ~/dog && git pull && cd backend && docker compose up -d --build backend

# back up the accounts. The geo index is disposable; the user store is not.
bash ~/dog/deploy/backup-redis.sh
```

Run that as `thomas`, not as `root`. The deploy key lives in
`/home/thomas/.ssh/github_mikywalk` and the repo is owned by `thomas`; root has
neither the key nor the `Host github.com` entry in `~/.ssh/config`, so a `git
pull` from a root shell fails with `Permission denied (publickey)`. That reads
like a missing or revoked deploy key and sends you looking at GitHub, but it is
only the wrong user. From a root shell:

```bash
sudo -u thomas bash -lc 'cd ~/dog && git pull && cd backend && docker compose up -d --build backend'
```

`thomas` is in the `docker` group, so the whole sequence works under that user.

A reboot brings the stack back on its own — the containers carry
`restart: unless-stopped` in `docker-compose.yml` and Caddy is enabled by its
package. After any reboot or update, run the verify script again:

```bash
sudo bash ~/dog/deploy/verify.sh
```

Caddy renews certificates on its own. `apt` upgrades itself via
`unattended-upgrades`. The account store is backed up daily by
`deploy/backup-redis.sh`, installed as `/etc/cron.d/mikywalk-backup`:

```bash
# 03:17 daily, as thomas (who is in the docker group). One line per run.
17 3 * * * thomas /home/thomas/dog/deploy/backup-redis.sh >> /home/thomas/backups/backup.log 2>&1
```

Snapshots land in `/home/thomas/backups/redis-YYYY-MM-DD.rdb.gz` and are pruned
after 14 days (`KEEP_DAYS`). The script exits non-zero on any failure, so a
broken run shows up in `backup.log` instead of passing silently. It snapshots
with `BGSAVE` rather than tarring the volume, because a tar of a live volume can
capture the AOF mid-append and restore as a torn command.

**Restoring** is the one thing this does not automate:

```bash
docker compose -f ~/dog/backend/docker-compose.yml stop backend redis
gunzip -c /home/thomas/backups/redis-YYYY-MM-DD.rdb.gz > /tmp/dump.rdb
docker run --rm -v backend_redis-data:/data -v /tmp:/restore alpine \
  sh -c 'rm -rf /data/appendonlydir && cp /restore/dump.rdb /data/dump.rdb'
docker compose -f ~/dog/backend/docker-compose.yml up -d redis backend
```

Dropping `appendonlydir` matters: with AOF enabled Redis prefers it over the
RDB, so leaving a stale AOF in place restores the old data and looks like the
backup did nothing.

## 13. CrowdSec (installed 2026-11-15)

CrowdSec watches the two public surfaces — Caddy's access log (via journald) and
`/var/log/auth.log` — and bans through the firewall bouncer in an nftables table.
The backend container stays loopback-only, so Caddy is the right layer.

```bash
curl -s https://install.crowdsec.net | sh
apt install -y crowdsec crowdsec-firewall-bouncer
# acquisitions for caddy (journalctl) and sshd (auth.log) ship as
# /etc/crowdsec/acquis.d/setup.{caddy,sshd,linux}.yaml — nothing to write.
# collections enabled by default: caddy, base-http-scenarios, http-cve, sshd,
# linux, whitelist-good-actors.
```

Three packaging gotchas, all hit on this box:

1. `crowdsec-firewall-bouncer.service` has `ConditionPathExists=!/var/lib/crowdsec/pending-registration`;
   the file ships with the package and must be removed once the bouncer is
   registered: `rm -f /var/lib/crowdsec/pending-registration`.
2. The bouncer merges `/etc/crowdsec/bouncers/<name>.yaml.local` over the main
   yaml — the package drops a stale auto-generated `api_key` there, which silently
   wins. Delete the `.local` file after putting the real key in the main one:
   `cscli bouncers add crowdsec-firewall-bouncer` (capture the printed key!), then
   `sed -i "s|^api_key: .*|api_key: <key>|" /etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml`.
3. The unit is `Type=notify` but the bouncer never sends READY and `daemonize: true`
   makes it exit 0 — either way systemd kills it. Fix both with a drop-in and the
   config: `printf "[Service]\nType=simple\n" > /etc/systemd/system/crowdsec-firewall-bouncer.service.d/override.conf`,
   `daemon-reload`, and `sed -i "s|^daemonize: .*|daemonize: false|; s|^mode: .*|mode: nftables|"` on the
   bouncer yaml (`mode: iptables` needs the `ipset` binary; `nftables` does not).

Allowlist the admin workstation so a false positive cannot lock you out —
`/etc/crowdsec/parsers/s02-enrich/99-local-allowlist.yaml`:

```yaml
name: local/admin-allowlist
whitelist:
  reason: "admin workstation"
  ip:
    - "178.51.71.124"
```

(Loopback and RFC1918 are already allowlisted by `crowdsecurity/whitelists`.)
Enroll with the console for the community blocklist: `cscli console enroll <key>`.

```bash
systemctl enable --now crowdsec crowdsec-firewall-bouncer
cscli decisions list          # live bans
nft list set ip crowdsec crowdsec-blacklists   # what the bouncer enforces
```

Verified live: 40 requests to nonexistent paths from the box itself produced an
`http-probing` ban on its own IP, `nft` showed it in the set, and the same request
then failed until the decision was deleted; SSH brute-force from two real IPs was
banned from auth.log; `deploy/verify.sh` still passes every check.

## What this does not do

- ~~**No SMTP credentials yet.**~~ Closed on 2026-11-16: Gmail over port 465 SSL
  (the only outbound SMTP port this box opens), configured both in the admin
  panel's Redis slot and in the `.env` — `EMAIL_SENDER=smtp`, and
  `ALLOW_LOGGING_EMAIL` removed so the boot cannot fall back to the log. Verified
  end to end: a test message arrived and a real reset request's code appears
  nowhere in `docker compose logs backend`. See the 2026-11-16 session log in
  `HANDOFF.md` for what was stored where.
- ~~**No second gate on the admin panel.**~~ Closed: `/admin*` sits behind
  Caddy `basic_auth` (user `thomas`) before the app's own login. The password
  was set at deploy time and is not in this repository — regenerate with
  `caddy hash-password`, edit `deploy/Caddyfile`, then step 10 again.
  (Rotated once already, 2026-11-16 — the deployed hash, the repo's `deploy/Caddyfile`
  line, `.secrets/bcrypt.txt` and the secrets-file password all move together.
  Notes in the 2026-11-16 HANDOFF session log.)
- ~~**No backups.**~~ Closed on 2026-11-18: `deploy/backup-redis.sh` runs daily
  from `/etc/cron.d/mikywalk-backup`, keeps 14 days, and logs to
  `/home/thomas/backups/backup.log`. Restore is still manual — see step 12.
- **No monitoring.** If the container dies, nothing tells you.
