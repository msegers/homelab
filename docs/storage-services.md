# Private SMB and Immich on the existing RAID

## Layout and safety

The November 7, 2026 read-only host inspection confirmed a healthy whole-disk
mdadm RAID 1 (`[UU]`), `/dev/md/storage` resolving to `/dev/md127`, with ext4 UUID
`3b5ea8e0-2a29-42b5-bf10-84323fd768f1` mounted read/write at `/srv/storage`.
UUID-based fstab persistence already exists. These changes reuse it; **do not
initialize, reformat, or migrate anything**. The existing storage role maintains
the identity record and verifies the hardware, RAID membership and filesystem.
If its record is missing, stop for diagnosis; do not enable initialization.

| Location | Purpose / permissions |
| --- | --- |
| `/srv/storage/shared` | The **only** SMB export; `shared:shared`, directory 2770, new files 0660. |
| `/srv/storage/immich` | All Immich media, thumbnails, transcodes, and automatic DB dumps; private root-owned directory. |
| `/srv/storage/valheim` | Reserved, private directory; live game data is **not** moved. |
| `/srv/storage/backups` | Private local staging, **not an independent backup**. |
| `/srv/storage/backups/valheim` | New Valheim backup output, owned by the existing game account, 0700/0600. |
| `/srv/storage/backups/immich` | Optional private staging for exports/copies; no automatic duplicate media copy. |
| `/var/lib/homelab-immich` | SSD: PostgreSQL, Valkey state and ML model cache. |
| `/opt/immich` | SSD: protected Compose configuration and `.env`. |

Paths and credentials are role variables. The bulk media/export locations are
deliberately constrained to the approved directories to avoid exposing private
data or accidentally placing assets on SSD. Existing directory contents are not
recursively chowned. PostgreSQL/Valkey own their initialized subdirectories;
repeat provisioning must not reset that ownership.

Consumers verify the exact writable ext4 UUID before creating directories.
Samba and Immich have `RequiresMountsFor`, `After`, and `BindsTo` dependencies on
`srv-storage.mount`, plus a UUID/type/read-write startup check. Their startup
fails if mounting fails; normal mount removal stops them. Immich container
restart policies are **off**: systemd owns their lifecycle, preventing Docker's
boot restoration from bypassing mount checks. Bind mounts forbid automatic
host-path creation. Neither service gates Docker globally or changes unrelated
services' boot dependencies. Recovery after a missing mount is deliberately
manual: fix the storage, then explicitly start the affected service.

Valheim's Rust controller verifies required mount UUIDs before start/archive,
and the backup bind mount also forbids host-path creation. An already running
container holds its original filesystem bind mount rather than falling through
to SSD. Forced/lazy unmounts and disk-failure simulation are not acceptance
tests; do not perform them on the live game/library.

## Secrets and deployment (owner-run)

Nothing has been deployed by these repository changes. From `ansible/`, edit
the **existing** Vault; do not recreate it or replace existing secrets:

```bash
ansible-vault edit host_vars/homelab/vault.yml
```

Add `vault_samba_password` (12+ characters, no newlines) and
`vault_immich_db_password` (24–128 random alphanumeric characters, per Immich's
Compose recommendations). Keep passwords in your password manager. No plaintext
credentials belong in Git, command-line extra-vars, logs, or `--diff` output.
Rendered `.env` and Compose files are root-only. Changing the Vault DB password
does **not** rotate an initialized PostgreSQL account; the role refuses that
mismatch. Review a separate credential-rotation procedure if needed.

The committed host settings enable these services; reusable role defaults are
disabled. `site.yml` provisions storage first, the existing Docker/Caddy stack,
then SMB and Immich. Independent service playbooks require existing Docker,
the proxy network, and mounted/verified storage; they do not provision those
dependencies or reconfigure other services.

```bash
ansible-playbook site.yml --tags storage --check --ask-vault-pass --ask-become-pass
ansible-playbook site.yml --tags storage --ask-vault-pass --ask-become-pass
ansible-playbook samba.yml --check --ask-vault-pass --ask-become-pass
ansible-playbook immich.yml --check --ask-vault-pass --ask-become-pass
```

Check mode on an existing host reads mount/Docker state but starts nothing. It
cannot validate a not-yet-rendered Compose file or use newly installed Samba
utilities. The real apply validates both configurations before starting them.

When ready to authorize installation/startup and Caddy/Pi-hole reconfiguration,
use the normal full deployment in a maintenance window:

```bash
ansible-playbook site.yml --ask-vault-pass --ask-become-pass
```

For subsequent independent deployments:

```bash
ansible-playbook samba.yml --ask-vault-pass --ask-become-pass
ansible-playbook immich.yml --ask-vault-pass --ask-become-pass
```

Repeat each apply: unchanged configuration should report no changes. Existing
unmanaged Samba or Immich configuration causes a failure, not an overwrite.
Review and back up any such configuration before explicitly adopting it. A
failed first Samba installation can leave the package's stock `smb.conf`; review
it too rather than removing the guard. Password updates use
`samba_update_password: true` only for an explicitly authorized rotation;
ordinary runs do not rewrite Samba's password database.

### Private HTTPS and networking

`https://photos.mathijs.se` is LAN-only through existing Caddy, using the same
Cloudflare DNS-01 certificate setup as Home Tools. Pi-hole resolves it to
`192.168.68.111`. LAN clients must use that DNS server. Caddy checks the real
TCP peer against `192.168.68.0/24`; forged forwarding headers do not grant access.
Immich publishes **no host port**. Only its server joins `homelab_default` with
alias `homelab-immich`; PostgreSQL, Valkey and ML use its separate project network.
An Immich deployment does not restart Caddy; initial route/DNS setup is part of
the normal full deployment. Disabling/removing Immich can leave a harmless
private 502 route until the optional Caddy route/DNS record is separately disabled.

Do not add public A/AAAA records, listeners or router forwarding. DNS-01 needs
none of those. The certificate hostname can appear in public transparency logs.
Away from home, mobile uploads wait until LAN access returns, unless you arrange
an explicitly authorized VPN and compatible access rules. Immich login is still
required inside the LAN. Create the first admin account promptly after deployment.

Samba binds only the configured LAN IP, TCP 445, with its own LAN client
allowlist. SMB2/3 and authentication are required; NetBIOS, guests, printers and
symlink traversal are disabled. No Immich, game, or backup path is exported.
There is no Ansible-owned firewall in this repository. Inspection found nftables
and UFW services inactive, which does **not** establish whether rules exist.
Before applying, inspect `nft list ruleset` and any Docker/iptables filtering in
a privileged host shell. Preserve all existing rules; if an existing firewall
blocks SMB, add only a LAN-source TCP 445 allowance using its current management
mechanism. Do not flush rules or expose Docker's socket.

On this ifupdown Wi-Fi host, Samba also requires the same
`ifup@wlp0s20f3.service` instance whose one-shot readiness correction is already
managed by the Docker role. This prevents Samba binding before DHCP finishes;
no networking restart or new readiness daemon is added. Independent Samba
deployment assumes that existing readiness configuration is already applied.

### Connect to SMB

Windows: enter `\\shared.mathijs.se\shared` in File Explorer (or Map network drive),
then log in as **`shared`** with the Samba password. The share name and account
name are the same, but represent different things. No Windows discovery service
is needed. For a subfolder use `\\shared.mathijs.se\shared\folder`. This is an
SMB path, not a browser URL. Pi-hole supplies the private `shared.mathijs.se`
record; clients must use LAN DNS. `\\192.168.68.111\shared` also works without
DNS. No Caddy route, HTTPS certificate or public DNS record is needed for SMB.

Linux: open `smb://shared.mathijs.se/shared` in a file manager, or install `cifs-utils`
on the **client** and mount interactively:

```bash
sudo mkdir -p /mnt/homelab-shared
sudo mount -t cifs //shared.mathijs.se/shared /mnt/homelab-shared \
  -o username=shared,vers=3.1.1,uid="$(id -u)",gid="$(id -g)",nosuid,nodev
```

Enter the password at the prompt, not in shell history. For persistent client
mounts, use a private root-owned 0600 credentials file rather than plaintext
credentials in fstab. Test file creation, nested directories, editing and
deletion from both client types; unauthenticated access must fail.

## Immich version and resource budget

Configuration is based on the official **v3.3.6 release** Compose artifact, not
the development branch or an old community example:

- [Official Compose installation](https://docs.immich.app/install/docker-compose/)
- [Release Compose artifact](https://github.com/immich-app/immich/releases/download/v3.3.6/docker-compose.yml)
- [Requirements](https://docs.immich.app/install/requirements/)

The four upstream services and release-matched database/vector and Valkey image
digests are retained. Local adjustments are private networking, safe bind mounts,
systemd restart ownership, bounded logs, and memory/CPU limits. PostgreSQL stays
on the local SSD root filesystem (ext4/XFS), never SMB/NFS or the RAID. The role
rejects symlinked paths. Do not manually create nested mounts under its SSD paths.

Immich currently requires at least 6 GB RAM and recommends 8 GB; PostgreSQL needs
at least 2 GB when capped. Limits total 8.25 GiB: server 4, ML 2, PostgreSQL 2,
Valkey 0.25. Server/ML each have a two-CPU quota. This leaves nominal headroom
on the 16 GB host, **not a guarantee** under concurrent Valheim/import load.
Start with a small import, reduce job concurrency in Immich administration, and
schedule large imports/transcoding outside gameplay. Watch memory/swap and OOM
events before raising limits. No automatic upgrades or GPU setup are introduced.
Review upstream release notes, Compose changes and backups together on upgrades.

## Backups and existing monitoring

The existing image backup worker is enabled for Valheim: scheduled hourly while
its container is running (the image is configured to skip idle backups), maximum
age 14 days and maximum count 48 archives. Idle
shutdown means no scheduled backups while stopped. New archives go to
`/srv/storage/backups/valheim`; live saves and existing SSD backups are untouched.
Provisioning only renders the game's configuration. A running game adopts it
on the next **owner-authorized restart**, not during this change. The controller's
manual/pre-change archives use the new destination after controller deployment.
Native Valheim save backups remain enabled. These RAID copies help recover from
SSD loss or an accidental world change; they are not off-machine backups.

Immich automatically dumps its database under `/srv/storage/immich/backups`
(upstream default: daily at 02:00, keep 14). Verify a successful dump in
Administration and test a restore on a separate disposable instance. Those dumps
contain metadata, **not photos**. Keep a copy of the complete media tree plus DB
dump outside this host; do not treat the live PostgreSQL directory as a valid
file-level database backup. `backups/immich` is optional staging for an export,
not a second automatic media tree that fills the same array.

For a simple manual external backup, trigger a fresh DB dump, then copy the
media tree (including its `backups` directory) to an external disk/other host.
If changes cannot be paused, follow upstream's ordering: database first, media
second. A coordinated offline copy can instead be taken after an explicitly
authorized clean Immich stop. Do not issue ad-hoc Compose stop commands behind
the supervising systemd unit. Also preserve the encrypted Vault, release/config
versions and external libraries if added later. No backup scheduler, deletion
policy, or external destination is invented here.

[Official backup/restore guidance](https://docs.immich.app/administration/backup-and-restore/)
explains version compatibility and the current UI restore flow. Never erase a
database or existing library just to try a restore.

Read-only inspection confirmed running `mdmonitor` (`mdadm --monitor --scan`),
running `smartd`, and scheduled `mdcheck_start`/`mdcheck_continue` timers. Keep
these lightweight distro tools. Verify their selected disks, SMART test schedule,
mdadm MAILADDR/PROGRAM and smartd notification settings with the owner; actual
alert delivery has **not** been verified. Do not assume root mail is delivered.
`cat /proc/mdstat`, `mdadm --detail --test /dev/md/storage`, and `smartctl -a` on
each approved member are useful read-only diagnostics. No automatic repair or
disk replacement is configured. The dashboard automatically lists Compose
containers and already reports SSD/RAID capacity; it does not report SMART/RAID
health or native Samba service state.

## Acceptance and independent removal

Local regression tests check rendered configuration, mount guards and isolation;
they do not prove hardware, gameplay, real uploads or reboot behavior. After
deployment, inspect in a privileged host shell:

```bash
findmnt --mountpoint /srv/storage --output SOURCE,UUID,FSTYPE,TARGET,OPTIONS
systemctl status smbd homelab-immich --no-pager
systemctl cat smbd homelab-immich
testparm --suppress-prompt
docker compose --project-directory /opt/immich ps --all
ss -lnt
```

Require all four Immich containers running/healthy, no published 2283/5432/6379
ports, Samba bound only to the LAN address on 445, valid HTTPS through local
DNS, successful browser/mobile photo and video uploads, working thumbnails and
DB dumps, and no guest/off-subnet access. Confirm the public website, Pi-hole,
Home Tools and expected Valheim state are unchanged. Missing-mount behavior is
covered with local fixtures; do not unmount live storage to test it. Arrange a
separate owner-approved reboot window with console access and verify mount and
service order **before** rerunning Ansible. Repeat apply for idempotence.

To stop/disable only Immich while preserving all data:

```bash
ansible-playbook immich.yml -e '{"immich_enabled": false}' --ask-vault-pass --ask-become-pass
```

Also set `immich_enabled: false` in inventory to prevent a later full deployment
reenabling it. The unit's Compose `down` removes only its project containers and
private network, never the external proxy network, data directories or volumes.
No `down -v` or data-deletion task exists. Optional route/DNS cleanup can be
reviewed separately; SMB and other services remain running.
