# Optional Valheim server

Valheim uses the EliteDesk's existing Docker Engine, in its own Compose project.
Scripts control it over SSH; a systemd timer stops it after an hour empty.

## Enable and choose storage

Add to `ansible/host_vars/elitedesk/main.yml`:

```yaml
valheim_enabled: true
valheim_server_name: Segers Valheim
valheim_world_name: Meadows
valheim_server_port: 2456   # UDP 2456–2458

# Optional: defaults are saves/ and backups/ in /var/lib/homelab-valheim/.
valheim_worlds_dir: /srv/games/valheim/saves
valheim_backups_dir: /srv/backups/valheim

valheim_idle_shutdown_enabled: true
valheim_idle_timeout_seconds: 3600
```

Use dedicated directories: Ansible assigns game storage to `valheim_uid` and
`valheim_gid` (both default to 1000), with mode 0700. For NAS storage, mount it
first and ensure it supports that UID's ownership/permissions.
Set `valheim_required_mounts: [/mnt/nas]` to fail safely if
your required mount is missing. Existing NAS mount configuration stays as-is.

Add `vault_valheim_server_password: 'YOUR_GAME_PASSWORD'` (5+ characters) using:

```bash
cd ansible
ansible-vault edit host_vars/elitedesk/vault.yml
ansible-playbook site.yml --tags valheim --ask-vault-pass
```

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
variables/Vault. It prompts for the Vault password unless you configure Ansible's
standard password-file/identity options. Extra Ansible flags are forwarded:

```bash
./scripts/valheim start --limit elitedesk --ask-become-pass
```

First start downloads the image/game; allow several minutes. Join
`192.168.68.111:2456`. Forward only the configured UDP game ports for Internet
players; management uses SSH/LAN/Tailscale.

`world NAME` backs up before switching, preserves whether the server was running,
and generates a new world on its next start. World selection survives Ansible
reruns. Configuration changes take effect on an explicit start/restart; Ansible
does not start or restart the game during provisioning. The server stays stopped
after host reboots too.

For storage moves: stop the game, copy saves/backups preserving ownership, update
the variables, rerun provisioning, then start it. Set `valheim_uid`/`valheim_gid`
to match existing saves' ownership, or adjust ownership before starting.

## Automatic shutdown

The system-wide **`valheim-idle.timer`** checks every 60 seconds. After
3600 seconds of confirmed zero-player readings, it gracefully stops the game
container (120-second save/shutdown allowance).

- Player activity, failed/disagreeing queries, restarts, and monitoring gaps
  reset the countdown. Connection logs also catch short visits between polls.
- Counts are queried inside the container using Steam A2S. This requires
  `valheim_server_public: true` and `valheim_crossplay: false` (defaults).
  Public listing does not configure router port forwarding.
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

The game image also keeps hourly world backups while recently active, capped at
48 copies/14 days. Those backups run while the game container is running.

`update` pulls the configured, digest-pinned image and recreates the game only
if already running. Steam game binaries update on startup. To change the image
version, override `valheim_server_image`, reprovision, then run `update`.

For restores, stop the game first. Inspect/extract a trusted archive; restore
`saves/` and `config/` to their configured paths and
`world.json` to `/var/lib/homelab-valheim/control/`. Preserve ownership and test
the restored world before discarding old backups. Reprovision to regenerate
Compose configuration for the current paths/secrets.

## Permissions and lifecycle

The image initializes permissions as container root, then runs the game using
the configured non-root UID/GID. Privileged mode is disabled, no extra
capabilities are added, and **no Docker socket is mounted**. Compose and control
files stay root-owned; administration uses sudo on the host.

Valheim has its own project, persistent bind mounts, and `restart: "no"` policy.
Commands explicitly target its service/container and verify Compose ownership.
They leave Caddy, Pi-hole, and mathijs.se running. The idle timer runs a short
check once per minute; there is no additional Docker daemon to keep idle.

To retire Valheim, stop the game, set `valheim_enabled: false`, and run
`sudo systemctl disable --now valheim-idle.timer`. Preserve the storage folders.

If you deployed the earlier rootless preview, stop its game/timer before
switching engines, back up the saves, and preserve/adjust their numeric ownership.
Retire that old user daemon manually after verifying the backup; this playbook
does not delete previous users, daemons, or world data.

Reference: [maintained Valheim image](https://github.com/community-valheim-tools/valheim-server-docker).
