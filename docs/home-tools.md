# Home Tools (LAN only)

The existing Rust dashboard now has a small Home Tools navigation shell. `/`
redirects to `/valheim`; future tool pages can have their own routes without
introducing a frontend framework or a separate service. The service/binary names
remain `valheim-dashboard` for compatibility. Only the typed Valheim operations
cross the restricted broker socket; the web account still has no Docker access.

## Hostname: first stage

The host configuration explicitly allows `lan.mathijs.se` in addition to its LAN
IP. This is a Host-header allowlist, **not DNS configuration or access control**.
The real TCP peer must still be in `192.168.68.0/24` or loopback.

Ansible now prepares Pi-hole local records for `lan.mathijs.se` and
`pihole.mathijs.se`, both pointing to `192.168.68.111`,
through `compose_pihole_local_hosts`. The reusable default is `null` (unmanaged);
an explicit list owns **all** Pi-hole local-host records and makes that setting
read-only in Pi-hole's UI. Import any pre-existing manual records into the list
before deploying, so they are not replaced. No live Pi-hole configuration has
been inspected or changed.

No LAN clients currently use Pi-hole for DNS. For initial testing after an
authorized deployment, add this to a client hosts file:

```text
192.168.68.111 lan.mathijs.se pihole.mathijs.se
```

The implemented direct listener is still
`http://lan.mathijs.se:8787/valheim`. It is not yet the requested port-free URL.
For central resolution later, deploy the record and configure router DHCP or
individual clients to use Pi-hole. Local records are
not usable by clients that choose another resolver (including browser DoH).
No public DNS record, router forwarding, or router conversion is required.

## Pi-hole first use

Target after an authorized deployment: **`http://pihole.mathijs.se/admin/`**.
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
   curl --resolve pihole.mathijs.se:80:192.168.68.111 http://pihole.mathijs.se/admin/
   ```

2. Open the admin URL in the browser, sign in, and check Pi-hole is healthy.
3. Test DNS directly, without changing any client resolver settings:

   ```bash
   dig @192.168.68.111 pihole.mathijs.se A
   dig @192.168.68.111 lan.mathijs.se A
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

## Port-free HTTP: pending networking verification

Target: `http://lan.mathijs.se/valheim`. A second LAN IP is **not required**.
The existing Caddy listener at `192.168.68.111:80` can route by hostname while
continuing to serve `mathijs.se`. However, public website forwarding also reaches
that listener: a private DNS record alone does not prevent someone outside the
LAN from supplying `Host: lan.mathijs.se`.

Before adding the route:

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

No **Home Tools** Caddy route is included yet (the existing Pi-hole route is
updated separately): the above verification needs
operator input, not assumptions about the live router or Docker bridge. A
separate LAN IP remains an optional stricter isolation arrangement, not a DNS
requirement. HTTPS and making the homelab a router are outside this stage.

## Acceptance still required

Automated tests use fake engines, local HTTP/Unix sockets, and Ansible fixtures.
They do not prove live game save compatibility, clean saves during real world
switches, router isolation, or LAN DNS resolution. Test with disposable worlds
first; never use valuable saves to validate deletion with backups disabled.
