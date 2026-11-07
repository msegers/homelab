# Optional Valheim server

Valheim uses the EliteDesk's existing Docker Engine, in its own Compose project.
Scripts control it over SSH. An opt-in systemd timer can stop it after an hour
empty, once player detection has passed the [live acceptance test](valheim-acceptance.md).
The [private dashboard](#private-dashboard) provides status, start/stop/restart,
and world management at `/valheim` inside Home Tools. Both services and the CLI controller are Rust, with source in
[`tools/valheim/`](../tools/valheim/).

## Enable and choose storage

Add to `ansible/host_vars/homelab/main.yml`:

```yaml
valheim_enabled: true
valheim_server_name: Segers Valheim
valheim_world_name: Meadows
valheim_server_port: 2456   # UDP 2456–2458

# Optional: defaults are saves/ and backups/ in /var/lib/homelab-valheim/.
valheim_worlds_dir: /srv/games/valheim/saves
valheim_backups_dir: /srv/backups/valheim

# Enable only after the live acceptance test:
valheim_idle_shutdown_enabled: false
valheim_idle_timeout_seconds: 3600
```

Ansible creates the locked, non-login system account/group `valheim-game` for
game-data ownership, without Docker/sudo access. The OS allocates its IDs once;
reruns preserve them. Optional `valheim_uid`/`valheim_gid` can pin **unused** IDs
before creation; an existing account is never renumbered. Directories use mode
0700. For NAS storage, mount it first and support these numeric IDs/permissions
(including host-admin access for manual backups).
Set `valheim_required_mounts: [/mnt/nas]` to fail safely if
your required mount is missing. Existing NAS mount configuration stays as-is.
Use canonical absolute paths for storage and administrative directories: no
symlinks (including parent directories), duplicate slashes, or trailing slashes.
Ansible rejects system storage roots and game mounts that would expose the
administrative home, control files, or Docker sockets before changing directories.
Saves and backups must be separate, non-nested directories. Configuration must
not be inside the backup destination; manual archives exclude that destination
to avoid recursively backing up earlier archives.

Before provisioning from a Linux x86_64 controller, install Rust 1.88+ and its
static target once:

```bash
rustup target add x86_64-unknown-linux-musl
```

Ansible builds the locked Rust project locally and copies static binaries to the
host; no compiler or Rust toolchain is installed on the EliteDesk. See the
[development guide](../tools/valheim/README.md) for build and test commands.

Add `vault_valheim_server_password: 'YOUR_GAME_PASSWORD'` (5+ characters) using:

```bash
cd ansible
ansible-vault edit host_vars/homelab/vault.yml
ansible-playbook site.yml --tags valheim --ask-vault-pass
```

Normally, deploy the entire homelab with
`ansible-playbook site.yml --ask-vault-pass --ask-become-pass` from `ansible/`.
The `--tags valheim` example above is an optional way to apply only Valheim changes.

Provisioning leaves the game stopped. The normal playbook includes this role
when enabled; the default is disabled. Other settings live in
`ansible/roles/valheim/defaults/main.yml`. Game ports bind to
`valheim_game_bind_ip`, defaulting to `compose_bind_ip` independently of SSH.

## Start, stop, and switch worlds

From the repository root, with Ansible available:

```bash
./scripts/valheim start
./scripts/valheim status
./scripts/valheim stop
./scripts/valheim restart
./scripts/valheim worlds
./scripts/valheim world NewWorld
```

The script uses your **gitignored `ansible/inventory.ini`** and normal host
variables/Vault. It prompts for the privilege-escalation password (root's password
when the inventory uses `ansible_become_method=su`) and for the Vault password
unless you configure Ansible's standard password-file/identity options. Extra
Ansible flags are forwarded:

```bash
./scripts/valheim start --limit homelab
```

First start downloads the image/game; allow several minutes. Join
`192.168.68.111:2456`. Forward only the configured UDP game ports for Internet
players; management uses SSH/LAN/Tailscale.

`world NAME` follows the configured pre-change backup policy, preserves whether the server was running,
and generates a new world on its next start. World selection survives Ansible
reruns. Configuration changes take effect on an explicit start/restart; Ansible
does not start or restart the game during provisioning. The server stays stopped
after host reboots too.

### Existing-data migration

Stop the game and take a backup before changing ownership or storage paths.
Ansible refuses non-empty folders owned by a different UID/GID. Initial
provisioning creates the account, then reports any migration needed:

```bash
getent passwd valheim-game
getent group valheim-game
# On the server, substitute ONLY your dedicated Valheim data directories:
sudo chown -hR -- valheim-game:valheim-game /path/to/config /path/to/saves /path/to/backups /path/to/server
```

Keep the game stopped, review the exact paths, and preserve an off-host backup.
Never chown an entire shared NAS directory. For a storage move, copy data first,
adjust ownership, update the variables, reprovision, then start and verify saves.

## Private dashboard

### Worlds and backup policy

`/` redirects to `/valheim`. The world list discovers save directories and flat
`.db`/`.fwl` saves, plus the selected world (which may not have generated yet).
The role's bootstrap default is `Homelab`; `Meadows` above is an example. Persisted
`world.json` takes precedence and survives Ansible reruns.

Select an existing world or enter a new save name (not a seed). Saves coexist;
switching does not rename or overwrite the previous world. A running server is
cleanly stopped and resumed; a stopped server remains stopped until Start is
clicked. Switching requires confirmation because players may be disconnected.

Deletion requires typing the exact name, rejects the selected/running world, and
removes only its exact save directory and/or primary `.db`/`.fwl` files. Symlink
targets and native `_backup_` world names are refused. Other worlds, native backup
files/directories, `.old` files and the backup destination are not deleted. A
running server is briefly stopped for consistency. Deletion refuses a save path
overlapping the backup destination, so it cannot remove its own recovery archive.
If a world directory contains `_backup_` or `.old` entries, deletion is refused;
preserve those backups outside the primary world directory before deleting it.

There are three independent backup mechanisms:

- **Docker image's scheduled worker:** `valheim_backups_enabled` controls only
  the image's `BACKUPS` setting. This host now enables it with new archives under
  `/srv/storage/backups/valheim` on the verified RAID. The worker runs only while
  the game container is running; existing SSD archives are not moved.
- **Controller safety archives:** `valheim_pre_change_backups` independently
  defaults to **`true`**, and is explicitly enabled on this host. It controls
  backups before world switches, not scheduled backups. Manual CLI backups remain
  available. **Deletion always requires a successfully completed archive**, even
  if this flag is false. An archive failure aborts deletion before touching any
  world save; existing backups and selection remain unchanged.
- **Valheim's native automatic backups:** stay enabled using the game's defaults.
  No `-backups 0` or other native backup-disabling arguments are set. These rolling
  world backups are separate from the Docker image's scheduled worker.

Deletion archives contain the complete saves, configuration, Compose file and
selected-world metadata in `valheim_backups_dir`, with private permissions. The
archive must be finished, synced and published successfully before removal. It
includes the deleted world's data for recovery, not just the selected world.
The host now points these safety archives at `/srv/storage/backups/valheim`, with
required mount/UUID checks in provisioning and the Rust controller. See
[storage-service safety and deployment](storage-services.md). Changing the image's
scheduled-backup setting takes effect on the next
game start/recreation, not by Ansible restarting the game.

For port-free trusted HTTPS and DNS setup, see [Home Tools](home-tools.md).

### Installation and access

The homelab host settings enable the dashboard; the reusable role defaults it off.
To opt in on another host, set `valheim_dashboard_enabled: true`. The optional
`valheim_dashboard_port` defaults to **8787**. This host binds to **192.168.68.111**
and allows clients in **192.168.68.0/24**, configured in host variables:

```yaml
valheim_dashboard_bind_ip: "{{ compose_bind_ip }}"
valheim_dashboard_allowed_networks: [192.168.68.0/24]
```

Change the subnet if your LAN differs. The application refuses wildcard/public
bind addresses and public/broad client ranges. Reusable defaults remain
`127.0.0.1` with `127.0.0.0/8` allowed for installations preferring SSH tunnels.

Deploy from the repository root after installing the Rust prerequisites above:

For the configured homelab, first deploy the Compose role through `site.yml`
after the [Cloudflare Vault setup](home-tools.md#cloudflare-certificate-setup-before-deployment).
Dashboard provisioning inspects the configured running Caddy container and
trusts only its private `/32` addresses. The Valheim-only command below requires
that proxy to exist already; it does not deploy Caddy.

```bash
cd ansible
ansible-playbook site.yml --tags valheim --ask-vault-pass --ask-become-pass
```

This installs/starts `valheim-dashboard.service` and
`valheim-dashboard-broker.service`, updates the controller and timer, and leaves
the game running/stopped as it was. No website/Caddy changes or public ports.

**After deploying**, use **https://lab.mathijs.se/valheim** with local DNS or a
hosts-file entry. Direct **http://192.168.68.111:8787** remains available on the LAN.
No SSH tunnel is required. Merging/pulling the code alone does not install or
start the dashboard. If a host firewall blocks the port, allow TCP 8787 only from
the configured LAN subnet; do not add a global allow rule.

The application checks the actual TCP client address; only explicitly configured
Caddy `/32` peers may provide the HTTPS scheme. It never trusts forwarded client
addresses. Public clients and other subnets receive 403, even with a forged LAN
Host/Origin. Do not configure router forwarding or a public Caddy/reverse-proxy
route: a trusted LAN proxy/NAT can hide the original client's address.

The page shows container/game status, player count (or unknown), selected world,
and configured idle policy. Start, stop, and restart reuse the existing controller
and lock. Stop/restart require confirmation; controls are disabled during work.
Initial downloads can take minutes. A successful start is not a game-readiness
guarantee. World listing, selection/creation and confirmed deletion are also
available. The new-world form also accepts an optional **seed** (1–10 letters or
numbers, case-sensitive); blank uses random generation. Seeds apply only to new
worlds and never alter existing saves. A seeded world is prepared before its
first start; terrain still generates in the game. Creating/selecting while stopped
does not start the server. See [seed implementation and acceptance checks](../tools/valheim/docs/world-seeds.md).
Manual backups and updates remain CLI-only.

**Auto-refresh (15s)** can be unchecked to leave the tab open without background
dashboard requests. **Refresh now** still fetches current status/worlds, and
actions refresh their results even while polling is paused. An in-flight refresh
finishes normally; displayed values can become stale while paused. The toggle
applies to this tab and resets to enabled on page reload. Refreshes in one tab
cannot overlap, but other tabs may still contend for the broker lock.

Each dashboard refresh checks Docker and, for a running game, makes Steam A2S
player-count queries; there is no shared status cache. Pausing the dashboard
does **not** disable the independent idle-shutdown monitor or its player checks.

The web account cannot read game secrets, invoke sudo, or access Docker. Its
Unix-socket broker allows only typed lifecycle/world operations. The allowed LAN is
the access boundary: **anyone on that subnet can control the game**. Browser-to-Caddy
traffic uses trusted HTTPS; direct backend HTTP is unencrypted and must remain
on the trusted LAN. There is no public
login system. Optional SSH access still works with a tunnel whose remote target
is the configured bind address (e.g. `-L 127.0.0.1:8787:192.168.68.111:8787`).

Check on the server:

```bash
sudo systemctl status valheim-dashboard.service valheim-dashboard-broker.service
sudo journalctl -u valheim-dashboard.service -u valheim-dashboard-broker.service -n 40
sudo ss -lntp 'sport = :8787'
```

Expect the listener at `192.168.68.111:8787`, never `0.0.0.0` or a public IP. Set
`valheim_dashboard_enabled: false` and redeploy to stop only the dashboard/helper,
leaving the game and its idle timer alone. Retirement stops both dashboard units
before stopping Valheim and preserves all saves/backups.

## Automatic shutdown

When enabled, the system-wide **`valheim-idle.timer`** checks every 60 seconds. After
3600 seconds of confirmed zero-player readings, it gracefully stops the game
container (120-second save/shutdown allowance).

- Player activity, failed/disagreeing queries, restarts, and monitoring gaps
  reset the countdown. Connection logs also catch short visits between polls.
- The game process must report RUNNING at each poll and the final stop check;
  starting, failed, stopped, or unknown process states reset the countdown.
- Counts are queried by Rust over Steam A2S at the host's published query port.
  No Python query script runs in the container. This requires
  `valheim_server_public: true` and `valheim_crossplay: false` (defaults).
  Public listing does not configure router port forwarding.
- A2S_PLAYER entries can have blank names and some modes/versions report false
  zeros. Matching A2S_INFO/A2S_PLAYER zeros are not proof that a server is empty.
  Shutdown defaults **off** until you validate occupied and empty readings using
  the [live acceptance procedure](valheim-acceptance.md#player-count-and-shutdown-test).
- Unqueryable/private-listing/crossplay modes stay running rather than risk
  disconnecting players. A final query is made immediately before stopping;
  a connection arriving at the exact stop instant can still race the check.
- Set `valheim_idle_shutdown_enabled: false` and rerun provisioning to disable
  automatic shutdown. It never starts the game automatically.

On the server, inspect the timer's logs or control it directly:

```bash
sudo systemctl status valheim-idle.timer
sudo journalctl -u valheim-idle.service -n 30
sudo valheimctl status
```

## Updates and backups

```bash
./scripts/valheim backup
./scripts/valheim update
```

`backup` stops the game for a consistent archive and resumes it only if it was
running. Archives include complete Valheim 1.0 world directories, configuration,
and world selection, stored in `valheim_backups_dir` with mode 0600. Manual
archives are retained until you rotate them. Copy/encrypt them off-host: the
included Compose configuration contains the game password.

Archive failures remove only this operation's temporary output and preserve
earlier backups, including on filename collisions. If saving succeeds but resume
fails, the error includes the saved archive path. A failed restart/resume returns
an error, never a success message. Check `status`/logs before assuming it is up;
container startup is separate from game initialization/download readiness.

When `valheim_backups_enabled` is true, the game image also keeps hourly world
backups while recently active, capped at 48 copies/14 days. Those backups run
while the game container is running. This host currently disables them until
NAS storage is ready.

`update` pulls the configured, digest-pinned image and recreates the game only
if already running. Steam game binaries update on startup. To change the image
version, override `valheim_server_image`, reprovision, then run `update`.

For restores, stop the game first. Inspect/extract a trusted archive; restore
`saves/` and `config/` to their configured paths and
`world.json` to `/var/lib/homelab-valheim/control/`. Preserve ownership and test
the restored world before discarding old backups. Reprovision to regenerate
Compose configuration for the current paths/secrets.

## Permissions and lifecycle

The pinned image's bootstrap needs root for user mapping, ownership, cron and
Supervisor setup; Supervisor then runs the game/updater/backup workers as the
dedicated PUID/PGID. Keep this supported initialization path instead of forcing
Compose `user:`. Privileged mode is disabled, NET_RAW is dropped, no extra
capabilities are added, and **no Docker socket is mounted**. Data modes remain
0700/0600; Compose/control files stay root-owned. See the
[pinned-source audit and runtime checks](valheim-acceptance.md#image-and-account-checks).

Valheim has its own project, persistent bind mounts, and `restart: "no"` policy.
Commands explicitly target its service/container and verify Compose ownership.
They leave Caddy, Pi-hole, and mathijs.se running. The idle timer runs a short
check once per minute; there is no additional Docker daemon to keep idle.

To retire safely:

```bash
./scripts/valheim retire
```

This disables the timer, cancels an in-flight check, and gracefully stops only
Valheim. Saves, backups, containers, and the game account remain for recovery.
Set `valheim_enabled: false` in host variables and apply the playbook to keep it
retired; this also performs the same safe stop on an installed instance. A fresh
disabled installation leaves other services alone. Re-enable and provision when
needed, then start manually. `valheim_idle_shutdown_enabled: false` disables
only automatic shutdown, leaving the game running.

If you deployed the earlier rootless preview, stop its game/timer before
switching engines, back up the saves, and preserve/adjust their numeric ownership.
Retire that old user daemon manually after verifying the backup; this playbook
does not delete previous users, daemons, or world data.

Reference: [maintained Valheim image](https://github.com/community-valheim-tools/valheim-server-docker).
