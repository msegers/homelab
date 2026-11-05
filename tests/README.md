# Valheim regression checks

Requires Ansible, the `community.docker` collection, PyYAML (included with
Ansible), and Docker Compose v2. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

For a standalone Compose binary, set `COMPOSE_COMMAND=/path/to/docker-compose`.
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
A real host is still needed to verify first boot, gameplay, and idle shutdown
with actual players. System timer configuration and project scoping are checked
without starting services.
