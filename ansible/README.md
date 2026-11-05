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
curl -I http://dns.192.168.68.111.nip.io/admin/
```

Before switching public website traffic, complete the
[HTTPS checklist](../docs/compose.md#https-and-public-cutover).
