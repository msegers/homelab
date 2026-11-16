# Services and maintenance

Deploy using the [Ansible setup guide](../ansible/README.md). The stack lives in
`/opt/homelab` on the server.

## Services

| Service | Address |
| --- | --- |
| Website | `https://mathijs.se` and `https://www.mathijs.se` |
| Pi-hole admin (LAN only, after deployment/local DNS setup) | `https://pihole.mathijs.se/admin/` |
| Home Tools (LAN only) | `https://lab.mathijs.se/valheim` |
| Pi-hole DNS | `192.168.68.111:53` (TCP and UDP) |

Ports bind to `compose_bind_ip` in the host variables. Caddy redirects website
HTTP to HTTPS and restricts Pi-hole admin to the explicit
`compose_pihole_allowed_networks` TCP-peer allowlist (`192.168.68.0/24` for this
host; loopback-only in reusable defaults). Forwarding headers are not trusted. Use the
admin interface over a trusted LAN or SSH tunnel, and keep DNS port 53 LAN-only.
Review router forwarding and Docker-aware firewall rules, especially if another
proxy rewrites client addresses.

For LAN testing, point the website domains at the bind IP using local DNS or a
hosts file. The homelab now configures `compose_pihole_domain: pihole.mathijs.se`
and its local DNS record; reusable defaults retain the old `nip.io` name. See
[Pi-hole first use](home-tools.md#pi-hole-first-use) to test without changing
your router's DNS settings. No live deployment has been performed by these changes.
With `compose_private_tls_enabled`, private hostnames and the public website use
Cloudflare DNS-01 with a scoped Vault token and a custom
Caddy build; see [private HTTPS setup](home-tools.md#cloudflare-certificate-setup-before-deployment).
With that option disabled, the public website retains stock Caddy's default
ACME validation. The public cutover instructions below apply only to that
website, not to private hostnames.

## HTTPS and public cutover

1. Point both public website domains at your WAN IP. Check A/AAAA records and
   remove stale IPv6 records; the stack publishes on IPv4. Check any CAA records
   permit Caddy's certificate issuer.
2. For public website traffic, maintain the intended origin address and existing
   TCP 80/443 forwarding to `compose_bind_ip`. Allow outbound HTTPS and DNS.
   This host obtains and renews certificates using Cloudflare DNS-01; it does not
   need inbound HTTP-01 or TLS-ALPN challenges. The custom Caddy build and scoped
   token are configured by Ansible. Local hosts entries alone do not issue certificates.
3. If the website uses Cloudflare's proxy (orange cloud), use **Full (strict)**
   SSL/TLS mode once Caddy has its trusted certificate. Avoid **Flexible**, which
   sends HTTP to Caddy and can loop against its HTTPS redirect. DNS-01 works with
   the proxy enabled, but does not fix an incorrect origin IP or unreachable origin
   (Cloudflare 523). Reusable defaults without DNS-01 still require working inbound
   ACME challenge routing. Schedule any public cutover separately; no router or
   Cloudflare edge settings are changed by this playbook.
4. Deploy, then check certificate issuance for both domains:

   ```bash
   cd /opt/homelab
   sudo docker compose logs --tail=100 proxy
   ```

5. Verify locally and from outside the LAN, with certificate validation enabled:

   ```bash
   curl -I http://mathijs.se/
   curl -I https://mathijs.se/
   curl -I https://www.mathijs.se/
   ```

   Expect an HTTP-to-HTTPS redirect, valid certificates, and the intended website.
   Also verify external requests to the Pi-hole admin hostname are blocked.

Keep the old host available to restore forwarding if checks fail. Caddy stores
certificates in the persistent `caddy_data` volume; include it in secure backups.

## Updates

### Stable Caddy source address for Home Tools

The homelab inventory opts into `compose_caddy_static_ip_enabled`; reusable defaults
leave it off. Caddy retains its `homelab_default` app-network connection but also
gets a dedicated `homelab_caddy_egress` bridge:

- `172.30.255.0/28`, host bridge `br-hl-caddy`, gateway `.1`, Caddy **`.2`**.
- The automatic allocation pool is `.8/29`, excluding Caddy's reserved address.
- `gw_priority: 100` makes this bridge the default egress path. Merely assigning
  an address on a second network is **not enough** to stabilize the source seen
  by a host-bound dashboard. The app-network address may still change harmlessly.
- The dashboard trusts **only `172.30.255.2/32`**, after confirming the named Caddy
  container actually has that address on the reserved network. It no longer adds
  the changing app-network address to its trusted peers. No Docker subnet is
  allowlisted. The dashboard still has no Docker socket access.

This fixes the reboot/recreation failure where Caddy moved from `172.18.0.4` to
`172.18.0.3` while the dashboard still trusted `.4`. The new network is deliberately
separate: the existing `homelab_default` IPAM, website/Pi-hole connections and
Immich's external attachment are **not** reconfigured, disconnected or deleted.
Published ports, Caddy's LAN client restrictions, DNS, certificates and volumes
are unchanged. The Home Tools dashboard needs no new service/metrics entry: this
is an address change to its existing proxy, not an additional application.

**Not deployed by this PR.** Review `compose_caddy_network_prefix` before applying;
`172.30.255` is a proposed private three-octet prefix, not a measured free subnet.
Preflight rejects overlapping non-default IPv4 routes in all host routing tables,
including LAN/VPN/Docker routes; Docker also rejects overlapping IPAM pools when
creating the network. An existing reserved network must match its ownership,
bridge name, subnet/gateway/pool and expected Caddy-only membership. No automatic
network migration or collision repair is attempted. If it conflicts, choose a
free prefix **before first deployment**. Changing it on an already created bridge
requires a separately reviewed migration; do not delete shared networks to retry.

Requires **Docker Compose >= 2.33.1 and Docker API >= 1.48 (Engine 28+)** for gateway
priority. Preflight checks both before rendering/reconciling. Unsupported versions
fail rather than silently continuing with a dynamic source. Upgrade separately
if needed; this change does not restart Docker or install an upgrade.

No firewall rules are changed. Review any existing Docker/host filtering before
apply: a firewall that permits only the old proxy source may need a separately
approved allowance from the new exact IP to TCP 8787. Preserve existing rules;
do not flush them or allow the entire bridge subnet as a workaround.

For an already provisioned host, plan a brief **Caddy interruption** affecting
website/private HTTPS. Keep a direct LAN/SSH management path available. From
`ansible/`, first review a check run; only apply in an explicitly approved window:

```bash
ansible-playbook site.yml --tags caddy-ip,valheim --check --ask-vault-pass --ask-become-pass
# Owner-authorized apply, not an automatic recovery/reboot step:
ansible-playbook site.yml --tags caddy-ip,valheim --ask-vault-pass --ask-become-pass
```

The `caddy-ip` path renders Compose and reconciles **only `proxy`**, with dependencies,
pulls and builds disabled; it requires the existing stack, Caddy configuration and
image. The `valheim` provisioning tag then discovers the reserved peer and refreshes
dashboard/controller configuration; it does **not** start/restart the game. Do not
omit that tag during the initial migration or the dashboard will retain old trust.
A normal full `site.yml` apply also supports this configuration, but can reconcile
other services and therefore has a broader maintenance scope. DNS-only applies
refuse a first-time Caddy migration; deploy this change first.

Apply a second time to check idempotence. Post-apply and after an owner-approved
reboot/proxy recreation, use the **read-only** verifier before reconciling again:

```bash
ansible-playbook verify-compose.yml --ask-vault-pass --ask-become-pass
```

It checks Caddy's actual endpoint, explicit IPAM request, gateway priority and the
route's selected source to `compose_bind_ip` using `ip route get` inside Caddy.
It sends no probe traffic and restarts nothing. Also verify on the server:

The route check uses the `ip` utility shipped in the configured Alpine Caddy
images; custom Caddy images must provide it too.

```bash
sudo systemctl show valheim-dashboard -p ExecStart --no-pager --full
sudo docker exec homelab-proxy-1 ip -4 route get 192.168.68.111
```

Expect `--trusted-proxy 172.30.255.2/32` and `src 172.30.255.2`. Check
`https://lab.mathijs.se`, the public website, private Pi-hole/Immich, actual Pi-hole
DNS resolution, and that the game's state is unchanged. Non-LAN and spoofed-header
requests must still fail. Do not test missing-network behavior by deleting live
networks. Local tests render/parse Compose, exercise fixture guards and verify
proxy trust across changing app IPs; **real routing, reboots and service continuity
remain host acceptance**, not claims from those tests.

Rollback is an owner-reviewed maintenance action: set
`compose_caddy_static_ip_enabled: false` in inventory and apply the same
`caddy-ip,valheim` tags. The host's dashboard selectors then revert to exact-peer
discovery. This recreates only Caddy and refreshes trust; it intentionally leaves
the now-unused reserved network rather than deleting anything automatically.
Confirm routing/access before separately reviewing any empty-network cleanup.
Rollback restores the old dynamic-source behavior, including its reboot caveat.

### Image and configuration updates

Change host variables or role templates and rerun Ansible. For image updates,
back up first, then run on the server:

```bash
cd /opt/homelab
sudo docker compose pull
sudo docker compose up -d --wait
sudo docker compose ps
```

Pin `compose_*_image` variables to tested tags or digests for reproducibility.
Ansible pulls missing images and restarts Caddy when its configuration changes.
The website image should support the server's architecture and listen on port 80.
For a private image, authenticate as root on the server, then rerun deployment:

```bash
printf '%s' "$GHCR_TOKEN" | sudo docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

## Backups and migration

- Back up Pi-hole settings with Teleporter. Check v6 import compatibility when
  restoring settings from an older installation; DHCP needs a separate plan.
- Protect backups of named volumes, encrypted host variables, and the generated
Compose file, which contains the Pi-hole password and Cloudflare token. Preserve volumes when
  stopping the stack: `docker compose down -v` deletes them.
- Verify DNS and restored settings before pointing LAN clients at the new IP.
- Keep the old host for rollback until migration is verified, then retire it
  and remove or archive the legacy K3s/Flux/Terraform configuration.

## Troubleshooting

- Inspect logs with `sudo docker compose logs --tail=100` in `/opt/homelab`.
- Check port conflicts with `sudo ss -lntup`, including an existing host DNS
  resolver on port 53.
- Resolve conflicting distro Docker/containerd packages before installing
  Docker's packages. Existing K3s servers require backup and manual retirement.
- Treat `ansible-playbook site.yml --check --diff --ask-vault-pass` as a preview:
  fresh-host package dependencies may fail in check mode; Compose startup is
  skipped. Verify the applied deployment using the service checks above.
