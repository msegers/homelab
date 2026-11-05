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
Private hostnames use Cloudflare DNS-01 with a scoped Vault token and a custom
Caddy build; see [private HTTPS setup](home-tools.md#cloudflare-certificate-setup-before-deployment).
The public website still uses its original ACME validation. The public cutover
instructions below apply only to that website, not to private hostnames.

## HTTPS and public cutover

1. Point both public website domains at your WAN IP. Check A/AAAA records and
   remove stale IPv6 records; the stack publishes on IPv4. Check any CAA records
   permit Caddy's certificate issuer.
2. Forward TCP 80/443 to `compose_bind_ip` and allow outbound HTTPS. Caddy uses
   public ACME validation to obtain and renew certificates. Local hosts entries
   alone cannot satisfy validation.
3. Schedule the forwarding switch from the old host. Pre-issuing certificates
   through DNS validation requires a custom Caddy image with a provider plugin.
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
