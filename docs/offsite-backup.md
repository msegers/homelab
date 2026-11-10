# Mutual off-site backups: restic + rest-server

**Opt-in, not deployed.** Each direction is independent: we send our encrypted
backups to a friend's REST endpoint, and optionally host their encrypted repository.
The friend need not use our Ansible, paths, scheduler, or operating system.

## Security and scope

- Restic encrypts on the sender. Each owner keeps their own repository password;
  exchange HTTP credentials and public TLS certificates/CA trust, **not encryption keys**.
- The receiver runs as a locked, unprivileged account with TLS 1.3, bcrypt HTTP
  authentication, `--private-repos` and `--append-only`. A username `friend`
  connects to `rest:https://host:8000/friend/`. Lock-file deletion is permitted by
  rest-server so normal backups work; snapshot/data deletion and overwrites are not.
- Append-only protects against deletion through the backup endpoint. The storage
  host's administrator, malware with local storage access, disk failure, or loss of
  the encryption password can still destroy availability. This is not immutable storage.
- No shell access, Docker socket, public Caddy route, router forwarding, VPN changes,
  automatic init, prune, forget, unlock, application stop, or application restart.
- Receiver storage is `/srv/storage/backups/peers`, mode 0700. Its systemd namespace
  binds just that directory into a private runtime path so root-only RAID parent
  permissions stay intact. A mount UUID/type guard and mount dependency prevent SSD
  fallback. Outgoing sources are read-only inside their service sandbox.
- Default outgoing sources: `/srv/storage/shared`, `/srv/storage/immich` (including
  automatic database dumps), `/srv/storage/backups/valheim`. Never back up incoming
  repositories, PostgreSQL's live files, or the restic cache. `--one-file-system`
  avoids crossing nested mounts; symlinks are not followed. Custom source selection
  is limited to these trees, not an arbitrary filesystem backup framework.

**Immich consistency:** this is an online file backup, not a coordinated database
dump/media snapshot. Verify automatic dumps are succeeding and fresh. It does not
guarantee an application-consistent point in time under concurrent changes. For an
initial trusted copy, follow [Immich's backup guidance](https://docs.immich.app/administration/backup-and-restore/)
and arrange an owner-authorized quiet/stopped window. Restores need compatible
Immich versions, media and a database dump; never restore onto the live library
as a test. Valheim coverage is existing completed archives, not live saves.

## Private transport: follow-up PR

Tailscale is already available, but this PR does not change its policy. Before
enabling remote access, review device sharing/tags and Tailscale grants/ACLs so the
friend can reach **only the receiver's TCP backup port**, not SSH, SMB, dashboards,
or the rest of the LAN. Existing broad allow rules must be considered; an added
narrow grant does not override an existing broad grant. Verify both allowed and
denied access from the friend's device. No subnet routing or second VPN is needed.

The receiver defaults to loopback. An explicitly configured, currently assigned
Tailscale IPv4 (`100.64.0.0/10`) is the only non-loopback binding allowed here.
Binding to a Tailscale address alone does **not** restrict other tailnet members;
do not enable it before policy review. TLS verification is always required, even
over the overlay. The private endpoint and firewall policy on the friend's server
remain their responsibility. Boot-before-Tailscale/address recovery needs real
host acceptance; this PR adds no Tailscale lifecycle dependency or repair daemon.

## Configuration

Defaults and available knobs: [`defaults/main.yml`](../ansible/roles/offsite_backup/defaults/main.yml).
Keep non-secret settings in `ansible/host_vars/homelab/main.yml`; edit the existing
Vault with `ansible-vault edit host_vars/homelab/vault.yml` from `ansible/`.
Do not replace existing Vault contents. Example sender settings, **after transport
approval and repository initialization**:

```yaml
offsite_backup_send_enabled: true
offsite_backup_repository: rest:https://friend.example.ts.net:8000/homelab/
offsite_backup_username: homelab
offsite_backup_http_password: '{{ vault_offsite_http_password }}'
offsite_backup_password: '{{ vault_offsite_repository_password }}'
# Optional private/self-signed CA PEM supplied by the friend; omit for public CA.
offsite_backup_ca: '{{ vault_offsite_ca }}'
offsite_backup_upload_limit: 2048 # KiB/s
# Pick a time when the friend's tower is usually on. Hourly is also possible.
offsite_backup_calendar: '*-*-* 19:30:00'
```

Use a random repository password of at least 20 characters and a separate random
HTTP password of at least 12. Keep the repository password in an independent
password manager/recovery record: losing this host must not lose the only key.
Do not put passwords in endpoint URLs, command arguments, Git, or logs. Files are
root-only; Ansible suppresses secret task logs and diffs. The friend may use any
compatible authenticated HTTPS REST endpoint, with their own repository path and CA.

Receiver example (independent of sending):

```yaml
offsite_backup_receive_enabled: true
offsite_backup_listen_ip: 100.64.12.34 # replace with this host's assigned address
offsite_backup_htpasswd: '{{ vault_offsite_htpasswd }}'
offsite_backup_tls_cert: '{{ vault_offsite_tls_cert }}'
offsite_backup_tls_key: '{{ vault_offsite_tls_key }}'
offsite_backup_max_size: 400000000000 # optional per-repository byte limit
```

Generate a bcrypt htpasswd entry with `htpasswd -nB friend` on a trusted machine
(interactive password prompt), then store the resulting line in Vault. Multiple
lines/users are supported, not a generalized tenant manager. Provide a certificate
whose SAN matches the endpoint hostname/IP and its matching private key; distribute
only the public certificate/CA. Certificate issuance/renewal is owner-managed here.
Changed credentials/certificates restart **only the receiver** during an authorized
apply. There is no automatic renewal task.

The receiver downloads upstream rest-server **0.14.0 Linux amd64** with a pinned
SHA-256. Version/checksum must be updated together. The sender uses Debian's restic
package. No custom runtime program or new Rust dependency is needed.

## Initialization and owner-run deployment

The independent playbook touches only backup services, not storage provisioning,
networking or applications. Mounted, verified storage is a prerequisite. Both
directions default disabled, including during normal `site.yml` runs.

```bash
cd ansible
ansible-playbook offsite-backup.yml --check --ask-vault-pass --ask-become-pass
# Only after explicit installation/startup authorization:
ansible-playbook offsite-backup.yml --ask-vault-pass --ask-become-pass
```

Check mode reads mount/path state without starting services. It cannot validate
not-yet-installed binaries, TLS keys or remote reachability. Reapply to check
idempotence. A first-enable check can also fail on not-yet-created users/directories;
it is most useful against an already installed role. Existing unmanaged
units/configuration are refused, not adopted.

After sender configuration installation, initialize **once**, manually, in an
owner shell. The first timer run fails until the repository exists; Ansible never
creates or replaces it. Do not source the generated environment file: systemd
quoting is not shell quoting. Instead launch a transient restic job
using the same environment file:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/homelab-offsite-backup/client.env \
  /usr/bin/restic init
sudo systemctl start homelab-offsite-backup.service
sudo journalctl -u homelab-offsite-backup.service --no-pager
```

The scheduled unit is oneshot, so overlapping timer/manual starts cannot run two
copies of that unit. Default is daily at 03:30 with up to 30 minutes jitter, a
2 MiB/s upload limit and a 12-hour timeout. No immediate provisioning/catch-up
backup, persistent catch-up, automatic retries, or retention. Offline peers and
interrupted uploads fail the run; the next scheduled run retries incrementally.
Choose a daytime/hourly calendar for an intermittently powered peer. Interrupted
runs may leave stale locks: inspect before an owner-authorized `restic unlock`.

## Retention, capacity and acceptance

Append-only grows indefinitely. Start with **no pruning**. Agree on a capacity
budget and maintenance procedure before filling the disk. `--max-size` is only
per repository (subrepositories can consume additional space), not a hard tenant
quota or disk reservation. Incoming backups share RAID capacity with local data;
monitor free space and stop receiving before this threatens applications. For
strong quotas, a separate filesystem/project quota needs a separately reviewed plan.

Later pruning must be owner-controlled maintenance with temporary deletion access,
without handing the storage peer your encryption password or granting the normal
backup host a permanent unrestricted endpoint. The peer cannot prune encrypted
snapshots safely without the key. Do not add an automatic host-side prune job.

Before trusting either direction, use a **disposable repository** to verify:

1. Valid HTTPS/authentication, backup and byte-for-byte restore, then `restic check
   --read-data`. Wrong password and another username's repository path must fail.
2. Create a snapshot and explicitly `restic forget SNAPSHOT_ID`: it must fail with
   HTTP 403, and the snapshot must remain readable. Older restic versions may
   still exit zero after logging that rejection, so do not rely on exit status
   alone. `forget --keep-last 1` on a single snapshot is a no-op, not a test.
   Do not test deletion protection against irreplaceable backups.
3. Recover a representative Immich dump/media set on a separate disposable instance
   and a Valheim archive separately; retain the encrypted Vault/config versions
   elsewhere too. Physical seeding can avoid a multi-terabyte initial upload.
4. Inspect `systemctl status homelab-rest-server`, timer schedule, journal failures,
   `ss -lnt`, and real Tailscale access boundaries. Test offline-peer/reboot recovery
   in an approved window. Never unmount the live array for testing.

Local tests cover rendered units, guards, opt-out and optionally real TLS REST
backup/restore/authentication/deletion protection. They do **not** prove systemd
namespace execution, hardware/boot behavior, dump freshness or gameplay acceptance.

The Homelab dashboard already reports RAID usage, but discovers Compose containers,
not native backup services. No dashboard service entry/control is added for disabled
services; use journals and `systemctl list-timers homelab-offsite-backup.timer`.
Last-success age, capacity alert delivery and backup health UI are **not implemented**;
failed/stale backups need owner review until monitoring is added.

To disable either direction, persist its enable flag as false and apply the
independent playbook. Only its managed service/timer is stopped/disabled. Repositories,
passwords, cache, accounts and configuration remain; there is no deletion task.
