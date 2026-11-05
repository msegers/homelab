# Home Tools (LAN only)

The existing Rust dashboard now has a small Home Tools navigation shell. `/`
redirects to `/valheim`; future tool pages can have their own routes without
introducing a frontend framework or a separate service. The service/binary names
remain `valheim-dashboard` for compatibility. Only the typed Valheim operations
cross the restricted broker socket; the web account still has no Docker access.

## Private hostnames and trusted HTTPS

The host configuration explicitly allows `lab.mathijs.se` in addition to its LAN
IP. This is a Host-header allowlist, **not DNS configuration or access control**.
The real TCP peer must still be in `192.168.68.0/24` or loopback.

Ansible prepares Pi-hole local records for `lab.mathijs.se` and
`pihole.mathijs.se`, both pointing to `192.168.68.111`,
through `compose_pihole_local_hosts`. The reusable default is `null` (unmanaged);
an explicit list owns **all** Pi-hole local-host records and makes that setting
read-only in Pi-hole's UI. Import any pre-existing manual records into the list
before deploying, so they are not replaced. No live Pi-hole configuration has
been inspected or changed.

No LAN clients currently use Pi-hole for DNS. For initial testing after an
authorized deployment, add this to a client hosts file:

```text
192.168.68.111 lab.mathijs.se pihole.mathijs.se
```

After an authorized deployment, use **`https://lab.mathijs.se/valheim`**.
Caddy terminates browser-trusted HTTPS and proxies to the private HTTP backend
on port 8787. The port-free route checks the real LAN peer before serving any
page, token or API. Only the explicitly discovered Caddy `/32` peers may supply
the HTTPS forwarding scheme; other clients cannot spoof it. Host, Origin and
action-token checks remain enabled. Re-run the Valheim playbook after recreating
the proxy container if its IP changes; stale peers fail closed.
For central resolution later, deploy the record and configure router DHCP or
individual clients to use Pi-hole. Local records are
not usable by clients that choose another resolver (including browser DoH).
No public DNS record, router forwarding, or router conversion is required.

### Cloudflare certificate setup (before deployment)

Cloudflare hosts the authoritative DNS for `mathijs.se`. Create an API token in
Cloudflare **My Profile → API Tokens → Create Token → Custom token**:

- Permissions: **Zone → Zone → Read** and **Zone → DNS → Edit**.
- Zone resources: **Include → Specific zone → mathijs.se** only.
- Do not use the global API key or an account-wide token. If setting expiration,
  plan rotation before expiry so renewals continue working.

From `ansible/`, edit the **existing** Vault, preserving its passwords:

```bash
ansible-vault edit host_vars/homelab/vault.yml
```

Add `vault_cloudflare_api_token: 'YOUR_SCOPED_TOKEN'` in that editor, never in
chat, a shell command, or plaintext Git files. The committed host configuration
enables `compose_private_tls_enabled` and references this Vault key. Reusable
defaults leave TLS opt-in and private routes loopback-only. Missing credentials
abort deployment rather than falling back to an untrusted certificate.

Ansible builds Caddy 2.11.8 with `caddy-dns/cloudflare` v0.2.4; the build context
excludes the token-bearing Compose file. The runtime token is in the root-only
Compose configuration/container environment; Docker/root administrators can
read it. Container inspection is hidden from Ansible output. Do not publish raw
`docker inspect` or `docker compose config` output.

With `compose_private_tls_enabled`, both the private hostnames and the public
website (`mathijs.se` and `www.mathijs.se`) use DNS-01: Caddy creates temporary
public ACME TXT records through Cloudflare. No public A/AAAA records or new
forwarding are needed for the private names. Outbound HTTPS and DNS are needed
for issuance and renewal; inbound HTTP/TLS challenge routing is not required.
Public website proxy routing and persistent Caddy certificate storage are unchanged.
Certificates expose these hostnames in public Certificate Transparency logs;
they do not make the applications public. No private CA installation is needed.

## Pi-hole first use

Target after an authorized deployment: **`https://pihole.mathijs.se/admin/`**.
The existing Pi-hole Caddy route now uses that hostname on this host, with an
explicit `192.168.68.0/24` real-TCP-peer allowlist. It rejects other peers with
403 on all paths, including the admin API; spoofed forwarding headers do not
grant access. Pi-hole's own password is still required (the existing vaulted
`vault_compose_pihole_password`). No new listener, public DNS record, or router
forwarding is added. Reusable proxy defaults permit only loopback peers.

You do **not** need to change the Deco or enable DHCP in Pi-hole to try it:

1. After approving deployment, use the hosts-file entry above on one LAN client.
   Alternatively, test routing without DNS with:

   ```bash
    curl --resolve pihole.mathijs.se:443:192.168.68.111 https://pihole.mathijs.se/admin/
   ```

2. Open the admin URL in the browser, sign in, and check Pi-hole is healthy.
3. Test DNS directly, without changing any client resolver settings:

   ```bash
   dig @192.168.68.111 pihole.mathijs.se A
    dig @192.168.68.111 lab.mathijs.se A
   dig @192.168.68.111 example.com A
   ```

   Both local records should return `192.168.68.111`; ordinary names should resolve.
4. Optionally set just that client's DNS to `192.168.68.111` for a trial. Only
   after it works should you consider advertising it via Deco DHCP below.

Before deployment, verify public forwarded traffic does not appear to Caddy as
a trusted LAN peer (the source-address caveat below applies to Pi-hole too).
After deployment, test from outside the LAN with the Pi-hole Host header and
expect 403; absence of a public DNS record is not an access-control test. Keep
port 53 unforwarded and protect it with the host's Docker-aware LAN firewall.

### TP-Link Deco P7

Keep the P7 as the router and DHCP server. TP-Link's [Deco DNS guide](https://www.tp-link.com/us/support/faq/1855)
documents **More → Advanced → DHCP Server** in the Deco app for LAN DNS settings
(wording/availability varies by firmware). After checking Pi-hole works on one
client, advertise `192.168.68.111` as the LAN DNS server, and renew clients' DHCP
leases. Reserve the homelab's existing IP so it remains stable. Do not advertise
a public secondary DNS if you expect local names to resolve consistently: clients
can use it even when Pi-hole is available. This makes LAN DNS depend on the
homelab being up; record the previous settings for rollback. Do not enable a
second DHCP server in Pi-hole or change port-forwarding rules for this feature.

Deco in Access Point mode does not own DHCP; configure the upstream router
instead. The model alone does not prove how external source addresses appear
at Caddy; port-free routing still needs the check below.

## Port-free HTTPS: networking acceptance required

Target: `https://lab.mathijs.se/valheim`. A second LAN IP is **not required**.
The existing Caddy listener at `192.168.68.111:443` routes by hostname while
continuing to serve `mathijs.se`. However, public website forwarding also reaches
that listener: a private DNS record alone does not prevent someone outside the
LAN from supplying `Host: lab.mathijs.se` and matching TLS SNI.

Before relying on the deployed routes:

1. Verify the router's public forwarding preserves the external client's source
   address. If it SNATs public traffic into a trusted LAN address, a subnet rule
   at Caddy cannot distinguish it from a LAN client; choose a separate listener
   IP/interface or enforce the distinction upstream instead.
2. Gate the route on the **actual** Caddy TCP peer in `192.168.68.0/24`, before
   proxying any static page, session token, or API request. Do not trust client
   `Forwarded`/`X-Forwarded-For` headers or use a broad `private_ranges` rule.
3. Establish Caddy's specific Docker-to-host source address and authorize only
   that proxy peer at the backend. Do not broadly allow Docker networks or turn
   off backend Host/Origin/token checks. Preserve the browser Host and Origin.
4. Test LAN page/actions and rejection from a real external connection, including
   a forged Host header. Confirm the public website is unchanged.

Both private routes are included, but none of these live checks have been run.
The above verification needs operator input, not assumptions about the live
router or Docker bridge. A separate LAN IP remains an optional stricter
isolation arrangement, not a DNS requirement. HTTP redirects may be visible to
external peers, but HTTPS application content must return 403 to them.

## Acceptance still required

Automated tests use fake engines, local HTTP/Unix sockets, and Ansible fixtures.
They do not prove live game save compatibility, clean saves during real world
switches, router isolation, or LAN DNS resolution. Test with disposable worlds
first. Deletion always requires a successful recovery archive, independently of
scheduled backups; still verify real-game restoration before relying on it for
valuable saves. Scheduled image backups remain off until the NAS is ready, while
pre-change archives and the game's native automatic backups remain enabled.
