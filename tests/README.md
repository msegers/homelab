# Valheim regression checks

Requires Ansible, the `community.docker` collection, PyYAML (included with
Ansible), and Docker Compose v2. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

For a standalone Compose binary, set `COMPOSE_COMMAND=/path/to/docker-compose`.
Tests use temporary files and mocked Docker responses; they never contact a Docker daemon
or provision the real host. They check default opt-out, bootstrap idempotence,
preservation of world selection, storage paths, secret quoting, profiles, and
idle shutdown behavior (players, query failures, monitoring gaps, and restarts).
A real host is still needed to verify first boot, gameplay, and idle shutdown
with actual players. System timer configuration and project scoping are checked
without starting services.
