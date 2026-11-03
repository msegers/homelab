# Debian + Docker Compose provisioning

The playbook targets the `homelab` inventory group. It configures a Debian
host's hostname, installs Docker Engine and the Compose v2 plugin from Docker's
official repository, and renders the Compose role's template to `/opt/homelab`.

It no longer installs k3s, changes Raspberry Pi boot settings, or installs Flux.
The old k3s role, Terraform, and Flux files remain as reference only.

## Configure the target

On the control machine:

```bash
python3 -m venv ~/.venvs/ansible
source ~/.venvs/ansible/bin/activate
python3 -m pip install ansible
cd ansible
ansible-galaxy collection install -r requirements.yml
cp inventory.example.ini inventory.ini
$EDITOR inventory.ini
```

`inventory.ini` is gitignored and holds the SSH connection settings.
The example targets the EliteDesk
at **192.168.68.111** (assuming the same subnet as the old Pi). Replace
`YOUR_SSH_USER` with your Debian user's name. The user must have sudo access.
Trust the new host's SSH key by connecting manually first.

To target a different machine, change `ansible_host` in this file. To manage
multiple hosts, add inventory entries and select one with `--limit elitedesk`.
Set `homelab_hostname` per host if using multiple hosts; its default is
`homelab` in `group_vars/all.yml`.

## Configure applications

Host variables are split by purpose:

```text
host_vars/elitedesk/
├── main.yml    # Ordinary settings and references to Vault variables
└── vault.yml   # Encrypted secrets; create locally, then commit encrypted
```

`main.yml` is already committed. Create the secrets file using your own Vault
password (store that password in your password manager, never in Git):

```bash
ansible-vault create host_vars/elitedesk/vault.yml
```

In the editor, enter this YAML with a strong, unique Pi-hole password:

```yaml
vault_compose_pihole_password: 'YOUR_PIHOLE_PASSWORD'
```

Save and exit. Vault encrypts the file on disk; the Vault password and Pi-hole
password are separate secrets. Confirm the file's first line starts with
`$ANSIBLE_VAULT;` before adding it to Git:

```bash
head -n 1 host_vars/elitedesk/vault.yml
git add host_vars/elitedesk/vault.yml
git commit -m "chore: add encrypted EliteDesk secrets"
```

Both files are intended to be committed. Never commit plaintext secrets or a
Vault password file. Edit secrets with
`ansible-vault edit host_vars/elitedesk/vault.yml`, not by decrypting the file
in place. The encrypted file is not supplied by this repository until you create
it with your own secrets and password.

Ansible loads both YAML files automatically for inventory alias `elitedesk`.
For another alias, name the directory to match it. If migrating an existing
`host_vars/elitedesk.yml`, move its settings/secrets into this directory layout
and remove the old file; don't keep both forms for the same host.

Non-secret defaults (images, timezone, domains, upstream DNS) are in
`roles/compose/defaults/main.yml`. Override them in `host_vars/elitedesk/main.yml` for one machine,
or `group_vars/all.yml` for all machines.

The two addresses have different purposes:

| Variable | Purpose |
| --- | --- |
| `ansible_host` (inventory) | SSH endpoint; may be a DNS name, VPN address, or management IP. |
| `compose_bind_ip` (`host_vars/elitedesk/main.yml`) | LAN IPv4 address assigned to the target, used for published HTTP/HTTPS and DNS ports. |

They may both be `192.168.68.111`, but are configured independently. Changing
the SSH endpoint does not change service exposure. The role verifies that
`compose_bind_ip` is assigned to the target; the default Pi-hole domain follows
this bind IP.

There is no `.env` file to maintain. Ansible renders the Compose YAML, including
the password, with root-only permissions and suppresses its contents in task
output. Vault protects secrets in the source file, not inside a running
container: root and users with Docker access can still inspect them.

See [`../docs/compose.md`](../docs/compose.md) for routing, data migration,
private images, and operational limitations.

## Run

From `ansible/`:

```bash
ansible homelab -m ping --ask-vault-pass
ansible-playbook site.yml --syntax-check --ask-vault-pass
ansible-playbook site.yml --check --diff --ask-vault-pass
ansible-playbook site.yml --ask-vault-pass
```

Add `--ask-become-pass` if sudo requires a password. No command is run against
the old Pi unless you explicitly put it in the inventory.

The application role requires the Vault secret before it can deploy. A syntax
check alone can run without `--ask-vault-pass` before `vault.yml` is created.

Check mode is only a preview: on a fresh host, tasks depending on a newly
created apt key/repository or installed packages may fail because check mode
does not create them. Compose startup is skipped entirely in check mode.

## Verify

```bash
ssh YOUR_SSH_USER@192.168.68.111 'sudo docker compose --project-directory /opt/homelab ps'
curl -I --resolve mathijs.se:80:192.168.68.111 http://mathijs.se/
curl --resolve mathijs.se:443:192.168.68.111 https://mathijs.se/
curl --resolve www.mathijs.se:443:192.168.68.111 https://www.mathijs.se/
curl -I http://dns.192.168.68.111.nip.io/admin/
dig @192.168.68.111 example.com
```

The website's HTTP request should redirect to HTTPS. HTTPS checks require Caddy
to have obtained publicly trusted certificates first; see the [HTTPS cutover
requirements](../docs/compose.md#https-and-public-cutover). Do not bypass
certificate verification with `curl -k` when accepting the migration.

The stack's startup wait checks container running/health status where available;
it does not verify certificate issuance or replace the checks above.

## Existing hosts and safety

- Use a fresh Debian installation. Existing distro Docker/containerd packages
  may conflict with Docker's packages; inspect and resolve these manually.
- The playbook refuses a host with a k3s server service file. It never runs the
  k3s uninstaller or deletes Kubernetes data.
- There are no automatic reboots, router changes, firewall changes, or user
  additions to the root-equivalent `docker` group.
- Reserve the EliteDesk IP in your router. Check ports TCP 80/443 and TCP/UDP 53
  are free before deploying (`sudo ss -lntup`). A DNS service bound on all host
  interfaces can conflict with Pi-hole; resolve that deliberately, not by
  blindly disabling the host resolver.
- Back up and restore application data separately before switching traffic.
