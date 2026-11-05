# Valheim regression checks

Requires Ansible, the `community.docker` collection, PyYAML (included with
Ansible), and Docker Compose v2. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

For a standalone Compose binary, set `COMPOSE_COMMAND=/path/to/docker-compose`.
For the Pi-hole Caddy integration check, install Caddy or set
`CADDY_COMMAND=/path/to/caddy` (otherwise that check is skipped). It validates the
shipped proxy configuration and exercises its actual peer allowlist on ephemeral
loopback listeners, including spoofed forwarding headers, with a harmless
upstream fixture. It never contacts the real Pi-hole or requests certificates.
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
