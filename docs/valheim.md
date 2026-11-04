# Optional Valheim server

A dedicated rootless Docker daemon runs Valheim. Ansible configures it;
scripts control it over SSH. A systemd timer stops the game after an hour empty.

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

Use dedicated directories: Ansible assigns them to the `homelab-valheim` account
with mode 0700. For NAS storage, mount it first and ensure it supports that UID's
ownership/permissions. Set `valheim_required_mounts: [/mnt/nas]` to fail safely if
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
the variables, rerun provisioning, then start it. The dedicated UID is available
with `id -u homelab-valheim` on the server.

## Automatic shutdown

The dedicated account's **`valheim-idle.timer`** checks every 60 seconds. After
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
sudo journalctl _SYSTEMD_USER_UNIT=valheim-idle.service -n 30
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

For restores, stop the game first. Inspect/extract a trusted archive as the
dedicated account; restore `saves/` and `config/` to their configured paths and
`world.json` to `/var/lib/homelab-valheim/control/`. Preserve ownership and test
the restored world before discarding old backups. Reprovision to regenerate
Compose configuration for the current paths/secrets.

## Isolation and idle overhead

The locked `homelab-valheim` account owns a separate Docker daemon and has no
main-Docker/sudo supplementary groups. The game receives **no Docker socket**.
Container root maps to this unprivileged host account; this shares the host
kernel and is weaker isolation than a VM. Protect private host/NAS files using
normal filesystem permissions. Existing website/Pi-hole/NAS services keep their
own daemon and configuration.

After stopping the game, measure the daemon/timer's actual idle footprint:

```bash
uid=$(id -u homelab-valheim)
sudo systemctl show user@$uid.service -p MemoryCurrent -p CPUUsageNSec
```

CPUUsageNSec is cumulative; compare readings over an idle interval. Memory also
includes filesystem cache. To retire the installation, stop the game, set
`valheim_enabled: false`, and stop/disable its Docker user service and timer;
preserve the storage directories for recovery.

References: [maintained Valheim image](https://github.com/community-valheim-tools/valheim-server-docker)
and [Docker rootless setup](https://docs.docker.com/engine/security/rootless/).
