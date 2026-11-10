# Repository guidance

## Language and maintainability

- Use **Rust** for tooling, controllers, services, and systemd-managed programs.
- Do not add Python runtime code. Python is allowed only where tightly coupled
  to Ansible (for example, its provisioning regression tests).
- Migrate existing Python tooling to Rust when working on it; preserve behavior
  and move its regression coverage rather than just dropping tests.
- Keep HTML, CSS, and JavaScript in separate, readable files. Avoid embedding
  large HTML strings in backend code. Prefer small modules and minimal build steps.
- Discuss meaningful architecture/UI decisions with the owner before implementing
  them. If explicitly asked to work unattended, choose conservative defaults and
  document decisions and remaining questions in the PR.

## Deployment and safety

- Ansible owns installation, configuration, and systemd lifecycle.
- The Valheim dashboard is private: the homelab host uses its LAN IP with an
  explicit client subnet allowlist; reusable defaults remain loopback-only. Do not add
  public Caddy routes, public listeners, or router forwarding without approval.
- Do not expose Docker's socket to a web process. Keep web permissions limited
  to the intended Valheim operations.
- Preserve saves, backups, selected worlds, and other homelab services.
- Never deploy, restart, or stop live services without explicit authorization.
- Test locally before proposing deployment; clearly distinguish automated
  verification from real-server/gameplay acceptance.

## Checks

- When making changes, check whether the Homelab dashboard (`tools/homelab/`, `/homelab`) needs corresponding updates to its service list, metrics, UI, tests, or documentation.
- Rust: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and
  `cargo test` from `tools/homelab/`.
- Ansible regression tests: `python3 -m unittest discover -s tests -v`.
  Set `COMPOSE_COMMAND` if using a standalone Compose v2 binary.
- Check playbook syntax and `git diff --check`. Keep Cargo.lock committed.
