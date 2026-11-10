# Home Tools (LAN only)

The existing Rust dashboard has a Home Tools overview at `/homelab`. `/`
redirects to `/homelab`; Valheim controls remain at `/valheim`, without
introducing a frontend framework or a separate service. The service/binary names
remain `valheim-dashboard` for compatibility. Only the typed Valheim operations
and read-only metrics cross the restricted broker socket; the web account still
has no Docker access.

## Host and Docker overview

After an authorized deployment, open **`https://lab.mathijs.se/homelab`**.
The overview shows host CPU (250 ms sample), used/total memory, uptime and
1/5/15-minute load averages. The Host section puts machine specifications on the
left and these four readings in a 2×2 grid on the right, stacked on mobile.
Specifications are the DMI product name, CPU model, visible physical-core/logical-
thread counts, and OS-usable RAM (not an inventory of installed DIMMs). These use
small `/proc/cpuinfo` and `/sys/class/dmi/id/product_name` reads, with missing facts
shown as unknown. Serial numbers and UUIDs are never collected.
All Compose-managed containers, including stopped
ones, appear with state and Docker health when configured. Running containers
also show CPU, memory/limit, cumulative network and block I/O, and PID counts.
Disk space is shown separately for the system/SSD filesystem (`/`) and the
repository's RAID filesystem (`/srv/storage`): used, available, total and percentage
used. These are filesystem counters, not a directory scan or physical-device
inventory. RAID capacity is usable filesystem space (a mirror is not counted twice).
Host memory uses a native HTML `<meter>`. Compact disk rows use a lightweight stacked
CSS bar (with accessible meter semantics): used, available and filesystem reserves
are portions of the same total. Labels and numbers accompany the colours. Normal
mounted status is omitted; missing/unreadable disks show an error instead of a bar.
CPU remains a numeric percentage; there are
no chart libraries, animations or colour-coded CPU thresholds. Missing capacity
has no bar (and is never rendered as an indeterminate task-progress indicator).
Available space excludes reserved blocks; percentage follows `df` semantics:
`used / (used + available)`. Thus used + available can be smaller than total.
An exact mount-table match is required before sampling each path; an unmounted
RAID shows **Not mounted**, not the underlying SSD's space. Sampling holds an open
filesystem descriptor and checks its device number to avoid unmount fallback.
Mounted is not a RAID health assessment: redundancy, SMART and rebuild status are
not currently monitored.
Application-specific counters (such as Pi-hole query totals) are not collected;
this avoids adding application credentials or extra privileged integrations.

One fetch runs on page load, with a manual refresh button and **no background
polling, history database, exporter or monitoring container**. The broker caches
snapshots for 10 seconds across clients. Each Docker command has an eight-second
timeout; collections share the broker's operation gate, so they do not run beside
Valheim actions. Busy/unavailable refreshes retain the previous snapshot with a
stale warning. Partial failures show warnings and unknown values, not false zeros.
At most 64 Compose containers are supported; an oversized list fails explicitly.

The existing Ansible-owned dashboard binary/service installation deploys these
embedded assets; no listener, proxy route, Docker socket mount or web permission
change is needed. It currently remains coupled to the enabled Valheim dashboard.
The broker issues fixed read-only `docker ps` / `docker stats --no-stream`
commands, filters to Compose containers, and returns only allowlisted fields,
never environment variables, logs or raw inspection output. Existing LAN peer,
Host-header and browser security policies protect the new routes too.

Local verification includes Rust fixture/socket tests and dependency-free browser
logic tests (`node --test tools/homelab/web/*.test.mjs`). No live metrics or router
acceptance has been performed. After approving deployment, verify host values
against `free`/`uptime`, compare container metrics with `docker stats --no-stream`,
check stopped/unhealthy services, and repeat the external-access rejection tests
below. Compare disk space with `df -B1 / /srv/storage` (only compare the RAID if
actually mounted). The conservative choice here is a snapshot overview, not alerting or
long-term monitoring; independent Home Tools installation remains future work.

### Collection cost

Nothing is collected when the page is closed. One snapshot uses two `/proc/stat`
reads separated by a 250 ms sleep, small `/proc` reads for memory/load/uptime, one
mount-table read, two filesystem capacity queries, and at most two short-lived
Docker CLI processes (`ps` plus `stats --no-stream` for running Compose containers).
The CPU sampling wait does not busy-loop. Disk capacity queries read filesystem
counters without walking directories or reading file contents; no `du`, SMART
scan, RAID scrub, exporter, database or persistent history is introduced.
Docker statistics are the largest part of the work, and can take a few seconds.
Host sampling, disk capacity reads and Docker collection run concurrently within
one snapshot, using two short-lived worker threads on a cache miss. Docker `ps`
still precedes `stats` so only explicitly discovered running Compose containers
are sampled. The response waits for all three collectors; latency is roughly the
slowest collector rather than their sum. Docker's sampling wait therefore remains,
so this is not an instant-load guarantee. Partial failures still preserve other
collectors' results. The sample timestamp is assigned after collection completes.
The 10-second shared cache and single operation gate bound repeat collection;
there is no timer-driven polling. Actual CPU/RAM overhead has not been benchmarked
on the homelab. This runs inside the existing dashboard/broker, with no additional
resident service. Disk metadata queries are intended for the local SSD/ext4 RAID,
not potentially hanging network filesystems.

## Private hostnames and trusted HTTPS

The host configuration explicitly allows `lab.mathijs.se` in addition to its LAN
IP. This is a Host-header allowlist, **not DNS configuration or access control**.
The real TCP peer must still be in `192.168.68.0/24` or loopback.

Ansible prepares Pi-hole local records for `lab.mathijs.se`, `pihole.mathijs.se`,
`photos.mathijs.se` and `shared.mathijs.se`, pointing to `192.168.68.111`,
through `compose_pihole_local_hosts`. The reusable default is `null` (unmanaged);
an explicit list owns **all** Pi-hole local-host records and makes that setting
read-only in Pi-hole's UI. Import any pre-existing manual records into the list
before deploying, so they are not replaced.

These private names also have individual `local=/name/` dnsmasq rules in
`compose_pihole_dnsmasq_lines`. Local A records alone do not stop other record
types being forwarded: public Cloudflare HTTPS records can advertise ECH keys
that the local Caddy does not have, causing Chromium/Brave TLS failures or retry
errors. The rules keep missing HTTPS/AAAA records local (no upstream answer),
without disabling HTTPS/ECH globally or making the public `mathijs.se` zone local.
The reusable custom-lines default is `null` (unmanaged). An explicit list owns the
**entire** Pi-hole `misc.dnsmasq_lines` setting; import any existing custom lines
before opting in. Applying the Compose change recreates Pi-hole and briefly
interrupts DNS. Caddy's certificates and LAN allowlists are unchanged.

For an existing installation, apply only DNS changes from `ansible/`:

```bash
ansible-playbook site.yml --tags pihole-dns --check --ask-vault-pass --ask-become-pass
ansible-playbook site.yml --tags pihole-dns --ask-vault-pass --ask-become-pass
```

This renders the existing Compose configuration but applies **only Pi-hole**,
without rebuilding/restarting Caddy, the website, or other services.

After applying, `dig @192.168.68.111 lab.mathijs.se A` should return `.111` and
`dig @192.168.68.111 lab.mathijs.se HTTPS` should return NOERROR with no answer,
not Cloudflare ECH data. Public-domain queries should still resolve normally.
Restart the phone's browser after cached HTTPS records expire. Hosts-file
overrides alone are not a substitute for these DNS rules.

For initial testing without configuring a client to use Pi-hole, add this to its
hosts file (this overrides addresses only, not HTTPS/ECH records):

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
valuable saves. The host now enables scheduled image backups to the verified
RAID at `/srv/storage/backups/valheim`; existing SSD archives and live saves are
not moved. Pre-change archives and the game's native automatic backups remain
enabled. See [storage-service deployment and safety](storage-services.md).
