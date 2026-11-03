# Homelab Compose stack

Deploy with [Ansible](../ansible/README.md). The Compose role renders
`ansible/roles/compose/templates/compose.yaml.j2` using role defaults and
inventory variables, and installs it alongside the role's Caddyfile in
`/opt/homelab`. No `.env` file is used.

## Services

| Service | Access (example configuration) |
| --- | --- |
| mathijs.se | `http://mathijs.se` and `http://www.mathijs.se` |
| Pi-hole admin | `http://dns.192.168.68.111.nip.io/admin/` |
| Pi-hole DNS | `192.168.68.111:53` TCP and UDP |

Caddy replaces ingress-nginx. Only its HTTP port and Pi-hole's DNS ports are
published, bound to `compose_host_ip` (default: `ansible_host`) rather than every
host interface. The website is
expected to listen on container port 80, matching the existing Helm service;
verify this against the actual image before cutover.

No Docker socket is mounted into containers.

Pi-hole uses **v6** environment variables and a persistent `/etc/pihole` volume.
DHCP is deliberately not enabled or exposed. If the old Pi-hole supplied DHCP,
plan that separately; a bridged Compose service cannot directly replace a LAN
DHCP server without extra networking configuration.

## DNS and exposure

The `nip.io` names resolve to the example LAN IP without editing public DNS.
Some routers/resolvers block private-IP wildcard DNS (DNS rebinding protection).
In that case use local DNS/hosts entries instead. Set your LAN DNS or hosts file
to resolve `mathijs.se` and `www.mathijs.se` to the EliteDesk when testing; merely
setting the Ansible variables does not change DNS or router port forwarding.

This is an **HTTP-only baseline**, like the checked-in ingress configuration.
It does not provision certificates or TLS. Caddy restricts Pi-hole
admin routes to private source addresses, but this is not authentication.
Do not expose the admin interface or DNS port 53 to the Internet. Docker port
publishing can bypass some host firewall rules; inspect Docker-aware firewall
rules and router forwarding. If another proxy/router rewrites client addresses,
the private-address check is not a sufficient security boundary.

Before exposing the website publicly, configure HTTPS deliberately (including
port 443 and certificate issuance), and review the admin routes. Do not send
admin passwords over untrusted HTTP networks.

## Images and updates

The website defaults to `ghcr.io/msegers/mathijs.se:0.0.5`, from the checked-in
Flux release. Confirm the image/tag is available for the EliteDesk's architecture.
If private, authenticate **on the target as root**, because Ansible runs Compose
with sudo:

```bash
printf '%s' "$GHCR_TOKEN" | sudo docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

Keep the token out of the repository. You may need to install Docker first;
the first deployment can then be rerun after login.

Pin the `compose_*_image` variables to tested tags/digests for reproducible deployments.
The defaults use moving Pi-hole/Caddy tags; an Ansible rerun only pulls
missing images. There is no Flux image automation or automatic update service.
For a deliberate update on the target, back up first, then:

```bash
cd /opt/homelab
sudo docker compose pull
sudo docker compose up -d --wait
sudo docker compose ps
sudo docker compose logs --tail=100
```

For configuration changes, edit the Ansible variables/templates and rerun Ansible. Direct target
edits will be overwritten. Ansible restarts the proxy when its configuration
files change so bind-mounted files are reopened. When using
Compose manually, run `sudo docker compose restart proxy` after these
file edits. Container environment/image changes trigger recreation normally.

## Data migration and cutover

This migrates deployment definitions, **not live Kubernetes state**.

1. Back up the old cluster's Pi-hole data/settings and any persistent website
   data before removing anything. The old values disabled persistence, but
   check the running cluster rather than assuming it has no state.
2. Provision the EliteDesk alongside the Pi. Nothing automatically uninstalls
   k3s on either host or changes your router.
3. Export/import Pi-hole settings using Teleporter. Check compatibility when
   moving from an old Pi-hole version to v6. Do not blindly copy old v5 runtime
   files over a v6 installation. The website manifests show no persistent mounts;
   inspect the actual deployment for anything else that needs restoring.
4. Verify website responses, admin login, restored settings, and DNS using the
   commands in the Ansible README.
5. Change LAN DNS/router clients to `192.168.68.111` and adjust website DNS/port
   forwarding only when ready. The Ansible variables do not configure clients.
6. Keep the old Pi available for rollback until the new services are proven.
   Only then manually retire the old k3s/Flux deployment.

Named volumes persist across container recreation and `docker compose down`.
Never use `down -v` unless you intend to destroy data. Back up the Pi-hole volume
and encrypted host variables securely; a Teleporter export is also useful for
portable restoration. The generated Compose YAML contains the password, so
protect any backups of that file too.
