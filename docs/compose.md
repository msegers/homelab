# Homelab Compose stack

Deploy with [Ansible](../ansible/README.md). The Compose role renders
`ansible/roles/compose/templates/compose.yaml.j2` using role defaults and
inventory variables, and installs it alongside the role's Caddyfile in
`/opt/homelab`. No `.env` file is used.

## Services

| Service | Access (example configuration) |
| --- | --- |
| mathijs.se | `https://mathijs.se` and `https://www.mathijs.se` (HTTP redirects to HTTPS) |
| Pi-hole admin | `http://dns.192.168.68.111.nip.io/admin/` |
| Pi-hole DNS | `192.168.68.111:53` TCP and UDP |

Caddy replaces ingress-nginx. Only its HTTP/HTTPS ports (TCP 80/443) and Pi-hole's
DNS ports (TCP/UDP 53) are published, bound to the explicit `compose_bind_ip`
host variable rather than every host interface. This LAN IPv4 address is
independent of `ansible_host`, which is only the SSH connection endpoint and
may be a DNS name or VPN/management address. The website is
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

Caddy restricts Pi-hole
admin routes to private source addresses, but this is not authentication.
Do not expose the admin interface or DNS port 53 to the Internet. Docker port
publishing can bypass some host firewall rules; inspect Docker-aware firewall
rules and router forwarding. If another proxy/router rewrites client addresses,
the private-address check is not a sufficient security boundary.

Pi-hole admin remains HTTP-only on the LAN hostname; it is not covered by the
website's public certificates. Do not send admin passwords over untrusted
networks; use a trusted LAN or an SSH tunnel.

## HTTPS and public cutover

Caddy's automatic HTTPS is enabled for both website hostnames. It obtains and
renews publicly trusted certificates and redirects website HTTP requests to
HTTPS. Certificate/account state persists in `caddy_data`; keep this volume
across updates and include it in secure backups. There is no self-signed
fallback for the public website.

Before accepting public traffic on the EliteDesk:

1. Verify both website domains are under your control and resolve publicly to
   your router's WAN IP (or the host's public IP if not behind NAT). Check A and
   AAAA records; remove stale IPv6 records if IPv6 is not routed to this server.
   This stack only publishes on the configured IPv4 bind address.
2. Arrange TCP 80 and 443 forwarding to `compose_bind_ip` and allow these ports
   through the relevant firewalls. Caddy needs public ACME challenge access and
   outbound HTTPS to obtain certificates. Do not forward DNS port 53. Check
   any existing CAA records permit Caddy's certificate issuer.
3. Coordinate a cutover/maintenance window: the old and new hosts cannot both
   receive the same router port forwarding. Private `curl --resolve` or hosts
   entries do not satisfy public ACME validation. Issuing certificates before
   changing forwarding requires a separately configured DNS challenge (the
   stock image does not include DNS-provider plugins).
4. Start the stack, inspect `sudo docker compose logs --tail=100 proxy` in
   `/opt/homelab`, and confirm certificate issuance for **both** domains.
5. Run the HTTPS verification commands in the Ansible README without `-k`.
   Also test from outside the LAN: both sites must have a valid certificate,
   serve the intended website, and redirect HTTP to HTTPS. Confirm Pi-hole
   admin remains inaccessible externally, including when its hostname is sent
   to the public HTTP endpoint.

Keep the old host ready to restore forwarding if issuance or website checks
fail. A successful Ansible run or a running container is not proof that HTTPS
is ready. Public DNS and router/firewall settings are not changed by Ansible.

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
5. Change LAN DNS/router clients to `192.168.68.111` when DNS is verified, and
   follow the HTTPS cutover checklist above for public website traffic. The
   Ansible variables do not configure clients.
6. Keep the old Pi available for rollback until the new services are proven.
   Only then manually retire the old k3s/Flux deployment and remove or archive
   its obsolete repository configuration. Those files are retained for rollback
   reference until migration is complete; they are not used by this playbook.

Named volumes persist across container recreation and `docker compose down`.
Never use `down -v` unless you intend to destroy data. Back up the Pi-hole volume
and encrypted host variables securely; a Teleporter export is also useful for
portable restoration. The generated Compose YAML contains the password, so
protect any backups of that file too.
