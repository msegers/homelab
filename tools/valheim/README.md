# Valheim tools

One Rust project for the controller, idle checker, and private dashboard. Ansible
is the installer, not the application's source directory.

## Build and test

Install Rust **1.88+** with rustup on your Linux Ansible controller:

```bash
rustup target add x86_64-unknown-linux-musl
cd tools/valheim
cargo test --locked
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
node --test web/app.test.mjs
cargo build --release --locked --target x86_64-unknown-linux-musl
```

The musl target is statically linked using Rust's bundled linker: no system
musl compiler, target-side Rust install, Node runtime, or Python daemon is needed.
The lockfile pins Rust dependencies. Node is only used for optional UI tests.
The deployment controller must be Linux x86_64 for this default build path.

## Where to change things

| Path | Responsibility |
| --- | --- |
| `src/controller.rs` | Lifecycle, state/locking, idle countdown, status |
| `src/engine.rs` | Explicitly scoped Docker commands with timeouts |
| `src/a2s.rs` | Native UDP Steam player-count queries |
| `src/archive.rs` | Private, consistent, non-overwriting backups |
| `src/dashboard.rs` | HTTP API and restricted local broker |
| `src/bin/` | Two small executable entry points |
| `web/index.html`, `web/app.css`, `web/app.js` | UI, without a frontend framework |
| `tests/` | Controller and HTTP/Unix-socket regression tests |
| `../../ansible/roles/valheim/` | Configuration, build/install, systemd units |

Frontend files are compiled into the dashboard binary with `include_str!` from
separate files. Edit them normally and rebuild/redeploy; no npm install or asset
bundler. UI testing uses Node's built-in runner with a small DOM/fetch stub.

## Privilege boundary

```text
LAN browser -> private address:8787 (TCP client subnet allowlist)
                         dashboard (valheim-dashboard user)
                             -> /run/valheim-dashboard/control.sock
                                broker (root, Unix socket only)
                                    -> shared Rust controller -> existing Docker Engine
```

The web process has neither Docker access nor sudo authorization. Its account is
locked and has no login shell. A root-owned runtime directory (0750, dedicated
dashboard group) protects a 0660 broker socket. The broker accepts only typed
`status`, `start`, `stop`, `restart`, `worlds`, `world`, and `delete` requests,
not arbitrary commands/arguments. World mutations accept a validated save name;
deletion additionally requires an exact typed confirmation at the broker boundary.
Manual backup/update actions remain CLI-only through SSH.

HTTP binds to one explicit **RFC1918 LAN or loopback IPv4 address**, never a
wildcard/public address. The homelab uses `192.168.68.111:8787` and allows
`192.168.68.0/24`; reusable defaults stay loopback-only. The real TCP peer must
match the configured private CIDR allowlist (or loopback); forwarding headers
are ignored. Missing connection metadata fails closed. Host checks
reject DNS rebinding, mutations require a same-origin request plus a random token,
and CSP denies framing/inline scripts. This is not a public, multi-user auth
system: **anyone on the allowed LAN can control the game**, over unencrypted HTTP.
Use only a trusted LAN. Do not forward the port, publish it in Caddy, or use a
public proxy that rewrites public client addresses to trusted LAN addresses.

Operations share the existing filesystem lock; the broker reports busy rather
than queuing simultaneous browser operations. CLI operations can also hold the
lock. A failed/timed-out request is not proof that a game operation was canceled:
check status before retrying. A2S split/compressed or malformed packets return
unknown, never zero; monitoring uncertainty resets the idle countdown.

## Deployment and migration

Follow [dashboard deployment/access](../../docs/valheim.md#private-dashboard).
Ansible preserves existing `world.json`, saves, and manual archives. The Rust
controller reads the existing config/state shapes; Ansible adds `query_address`.
The next idle sample safely resets any incompatible countdown. Manual backup
archive layout remains `world.json`, `saves/`, `config/`, `docker-compose.yml`.
Old Python runtime code is removed only after the Rust binaries are installed.

The Home Tools shell redirects `/` to `/valheim`. It includes status/player count,
start/stop/restart, saved-world discovery, world selection/creation, and confirmed
deletion. There is no public authentication, backup UI, or extra reverse proxy.
The new-world form accepts an optional case-sensitive seed (1–10 letters/numbers).
Blank uses native random generation; existing worlds cannot be reseeded. See
[seeded world creation](docs/world-seeds.md) for the metadata format, safeguards,
and real-game acceptance steps. No mods or extra services are required.
`--allow-host lab.mathijs.se` explicitly allows that DNS name without relaxing
the real-peer network allowlist. `--trusted-proxy 172.18.0.2/32` separately trusts
that exact private proxy peer and its HTTPS forwarding scheme; Ansible discovers
these addresses from the configured Caddy container. Forwarded client addresses
are never trusted. See [private DNS and trusted HTTPS](../../docs/home-tools.md).
