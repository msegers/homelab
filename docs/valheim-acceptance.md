# Valheim live acceptance checks

Run on a backed-up/test world before relying on unattended shutdown. These checks
require the EliteDesk and real players; local regression tests cannot establish
that a particular Valheim version reports its player count accurately.

## Handoff status (2026-11-05)

- Runtime tooling is now Rust in `tools/valheim/`: controller, systemd idle checker,
  private dashboard, and Unix-socket broker. The Python controller and in-container
  Python player query are gone. Python remains only in Ansible-related tests.
- Local checks cover the Rust lifecycle/archive/idle logic, native UDP query
  parsing, HTTP/Unix-socket API, UI behavior, rendered Compose/systemd units, and
  Ansible provisioning. Commands are in [the development guide](../tools/valheim/README.md).
  Static musl release binaries build locally; no live deployment was performed.
  Result: **35 Rust + 20 Ansible + 5 UI tests passed**, none skipped; formatting,
  Clippy, playbook syntax, rendered systemd verification, and diff checks passed.
- The homelab host opts into the dashboard and one-hour idle shutdown. The role
  defaults both off. Deploy the updated role, then use the
  [SSH-tunnel instructions](valheim.md#private-dashboard).
- No public route, Docker socket access for the web account, or frontend framework.
  Backup/update/world-selection stay CLI-only. Existing save/selection/archive
  formats are preserved; Ansible adds the native query address to control settings.
- Next operator checks: verify dashboard units and the loopback-only listener,
  open the page through SSH, exercise start/status/stop/restart, and verify other
  services stay untouched. Gameplay/player detection still needs real clients;
  disable idle shutdown while validating if not already verified. Backup/restore
  and retirement checks below remain live acceptance tasks, not automated claims.

## Image and account checks

The pinned image manifest is version **1.4.1**, source revision
[`d333bbeffab3f7cf4d9f23678acc32ef51ae28e2`](https://github.com/community-valheim-tools/valheim-server-docker/tree/d333bbeffab3f7cf4d9f23678acc32ef51ae28e2).
Its [bootstrap](https://github.com/community-valheim-tools/valheim-server-docker/blob/d333bbeffab3f7cf4d9f23678acc32ef51ae28e2/bootstrap)
starts as root, maps the container's `valheim` user to PUID/PGID, and recursively
chowns its game/config mounts. It also configures timezone, cron, and Supervisor.
The [Supervisor config](https://github.com/community-valheim-tools/valheim-server-docker/blob/d333bbeffab3f7cf4d9f23678acc32ef51ae28e2/supervisord.conf)
runs the game, updater and backup programs as `user=valheim`; Supervisor, cron
and syslog remain root inside the container. A Compose-level non-root `user:`
would break that bootstrap. The role uses the supported PUID/PGID path, restrictive
data permissions, no-new-privileges, no privileged mode or extra capabilities,
and drops NET_RAW. It neither mounts Docker's socket nor installs another daemon.

After provisioning and starting, on the server:

```bash
getent passwd valheim-game
id valheim-game
sudo passwd -S valheim-game
sudo -l -U valheim-game
sudo docker --context default exec valheim-server id valheim
sudo docker --context default top valheim-server -eo pid,uid,gid,comm
sudo docker --context default inspect --format '{{.HostConfig.Privileged}} {{json .HostConfig.CapAdd}} {{json .HostConfig.CapDrop}}' valheim-server
```

Expect a locked system account with `nologin`, only its dedicated group, and
**no sudo authorization** (that command should exit nonzero). The game worker's
UID/GID must match the host account; root Supervisor/cron processes are expected.
Privileged must be false, CapAdd empty/null, and CapDrop include NET_RAW. Avoid
printing full container environments/command lines: they include the password.
Create/save a world, stop/start it, and verify your changes persist.

Run provisioning twice, recording `id valheim-game`, container StartedAt, and
world selection. IDs must stay unchanged. After `./scripts/valheim stop`, repeat
provisioning: the game must remain stopped. Record other homelab containers'
IDs/StartedAt before and after Valheim start/stop; those must stay unchanged.

## Player-count and shutdown test

Keep `valheim_idle_shutdown_enabled: false` initially, including if you previously
enabled the preview's default. Start the game and wait for it to initialize:

```bash
./scripts/valheim start
./scripts/valheim status
```

Status includes `game_state`, `players`, and separate `a2s.info_players` and
`a2s.player_list_entries` observations. Check all of these transitions over
several repeated polls:

1. Empty world: both query counts are zero and game_state is RUNNING.
2. Join and fully enter the world with a real Steam client: both counts must
   become one, not zero. If possible, join a second client and verify two.
3. Disconnect/reconnect clients; verify counts track the known occupancy,
   including returning to zero only after everyone leaves.

A2S_PLAYER can omit player names; the length of its returned entries is used,
not non-empty names. It is **not universally reliable**: upstream documents
false-zero crossplay behavior in [common](https://github.com/community-valheim-tools/valheim-server-docker/blob/d333bbeffab3f7cf4d9f23678acc32ef51ae28e2/common)
and [issue #663](https://github.com/community-valheim-tools/valheim-server-docker/issues/663).
We reject private-listing/crossplay modes and disagreeing/error responses, but
both queries can agree incorrectly. If occupied readings are wrong, leave
shutdown disabled. Re-test after Steam/game/image/mode changes.

Only after those checks, temporarily enable a short timeout on a test world:

```bash
cd ansible
ansible-playbook site.yml --tags valheim --ask-vault-pass -e '{"valheim_idle_shutdown_enabled": true, "valheim_idle_timeout_seconds": 120, "valheim_idle_poll_seconds": 15}'
```

Stay connected longer than two minutes: the game must remain running. Leave,
rejoin before the timeout, and verify the journal reports a reset. Finally leave
everyone disconnected and confirm a graceful stop after at least two minutes
of confirmed empty samples. Query failures must reset the countdown, not stop
the server. Watch `sudo journalctl -u valheim-idle.service -f` on the server.

Set the intended host variables (normally 3600 seconds/60-second polling) and
rerun provisioning **without** the temporary overrides. Opt in permanently only
if every occupied/empty test passed. There is still an unavoidable race if a
player joins at the exact final stop instant.

## Recovery and retirement checks

- Run `backup`, inspect its archive, and test restoring into a separate directory
  before discarding any saves. Manual archives include the game password; store
  them securely. Failed disk writes, publication/name collisions, and failed
  resume/restart behavior are covered by fault-injection unit tests.
- `./scripts/valheim retire` must stop the game and disable/cancel its timer;
  verify with `status` and `systemctl is-enabled valheim-idle.timer`. Worlds and
  backups must remain present. Set `valheim_enabled: false` and rerun Ansible;
  it must stay retired without changing Caddy, Pi-hole, or the website.
