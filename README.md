# Homelab

Ansible provisions a Debian host named `homelab` and runs **Pi-hole, mathijs.se,
and Caddy** with Docker Compose. Caddy handles website HTTPS.

Ansible is the only supported provisioning path. The retired Terraform, Helm,
k3s, and Flux configuration is available in Git history; removing it does not
uninstall anything on existing hosts. Back up and manually retire any old k3s
installation before provisioning with Ansible.

## Run

First time? Follow the [setup instructions](ansible/README.md).
Upgrading an existing installation? Complete the
[one-time inventory and Vault migration](ansible/README.md#existing-installations-one-time-inventory-and-vault-migration)
before deployment; your gitignored inventory is not updated by pulling changes.
Once configured, deploy or apply changes from the repository root:

```bash
source ~/.venvs/ansible/bin/activate
cd ansible
ansible-playbook site.yml --ask-vault-pass
```

[Service URLs, HTTPS setup, and maintenance](docs/compose.md).

[Optional Valheim server and control scripts](docs/valheim.md).
[Private Valheim dashboard](docs/valheim.md#private-dashboard) ·
[Homelab statistics, Home Tools and LAN DNS](docs/home-tools.md) ·
[Rust source and development](tools/homelab/README.md).

[RAID-backed private SMB, Immich, backups and deployment](docs/storage-services.md).

Originally inspired by [Saura Sahu's homelab](https://github.com/mrsauravsahu/kube-homelab).
