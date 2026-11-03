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

```bash
cp host_vars/elitedesk.yml.example host_vars/elitedesk.yml
chmod 600 host_vars/elitedesk.yml
$EDITOR host_vars/elitedesk.yml
ansible-vault encrypt host_vars/elitedesk.yml
```

Set `compose_pihole_password` to a strong password before encrypting the file.
Ansible loads `host_vars/elitedesk.yml` automatically for the inventory alias
`elitedesk`. For another inventory alias, name the file to match it. These local
YAML files are gitignored; the example is safe to commit. Later edits use
`ansible-vault edit host_vars/elitedesk.yml`. Keep the Vault password out of Git.

Non-secret defaults (images, timezone, domains, upstream DNS) are in
`roles/compose/defaults/main.yml`. Override them in `host_vars` for one machine,
or `group_vars/all.yml` for all machines. The bind IP comes from `ansible_host`,
and the default Pi-hole domain follows it automatically.

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

If you choose not to encrypt your local host variables, omit `--ask-vault-pass`.

Check mode is only a preview: on a fresh host, tasks depending on a newly
created apt key/repository or installed packages may fail because check mode
does not create them. Compose startup is skipped entirely in check mode.

## Verify

```bash
ssh YOUR_SSH_USER@192.168.68.111 'sudo docker compose --project-directory /opt/homelab ps'
curl --resolve mathijs.se:80:192.168.68.111 http://mathijs.se/
curl -I http://dns.192.168.68.111.nip.io/admin/
dig @192.168.68.111 example.com
```

The stack's startup wait checks container running/health status where available;
it does not replace the application and DNS checks above.

## Existing hosts and safety

- Use a fresh Debian installation. Existing distro Docker/containerd packages
  may conflict with Docker's packages; inspect and resolve these manually.
- The playbook refuses a host with a k3s server service file. It never runs the
  k3s uninstaller or deletes Kubernetes data.
- There are no automatic reboots, router changes, firewall changes, or user
  additions to the root-equivalent `docker` group.
- Reserve the EliteDesk IP in your router. Check ports TCP 80 and TCP/UDP 53
  are free before deploying (`sudo ss -lntup`). A DNS service bound on all host
  interfaces can conflict with Pi-hole; resolve that deliberately, not by
  blindly disabling the host resolver.
- Back up and restore application data separately before switching traffic.
