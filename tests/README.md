# Ansible regression checks

Requires Ansible, the collections in `ansible/requirements.yml`, PyYAML (included with
Ansible), and Docker Compose v2. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

For a standalone Compose binary, set `COMPOSE_COMMAND=/path/to/docker-compose`.
For the private-route integration checks, install Caddy with the Cloudflare
module (`github.com/caddy-dns/cloudflare@v0.2.4`) or set
`CADDY_COMMAND=/path/to/caddy-cloudflare` (otherwise those checks are skipped).
OpenSSL is needed for disposable local TLS fixtures. CI downloads a module-enabled
Caddy build. Tests validate the production DNS-01 configuration, then exercise
HTTP/HTTPS peer gates and Host/Origin/token/scheme forwarding using ephemeral
loopback listeners and a harmless upstream fixture. They never contact the real
Pi-hole, request ACME certificates, modify DNS, or install a trusted CA.
These Python tests are tightly coupled to Ansible only: temporary provisioning,
rendered Compose/systemd validation, opt-out, storage/secret handling, and the
Ansible CLI wrapper. They never provision the real host. Runtime controller and
dashboard tests are Rust, in `tools/valheim/`:

```bash
cd tools/valheim
cargo test --locked
node --test web/app.test.mjs
```

Those cover idle shutdown, game lifecycle, archives/recovery, native Steam query
parsing, HTTP access restrictions, Unix-socket operations, and UI behavior.
Tests use fake Docker engines and local ephemeral HTTP/UDP/Unix sockets, not the
real Docker daemon. See the [project guide](../tools/valheim/README.md).

`test_vault_migration.py` additionally checks the read-only migration verifier
using temporary encrypted fixtures: moves preserve decryption, re-encryption/new
keys are allowed, and missing/changed old values fail without logging secrets.
It does not decrypt or assert the values in the real committed Vault files.
A real host is still needed to verify first boot, gameplay, and idle shutdown
with actual players. System timer configuration and project scoping are checked
without starting services.

`test_docker_networking.py` checks the ifupdown boot dependency and exercises the
actual Ansible attachment/port assertions with healthy-but-detached, stopped,
wrong-network, incomplete-endpoint, missing-port, and wildcard/wrong-address
fixtures. It also renders the exact-address start condition, exercises it with
fake `ip` output, and parses synthetic units with `systemd-analyze verify` when
available. It never contacts Docker or the homelab or starts a unit. See
`ansible/README.md` for owner-run deployment/reboot acceptance.

`test_storage.py` executes the role's Ansible safety assertions against fixtures:
initialization opt-in and interrupted-run records, disk identities/OS protection,
mounts/swap/holders, existing signatures and fstab ownership, RAID membership and
degraded health, and filesystem/mount UUIDs. It also checks idempotent mdadm
configuration in a temporary file. A restricted-PATH check-mode test runs only
`wipefs --no-act` on a disposable regular file and verifies it is unchanged.
Tests never open real disks, mount a filesystem, or run destructive commands.
Real-disk initialization, completed resync, and
reboot acceptance remain owner-run steps in `docs/storage.md`.
