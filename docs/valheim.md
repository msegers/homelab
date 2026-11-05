# Optional Valheim server

Valheim uses the EliteDesk's existing Docker Engine, in its own Compose project.
Scripts control it over SSH. An opt-in systemd timer can stop it after an hour
empty, once player detection has passed the [live acceptance test](valheim-acceptance.md).

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

`world NAME` backs up before switching, preserves whether the server was running,
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

## Automatic shutdown

When enabled, the system-wide **`valheim-idle.timer`** checks every 60 seconds. After
3600 seconds of confirmed zero-player readings, it gracefully stops the game
container (120-second save/shutdown allowance).

- Player activity, failed/disagreeing queries, restarts, and monitoring gaps
  reset the countdown. Connection logs also catch short visits between polls.
- Counts are queried inside the container using Steam A2S. This requires
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
