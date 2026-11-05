# Homelab

Ansible provisions a Debian host named `homelab` and runs **Pi-hole, mathijs.se,
and Caddy** with Docker Compose. Caddy handles website HTTPS.

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
[Rust source and development](tools/valheim/README.md).

Originally inspired by [Saura Sahu's homelab](https://github.com/mrsauravsahu/kube-homelab).
