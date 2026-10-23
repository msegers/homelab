# Ansible provisioning

This directory provisions a fresh Raspberry Pi OS Lite (64-bit) host for the
existing single-node k3s cluster. It deliberately covers only the machine and
k3s bootstrap. Application state, secrets, Flux, and persistent data are
restored separately.

The Terraform under `../k3s` and `../apps` is retained as legacy reference and
is not used by this playbook.

> This playbook is intended for a fresh host. The k3s install is guarded by the
> presence of `/etc/systemd/system/k3s.service`; if k3s is already installed,
> the playbook will not replace Docker with containerd or change existing k3s
> flags.

## What the playbook does

- Installs the small set of base packages needed by k3s.
- Sets the hostname and a matching `/etc/hosts` entry.
- Adds `cgroup_memory=1 cgroup_enable=memory` to the Raspberry Pi kernel command
  line, and reboots once if that changed.
- Installs a pinned k3s version using the official install script. There is no
  native k3s Ansible module, so the script is guarded with `creates` and never
  upgrades an existing installation.
- Uses k3s's default containerd runtime. Docker is not installed and `--docker`
  is not passed.
- Preserves the original cluster networking flags: `--disable=traefik`,
  `--write-kubeconfig-mode`, `--tls-san`, `--bind-address`,
  `--advertise-address`, and `--node-ip`.
- Rewrites the kubeconfig server from `127.0.0.1` to the node IP, as the old
  Terraform did.
- Enables and starts `k3s`, then waits for the node to report `Ready`.

## What remains manual

- Flashing the Raspberry Pi OS image and enabling SSH.
- Flux bootstrap and GitHub authentication.
- Kubernetes Secrets, including anything needed by Helm releases and GHCR.
- The checked-in Pi-hole values contain a literal `adminPassword`; replace it
  with an existing Secret rather than restoring that value as-is.
- Persistent volumes and other application data.
- The legacy Helm releases (`ingress-nginx`, Hajimari, and Pi-hole). They are not
  recreated by this playbook or by the currently checked-in Flux manifests.
- Kubernetes and k3s upgrades.
- Uninstalling k3s. The old Terraform had a destroy-time `k3s-uninstall.sh`
  hook; Ansible does not perform destructive actions.

---

## Prerequisites

On the machine running Ansible:

- Ansible installed, for example:

  ```bash
  python3 -m venv ~/.venvs/ansible
  source ~/.venvs/ansible/bin/activate
  python3 -m pip install ansible
  ```

- SSH access to the Pi, with a key or agent. The Pi should already be on the
  network and have SSH enabled.
- Connect once with `ssh` so the Pi's host key is trusted by the control machine.
- A Raspberry Pi OS Lite 64-bit installation. The example inventory assumes a
  `pi` user with sudo. If you use direct root SSH, adjust the inventory.
- On an older Raspberry Pi OS release that still uses `/boot/cmdline.txt`,
  override `common_cmdline_path` in `group_vars/all.yml`.

## Inventory

```bash
cd ansible
cp inventory.example.ini inventory.ini
$EDITOR inventory.ini
```

`inventory.ini` is gitignored. The inventory values are:

| Variable | Purpose |
| --- | --- |
| `ansible_host` | Address Ansible uses to reach the Pi. |
| `ansible_user` | SSH user. |
| `ansible_become` | Set to `true` when using a non-root user with sudo. |
| `k3s_node_ip` | The Pi's LAN IP. Must be an IP, not a DNS name. |
| `pi_hostname` | Hostname the k3s node will use. Keep `segers` if the existing Pi-hole node affinity should keep matching. |

The k3s settings live in `group_vars/all.yml`:

| Variable | Purpose |
| --- | --- |
| `k3s_version` | Pinned k3s version. |
| `k3s_extra_tls_sans` | Optional extra API certificate SANs. |
| `k3s_kubeconfig_mode` | Kubeconfig permissions. The default `0644` preserves the previous behaviour. |

Role defaults are in `roles/*/defaults/main.yml`.

## Run

Check connectivity first:

```bash
ansible -i inventory.ini pi -m ping
```

Review the planned changes without applying them:

```bash
ansible-playbook -i inventory.ini site.yml --check --diff
```

Apply:

```bash
ansible-playbook -i inventory.ini site.yml
```

A real run may reboot the Pi once if the kernel command line changed. The
playbook waits for the Pi to come back and then installs k3s.

> The old Terraform installation already uses `v1.33.0+k3s1`. The older version
> in `k3s/locals.tf` was not used by the install command. Change
> `k3s_version` deliberately; the playbook does not perform upgrades.

## Verify

```bash
ssh pi@192.168.68.113 'sudo systemctl status k3s --no-pager'
ssh pi@192.168.68.113 'sudo k3s kubectl get nodes -o wide'
ssh pi@192.168.68.113 'sudo k3s kubectl get pods -A'
```

Copy the kubeconfig to the machine you will use for Flux and `kubectl`:

```bash
mkdir -p ~/.kube
ssh pi@192.168.68.113 'sudo cat /etc/rancher/k3s/k3s.yaml' > ~/.kube/config
chmod 600 ~/.kube/config
kubectl get nodes
```

The kubeconfig already points at the node IP, and that IP is included in the
API certificate SANs. If you need to reach the API by DNS name, add it to
`k3s_extra_tls_sans` before provisioning.

---

## Flux restoration

Flux is not installed by Ansible. Bootstrap it after k3s is healthy, from the
machine holding the kubeconfig.

Install the Flux CLI and authenticate to GitHub. The token must not be committed
to this repository:

```bash
export GITHUB_TOKEN='...'
flux bootstrap github \
  --owner=msegers \
  --repository=homelab \
  --branch=main \
  --path=flux \
  --personal
```

`flux bootstrap github` creates a deploy key by default and stores its private
half in a Kubernetes Secret in the `flux-system` namespace. The public half is
registered as a GitHub deploy key with write access.

### If the old deploy key is lost

The private key is not recoverable from GitHub; GitHub only stores the public
key. After reinstalling the cluster, re-run the bootstrap command and add the
newly generated public key to the repository's deploy keys, replacing the old
one if necessary. If you backed up the `flux-system` Secret from the old
cluster, restore that instead.

Using `--token-auth` with a GitHub token is an alternative. The token is then
stored in the cluster, so treat cluster Secret backups as sensitive either way.

Verify Flux:

```bash
flux check
flux get sources git -A
flux get kustomizations -A
kubectl -n flux-system get gitrepositories
```

### Existing Flux configuration to check manually

These files were intentionally not changed:

- `flux/apps/mathijs-se/image-automation.yaml` references a GitRepository named
  `homelab`. Confirm that the GitRepository created or restored for this repo
  uses that name; a default Flux bootstrap often names it `flux-system`.
- `flux/apps/mathijs-se/helmrelease.yaml` references a GitRepository named
  `mathijs-se` for the chart source. That GitRepository is not defined in this
  repository, so restore or recreate it separately.
- `flux/apps/mathijs-se/images.yaml` expects tags matching
  `^0\.0\.[0-9]+$`. Make sure the website build publishes tags that match. In
  particular, `sha-...` tags will not satisfy that policy.
- There is no root `flux/kustomization.yaml` in this checkout. Confirm the path
  used by the Flux root Kustomization during restoration; the existing manifests
  may need to be wired up manually.

---

## Fresh install vs. restoring an existing cluster

Provisioning a fresh host with Ansible gives you an empty k3s cluster. It does
not restore the old cluster.

### Fresh cluster

1. Flash Raspberry Pi OS Lite 64-bit, enable SSH, and configure networking.
2. Set the desired hostname and IP in `inventory.ini`.
3. Run the playbook.
4. Bootstrap Flux.
5. Apply or restore the application layer.

### Restoring the previous cluster

In addition to the fresh-cluster steps:

- Restore Kubernetes Secrets from a backup, or recreate them. They are not
  stored in this repository.
- Restore persistent data before workloads need it. k3s local-path volumes
  normally live under `/var/lib/rancher/k3s/storage` on the Pi. Back that up
  before rebuilding.
- Restore or recreate the legacy Helm releases. The Terraform in `apps/` is a
  reference, not an Ansible-managed deployment.
- Restore any GHCR pull secret if the website image is private.
- Confirm the Pi-hole and Hajimari configuration. Their checked-in values
  disable persistence, but verify the actual cluster before assuming there is no
  data to restore.

Check what actually exists before a rebuild:

```bash
kubectl get pvc -A
kubectl get secrets -A
helm list -A
```

K3s upgrades are intentionally not automated. Upgrade manually when you are
ready, and only after taking a backup.
