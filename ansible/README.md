# Run Ansible

Sets the hostname to `homelab`, installs Docker and Compose, and deploys Pi-hole,
mathijs.se, and Caddy to `/opt/homelab`.

You need a Debian host with SSH access, a sudo-capable user, and a reserved LAN
IP. Keep TCP 80/443 and TCP/UDP 53 available for the services.

## 1. Install Ansible locally

From the repository root:

```bash
python3 -m venv ~/.venvs/ansible
source ~/.venvs/ansible/bin/activate
python3 -m pip install ansible
cd ansible
ansible-galaxy collection install -r requirements.yml
```

Run the remaining commands from `ansible/`.

## 2. Set the target

**New installations only:**

```bash
cp inventory.example.ini inventory.ini
$EDITOR inventory.ini
```

Replace `YOUR_SSH_USER` and confirm `ansible_host`. Connect once with
`ssh YOUR_SSH_USER@192.168.68.111` to trust the host key.

| File | Setting |
| --- | --- |
| `inventory.ini` | `ansible_host`: SSH address; this file is gitignored. |
| `host_vars/homelab/main.yml` | `compose_bind_ip`: LAN address for service ports, currently `192.168.68.111`. |
| `roles/compose/defaults/main.yml` | Images, domains, timezone, and DNS defaults; override in the host variables. |

Keep the inventory alias `homelab` matched to its `host_vars/homelab/`
directory. The inventory group is `homelab_hosts` to avoid a host/group name
collision. The SSH address and service bind IP are configured independently.

### Existing installations: one-time inventory and Vault migration

The old inventory used host **`elitedesk`** in group **`homelab`**. The new
playbooks target group **`homelab_hosts`**, containing host **`homelab`**.
Your `inventory.ini` is gitignored: pulling the PR **does not migrate it**.
Do this once, before running either deployment or `scripts/valheim`.

1. From `ansible/`, back up your inventory and the encrypted Vault **before
   pulling**, if possible. Keep this terminal open for the remaining commands:

   ```bash
   migration_dir=$(mktemp -d "$HOME/homelab-migration.XXXXXX")
   cp -p inventory.ini "$migration_dir/inventory.ini"
   cp -p host_vars/elitedesk/vault.yml "$migration_dir/previous-vault.yml"
   cp -a host_vars/elitedesk "$migration_dir/host_vars-elitedesk"
   ```

   The backup directory is private; the Vault backup stays encrypted. Keep it
   until deployment is verified. If you have **already pulled**, create the
   backup directory and copy `inventory.ini` as above, then use your own saved
   pre-migration Vault. If you have no such backup, the PR's base version can be
   recovered without decrypting it:

   ```bash
   git show 24389cb95ebdc80adbdf2d1458ffdd98bdbd15a9:ansible/host_vars/elitedesk/vault.yml > "$migration_dir/previous-vault.yml"
   ```

   That Git copy cannot recover uncommitted/local secret changes. Use the actual
   previously deployed Vault when available; comparing against Git only verifies
   the secrets committed at that revision.

2. Pull/checkout the updated code, then edit your existing inventory **in place**:

   ```bash
   ${EDITOR:-vi} inventory.ini
   ```

   Make these exact name changes, keeping all connection values/options:

   | Old | New |
   | --- | --- |
   | `[homelab]` | `[homelab_hosts]` |
   | Host alias `elitedesk` at the start of its line | Host alias `homelab` |
   | `[homelab:vars]`, if present | `[homelab_hosts:vars]` |
   | `[homelab:children]`, if present | `[homelab_hosts:children]` |

   Update references to the old group in parent `:children` sections as well.
   The resulting host entry has this shape (retain **your existing** values):

   ```ini
   [homelab_hosts]
   homelab ansible_host=192.168.68.111 ansible_user=YOUR_SSH_USER ansible_become=true
   ```

   **Do not copy `inventory.example.ini` over your inventory.** Preserve your
   SSH/LAN/Tailscale address, user, key, port, and all privilege-escalation options,
   including `ansible_become_method=su` if configured. The alias change does not
   require changing the server's address or recreating SSH keys.

3. Check that `host_vars/homelab/main.yml` and `host_vars/homelab/vault.yml` exist.
   Ansible now loads variables under the **new host alias**, not `elitedesk`.
   Move any custom/gitignored host-variable files into `host_vars/homelab/` and
   merge your old `main.yml` overrides into the current file. Do not blindly
   overwrite the new `main.yml` or Vault with an old directory copy.
   If you maintain custom `group_vars/homelab.yml` or `group_vars/homelab/`, rename
   that group-variable file/directory to `homelab_hosts` too. Update saved
   `--limit elitedesk` commands to `--limit homelab`.

4. Verify your encrypted secrets before deploying:

   ```bash
   ansible-playbook -i localhost, verify-vault-migration.yml --ask-vault-pass \
     -e "previous_vault_file=$migration_dir/previous-vault.yml"
   ```

   This read-only, local playbook decrypts both files in memory and checks that
   **every previous key/value is unchanged**, including the Pi-hole and Valheim
   passwords when present in the old file. It hides secret values, writes no
   plaintext files, and never contacts or changes the server. Additional new
   keys are allowed, but cannot be verified against a file that never held them.
   If the files use different Vault encryption passwords, supply both through
   Ansible's standard `--vault-id previous@prompt --vault-id current@prompt`
   options instead of `--ask-vault-pass`.

   **Important:** a plain move of `vault.yml` preserves its encrypted bytes and
   needs neither re-encryption nor password recreation: Vault encryption is not
   tied to the directory or inventory alias. However, commit `623a10a`, included
   in this PR, changed the ciphertext as well as the path. Ciphertext alone
   cannot establish whether the secret values stayed the same; the comparison
   above verifies that using your Vault password(s).

   If comparison fails, **stop before deployment**. Retain both encrypted files
   and restore/merge the exact existing values with `ansible-vault edit
   host_vars/homelab/vault.yml`, preserving any additional current keys. Do not
   run `ansible-vault create`, generate replacement game/Pi-hole passwords, or
   blindly copy an older Vault over one containing newer secrets. A directory
   rename itself requires **no password rotation**.

5. Check the inventory match without connecting, then test SSH access:

   ```bash
   ansible-playbook site.yml --list-hosts --ask-vault-pass
   ansible-playbook valheim.yml --list-hosts --ask-vault-pass
   ansible homelab -m ping --ask-vault-pass --ask-become-pass
   ```

   Both host lists must show exactly your intended host, `homelab`, under the
   `homelab_hosts` target—not zero hosts or the old alias. Ping runs Ansible's
   connectivity check, not deployment. Once verification passes, skip the
   first-time password creation below and continue at **4. Deploy**.

## 3. Add the Pi-hole password

For first-time setup:

```bash
ansible-vault create host_vars/homelab/vault.yml
```

Enter this YAML in the editor with your chosen Pi-hole password:

```yaml
vault_compose_pihole_password: 'YOUR_PIHOLE_PASSWORD'
```

Commit the encrypted `vault.yml`; keep the Vault password in your password
manager. For later edits, use `ansible-vault edit host_vars/homelab/vault.yml`.

## 4. Deploy

The homelab now opts into a separately gated RAID 1 role. On an uninitialized
host, the full playbook stops before other roles until storage is deliberately
initialized. Review the exact HDD identifiers and destructive operations in
[NAS storage setup](../docs/storage.md) first. Use `--tags storage` for that work;
do not run a full deployment just to initialize the disks.

The host also enables trusted HTTPS for `lab.mathijs.se` and `pihole.mathijs.se`.
Before deployment, add the scoped Cloudflare token to the existing Vault using
the [private HTTPS setup](../docs/home-tools.md#cloudflare-certificate-setup-before-deployment).
Do not change router forwarding or create public A/AAAA records for these names.

```bash
ansible homelab -m ping --ask-vault-pass
ansible-playbook site.yml --ask-vault-pass
```

Add `--ask-become-pass` if sudo requires a password. Rerun the playbook to apply
configuration changes. In a new terminal, first run
`source ~/.venvs/ansible/bin/activate`.

The committed host settings also enable Valheim and its private dashboard. That
role builds Rust binaries locally: install Rust 1.88+ on your Linux x86_64 control
machine and run `rustup target add x86_64-unknown-linux-musl` once before deploying.
See [Valheim setup and dashboard access](../docs/valheim.md#private-dashboard).
Set `valheim_enabled: false` to retire an installed game, or disable only its UI
with `valheim_dashboard_enabled: false`.

## 5. Verify

```bash
ssh YOUR_SSH_USER@192.168.68.111 'sudo docker compose --project-directory /opt/homelab ps'
dig @192.168.68.111 example.com
curl -I --resolve pihole.mathijs.se:443:192.168.68.111 https://pihole.mathijs.se/admin/
```

Before switching public website traffic, complete the
[HTTPS checklist](../docs/compose.md#https-and-public-cutover).

### Docker networking after reboot (Debian/ifupdown Wi-Fi)

Read-only inspection and owner-provided diagnostics confirmed **ifupdown** with
`allow-hotplug wlp0s20f3` and `iface wlp0s20f3 inet dhcp`; NetworkManager/networkd
are inactive. Docker CE is `29.9.3`, Compose is `5.7.0`. Do not print the complete
interfaces file: it may contain the Wi-Fi password.

The November 6 boot journal confirms this sequence (seconds since boot):

| Time | Event |
| --- | --- |
| 4.24 | `ifup@wlp0s20f3.service` declared started (`Type=simple`), DHCP still running. |
| 4.88 | Native ifupdown waiter finished; Docker started. |
| 6.98–7.27 | Caddy/Pi-hole failed to bind `192.168.68.111:80` / `:53`: `cannot assign requested address`. |
| 15.24 | DHCP finally leased `192.168.68.111`. |

The original fix (enabling the packaged waiter in `ifup` mode) was insufficient:
`ifquery --state` can succeed while DHCP is still running. `networking.service`
only raises auto interfaces, not this hotplug Wi-Fi interface.

1. Docker restored `restart: unless-stopped` containers before the Wi-Fi LAN IP
   exists. Pi-hole and Caddy publish ports on that **specific** IP. Merely reaching
   `network-online.target` is insufficient without an effective readiness waiter.
2. A port bind failed with `cannot assign requested address`. Docker 29's reported
   endpoint-cleanup regression can persist an empty `NetworkSettings.Networks`.
   A subsequent start succeeds with only loopback; restarting dockerd cannot
   recreate the lost attachment. A local Pi-hole health check can still pass.

This matches [Moby #53928](https://github.com/moby/moby/issues/53928), marked
duplicate of [#51758](https://github.com/moby/moby/issues/51758). On **2026-11-06**,
the fix [#52480](https://github.com/moby/moby/pull/52480) is merged (November 2)
and included in [29.10.0-rc.1 release notes](https://github.com/moby/moby/releases/tag/docker-v29.10.0-rc.1).
However, the latest stable release, Debian 13 stable repository listing, and the
host's cached APT candidate remain **29.9.3**; no stable fixed release was found.
The alternative PR #53961 is still open. Recheck releases before proposing an
upgrade; this change installs neither a release candidate nor a downgrade and
does not change the installed Docker version. Compose's `depends_on` only
orders an explicit Compose deployment, not dockerd's reboot-time restoration.
The default Compose bridge network is correct; no host networking, DNS upstream
change, extra Compose boot service, or automatic reconnect is needed.

The host selects `docker_ifupdown_wait_online_interface: wlp0s20f3` and
`docker_ifupdown_wait_online_address: '{{ compose_bind_ip }}'`. Ansible checks the
stack, auto/hotplug configuration, and current assignment of that address. It
preserves the interfaces file and other networking defaults and retains the
native waiter. The correction is an **instance-only** drop-in for
`ifup@wlp0s20f3.service`:

```ini
[Service]
Type=oneshot
```

Systemd must now wait for `ifup` (including DHCP) to finish successfully, using
the existing five-minute service timeout. No other interface unit is changed.
Docker's drop-in requires and orders after that instance as well as the waiter:

```ini
[Unit]
Requires=ifupdown-wait-online.service ifup@wlp0s20f3.service
After=ifupdown-wait-online.service ifup@wlp0s20f3.service

[Service]
ExecCondition=/bin/sh -ec '/usr/sbin/ip -4 -o address show dev wlp0s20f3 scope global | /usr/bin/grep -Fq " inet 192.168.68.111/"'
```

`Requires` plus `After` prevents Docker starting if `ifup` fails. The final
`ExecCondition` uses existing OS utilities to check the **exact IPv4 address on
the selected interface**, not merely carrier, IPv6, a default route, or an
ifupdown state record. It runs once with no retry or delay. Exit 1 skips startup
without triggering Docker's existing `Restart=always` policy; a wrong DHCP lease
therefore cannot start containers and damage their saved networking. The skip
is visible in systemd status/journal and the verifier fails when Docker is down.
No custom executable, polling loop, watchdog, internet/DNS probe, or automatic
recovery is installed. Other networking stacks are not configured implicitly.

Systemd is reloaded only when either drop-in changes; **Docker/networking are not
restarted on apply**. The one-shot semantics take effect on the next start of the
Wi-Fi unit. This prevents the demonstrated trigger but **does not repair already
lost attachments** or patch Docker's underlying bug. After correcting a failed
lease, the owner must explicitly decide when to start Docker; there is no retry
agent.

#### Owner-run deployment and controlled reboot

Run these commands from `ansible/` only when ready. The targeted tag applies just
the readiness configuration: it does not run package installation, Compose,
Valheim, or any restart handler. Omit `--diff` to avoid exposing unrelated secrets.

```bash
ansible-playbook site.yml --tags docker-networking --check --ask-vault-pass --ask-become-pass
ansible-playbook site.yml --tags docker-networking --ask-vault-pass --ask-become-pass
ansible-playbook site.yml --tags docker-networking --ask-vault-pass --ask-become-pass
ansible-playbook verify-compose.yml --ask-vault-pass --ask-become-pass
```

The second apply should report no changes. The separate verifier is read-only
and does **not** run `compose up`. It first requires Docker already active so
its CLI queries cannot inadvertently start Docker through socket activation.
All three expected containers must be running
with a `homelab_default` network ID, endpoint ID, IPv4 address, and gateway, even
if reported healthy. Caddy must also publish TCP 80/443 and Pi-hole TCP/UDP 53
on exactly `compose_bind_ip`, with the intended host ports and no wildcard
binding for those ports. The verifier reads **actual** `NetworkSettings.Ports`,
not just the intended `HostConfig.PortBindings`. It cannot prove that firewall
rules or application traffic work; DNS and HTTPS acceptance checks remain needed.
Normal full deployments also run this check after Compose.
It intentionally excludes Valheim, which may be stopped by its idle controller.
No volumes, Caddy configuration/certificates, passwords, or saves are modified.

Before reboot, use a privileged shell on the host to confirm configuration and
inspect the current/previous boot for the initial error:

```bash
systemctl is-enabled ifupdown-wait-online.service
systemctl cat docker.service
systemctl cat ifup@wlp0s20f3.service
ip -4 address show dev wlp0s20f3
ifquery --list --allow=auto
ifquery --list --allow=hotplug
ifquery --state wlp0s20f3
journalctl -b -o short-monotonic -u docker.service -u networking.service -u ifup@wlp0s20f3.service --no-pager
# If the failed boot is older and persistent journals exist:
journalctl -b -1 -u docker.service --no-pager
```

Look for port-bind failures, `cannot assign requested address`, endpoint errors,
or conflicting ports. The old nftables table-deletion messages alone do not prove
a bridge failure; the explicit bind errors and DHCP timestamps establish this race.
If the verifier fails, retain diagnostics and decide on manual recovery; do not
delete networks, remove volumes, or force-recreate the whole stack.

Schedule a maintenance window (including Valheim players), have local-console
access, record the successful baseline verifier/DNS/HTTPS results, then manually
reboot the host from that privileged shell with `systemctl reboot`. After SSH
returns, **before** running any deployment or repair:

1. Run `verify-compose.yml` again; require all attachment checks to pass.
2. Inspect `journalctl -b -u ifupdown-wait-online.service -u docker.service` and
   `systemd-analyze critical-chain docker.service`. Confirm the **Wi-Fi ifup
   process completed and DHCP assigned the intended IPv4 address before Docker
   started**, not merely that the waiter finished. There must be no bind/endpoint
   failures or skipped Docker address condition.
3. Repeat the DNS and HTTPS checks above, including Home Tools/public website as
   appropriate. Confirm Pi-hole data and Caddy certificates remain intact and
   other services (including expected Valheim state) are unchanged.

A local syntax/regression pass is not reboot acceptance. No reboot or deployment
is performed by the local tests or the read-only verifier.
