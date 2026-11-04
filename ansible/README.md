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

```bash
cp inventory.example.ini inventory.ini
$EDITOR inventory.ini
```

Replace `YOUR_SSH_USER` and confirm `ansible_host`. Connect once with
`ssh YOUR_SSH_USER@192.168.68.111` to trust the host key.

| File | Setting |
| --- | --- |
| `inventory.ini` | `ansible_host`: SSH address; this file is gitignored. |
| `host_vars/elitedesk/main.yml` | `compose_bind_ip`: LAN address for service ports, currently `192.168.68.111`. |
| `roles/compose/defaults/main.yml` | Images, domains, timezone, and DNS defaults; override in the host variables. |

Keep the inventory alias `elitedesk` matched to its `host_vars/elitedesk/`
directory. The SSH address and service bind IP are configured independently.

## 3. Add the Pi-hole password

For first-time setup:

```bash
ansible-vault create host_vars/elitedesk/vault.yml
```

Enter this YAML in the editor with your chosen Pi-hole password:

```yaml
vault_compose_pihole_password: 'YOUR_PIHOLE_PASSWORD'
```

Commit the encrypted `vault.yml`; keep the Vault password in your password
manager. For later edits, use `ansible-vault edit host_vars/elitedesk/vault.yml`.

## 4. Deploy

```bash
ansible homelab -m ping --ask-vault-pass
ansible-playbook site.yml --ask-vault-pass
```

Add `--ask-become-pass` if sudo requires a password. Rerun the playbook to apply
configuration changes. In a new terminal, first run
`source ~/.venvs/ansible/bin/activate`.

## 5. Verify

```bash
ssh YOUR_SSH_USER@192.168.68.111 'sudo docker compose --project-directory /opt/homelab ps'
dig @192.168.68.111 example.com
curl -I http://dns.192.168.68.111.nip.io/admin/
```

Before switching public website traffic, complete the
[HTTPS checklist](../docs/compose.md#https-and-public-cutover).
