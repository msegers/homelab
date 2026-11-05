# Private OpenCode V2 workspace

This is an **opt-in deployment**, separate from `ansible/site.yml`. Preparing or
pulling this repository does not install or start it. Nothing here changes
Valheim, Pi-hole, Caddy, or the website.

## Isolation and persistence

OpenCode runs as UID/GID **10001**, without sudo, Linux capabilities, host
networking, privileged mode, a Docker socket, or host SSH credentials. Its own
Compose project has a separate bridge network. It can access the internet for
models, Git, and dependencies; this is **not** network isolation from your LAN
or a VM-strength security boundary.

Only these host directories are mounted read/write:

| Host path (default) | Container path | Contents |
| --- | --- | --- |
| `/var/lib/homelab-opencode/workspace` | `/workspace` | Cloned projects, build outputs, scratch files placed here |
| `/var/lib/homelab-opencode/config` | `/home/opencode/.config` | Settings and integrations |
| `/var/lib/homelab-opencode/data` | `/home/opencode/.local/share` | Sessions, provider credentials, snapshots, worktrees, logs |
| `/var/lib/homelab-opencode/state` | `/home/opencode/.local/state` | Application state |

Everything else writable, including `/tmp`, home-directory experiments and
caches, lives in the disposable container layer. **Restarting does not clean
it; recreating does.** Repositories, build outputs, and scratch files deliberately
written to persistent storage survive recreation too. No existing host checkout
is mounted, so editing a clone cannot change the deployed homelab configuration.

The image includes Git, Bash, Rust/Cargo/Clippy/rustfmt, C build tools, OpenSSL
development headers, Node/npm, and an SSH client. Add further system dependencies
to the Dockerfile and redeploy; the agent cannot install root-owned packages.
User-space tools can be installed inside its writable home and disappear on
recreation unless deliberately stored in the workspace.

Defaults cap the **whole container**, including builds and language servers, at
4 GiB RAM, no extra swap, two CPUs, and 512 processes. These are ceilings, not
reservations. Cargo defaults to two build jobs. Heavy builds can hit the limit
and be killed. Host disk use is **not** capped: monitor workspace/build outputs,
caches, image layers and session storage. Docker logs rotate at 3 × 10 MB.

## Deployment (only when explicitly approved)

Docker and the Compose plugin must already be installed by the existing Ansible
setup. Add a new, unique password (24+ characters) to the existing encrypted
`ansible/host_vars/homelab/vault.yml` using `ansible-vault edit`; preserve all
existing secrets:

```yaml
vault_opencode_server_password: "YOUR_UNIQUE_LONG_PASSWORD"
```

Optional overrides belong in `host_vars/homelab/main.yml`; see
`roles/opencode/defaults/main.yml`. If raising the RAM limit, also raise
`opencode_memory_swap_limit` to the same value to continue disabling extra swap.
The host port is deliberately bound to **127.0.0.1 only**. No public proxy route,
remote tunnel, router forwarding, or LAN listener is configured.

From `ansible/`, deploying **only OpenCode** is:

```sh
ansible-playbook opencode.yml --ask-vault-pass
```

Ansible owns image building, configuration, and Compose lifecycle. The container
uses Docker's restart policy to survive reboot; there is no competing OpenCode
background daemon or extra systemd unit. Upstream OpenCode is pinned to V2.1.4
and its image digest. Alpine development packages resolve at build time; they
are not a fully locked toolchain. Global update checks are disabled initially;
update the image pin through Ansible instead of self-updating in the container.

## Connect from another computer

Keep this SSH tunnel running on the client device:

```sh
ssh -N -L 4096:127.0.0.1:4096 skoltr@homelab
```

Open **http://127.0.0.1:4096** and authenticate with the configured password
(HTTP Basic username, if requested: `opencode`). A compatible V2 terminal client
can connect through the same tunnel:

```sh
opencode --server http://127.0.0.1:4096
```

The client uses `OPENCODE_PASSWORD` for the server password; enter it locally
without placing it in shell history. Do not run `opencode pair --remote`: that
enables internet exposure and is not part of this setup. Browser/phone access
without SSH requires a separately approved private LAN/VPN access design.

In OpenCode, use `/connect` to connect OpenAI (ChatGPT sign-in or an API key),
or another provider, then `/models`. Provider credentials stay in the server's
persistent database. Treat backups of the data directory as secrets. Git
credentials are separate: do not mount your whole `.ssh` directory or forward
an SSH agent as a shortcut. Start with public clones; arrange narrowly scoped
repository credentials separately if private access/push is needed.

Clone projects under `/workspace` using OpenCode's terminal. Cloud models run
off-host; compilation and other tools run inside the container.

## Recreate safely

Recreation interrupts active sessions/tools, so do it only when deliberately
requested. With the configured defaults, use this Ansible ad-hoc lifecycle
operation from `ansible/`:

```sh
ansible homelab_hosts --become --ask-vault-pass \
  -m community.docker.docker_compose_v2 \
  -a 'project_src=/opt/homelab-opencode project_name=homelab-opencode state=present recreate=always build=never'
```

This replaces the disposable container layer and retains all four persistent
directories. **Do not delete storage to clean temporary files.** Anything under
`/workspace` or the other persistent mounts must be cleaned separately and
selectively. Rebuilding images can also leave old Docker image/build-cache
layers; avoid broad Docker pruning, which could affect other services.

## Verification

Local automated checks validate Ansible rendering, Compose configuration,
password escaping, loopback binding, and the storage/privilege boundaries.
They do not deploy to the homelab. After an approved deployment, separately
verify authenticated browser/TUI access through SSH, provider sign-in, a build,
resource usage during gameplay, and persistence across recreation. Do not test
recreation against a live session without permission.
