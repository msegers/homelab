# Private Tailscale access for mutual backups

This is an **owner-applied policy plan**, not a live network change. The local
Ansible renderer only writes a candidate JSON file. It never logs in, calls the
Tailscale API, shares a node, edits the active policy, or restarts a service.

## The model: who can reach which machine and port

The current default “All users and devices → all users and devices → all ports
and protocols” allows too much for sharing a backup receiver. **Replace that
allow-all rule**; adding a narrower grant does not subtract its permissions.
Tailscale grants and legacy ACLs are additive. There is no overriding network
deny rule; the `deny` entries below are policy **tests**, not firewall rules.

Start with this small access matrix:

| Source identity | Destination | Access |
| --- | --- | --- |
| Your exact Tailscale login | All available destinations | All ports/protocols, preserving your network management access |
| Friend's exact Tailscale login | Homelab's exact Tailscale IPv4 | TCP 8000 only (rest-server HTTPS) |
| Other identities | No explicit grant | Denied by default |

The broad owner grant is intentional; the friend's devices must never be signed
in under your account. User grants cover **all user-owned devices** belonging to
that identity, not just one tower. This first version is a one-owner/one-friend
plan, not a generic access-management system. To restrict a peer to one source
device or add trusted household users, review that separately.

“Services” here means machine + protocol + port. Tailscale does not distinguish
the website, dashboard and photo app if they share TCP 443, nor individual URL
paths or restic repository names. Do not allow 443 just to make backups work.
Rest-server's own TLS, HTTP credentials, private-repository isolation and
append-only mode remain required. Tailscale is the outer connectivity gate.

## Keep separate tailnets; share only each receiving machine

Each person keeps their own Tailscale account/tailnet. Share the Homelab receiving
machine **to the friend**, not membership of the entire tailnet. For the reverse
backup, the friend shares their receiving machine **to your account** and grants
your identity only their chosen backup port in their policy. Each person controls
the policy of their own tailnet. Share invitations alone do not enforce port limits.

Use Tailscale's ordinary, single-use machine-sharing invitation in the Machines
console. Do not invite the friend as a member/admin, enable subnet routing or an
exit node, or add public forwarding. Do not share an existing exit node without
separately reviewing its exit-node permissions. A shared node is quarantined by
default: it cannot initiate arbitrary connections into the recipient's tailnet.
Mutual sharing supplies the two intended receiving endpoints.

**Important prerequisite:** link-based sharing uses user-owned nodes. Tagged
devices cannot be used as cross-tailnet sharing clients, and ordinary sharing
has restrictions on tagged nodes. Keep these two backup machines authenticated
under their respective owners for this plan. In the Machines console, check that
Homelab and your sending device show your login, not `tag:...`. **Do not retag,
reauthenticate, or log a working node out just to follow this guide.** If a node
is already tagged, stop and choose a supported topology before changing it.
Tag grants identify whole devices, not individual services, and tagged devices
do not inherit their former owner's user grants or `autogroup:self` SSH access.

Verify the accepted recipient identity in the sharing dialog. An invitation sent
to an email can be accepted using a different Tailscale login; the policy needs
the **actual login**, including `name@github` or `name@passkey` if applicable.

## Prepare a candidate offline

From the repository root, copy `ansible/tailscale-policy.example.yml` to
`.ansible/tailscale-inputs.yml` (create `.ansible/` if needed). This directory is
gitignored. Edit the four values:

```yaml
tailscale_policy_owner: your-actual-login@example.com
tailscale_policy_friend: friends-actual-login@example.net
tailscale_policy_receiver_ip: 100.64.12.34 # Homelab's ACTUAL assigned Tailscale IPv4
tailscale_policy_backup_port: 8000
```

Neither a Tailscale API token nor an auth key is needed. The addresses/identities
are not credentials, but need not be committed. The input IP must match
`offsite_backup_listen_ip`; the port must match `offsite_backup_listen_port`.
The renderer refuses privileged ports and the existing Immich/dashboard ports.
If you customize the backup port, change both configurations together.

```bash
cd ansible
ansible-playbook -i localhost, tailscale-policy.yml -e @../.ansible/tailscale-inputs.yml
```

Review `.ansible/tailscale/policy.candidate.json` (relative to the repository root).
It contains two grants, a host alias, owner-only self-device SSH check rules, and
Tailscale policy tests. It is a **complete minimal candidate**, not a fragment to
append to the allow-all policy. It deliberately does not try to parse or merge
an unknown live policy. Re-rendering overwrites only this local candidate.

## Review and apply safely (explicit owner action)

1. **Keep an independent way back in.** Use the LAN or local console and keep
   Tailscale's web admin console open. Do not rely solely on the SSH session whose
   network rules you are replacing. Save a private copy of the complete current
   policy so you can restore it from the web console if needed.
2. Inventory current users/devices, their login identities/tags, any existing
   shared machines, subnet/exit-node routes and actual SSH port. The initial
   template preserves access **from your user-owned devices**, not from other
   household identities or automation tags. Add explicitly reviewed owner access
   tests for any nonstandard SSH port or other management device before saving.
3. Compare **all** existing sections against the candidate. Preserve needed
   `nodeAttrs`, `tagOwners`, posture settings and route approvals intentionally;
   do not blindly delete them. Review existing ACLs, grants and SSH rules rather
   than carrying broad permissions forward. Do not add `src: ["*"]`,
   `autogroup:shared`, or all tailnet members as the privileged owner group.
4. **SSH has two layers.** The owner grant permits network access to TCP 22;
   ordinary OpenSSH still checks its existing keys/accounts. If Tailscale SSH is
   enabled, the candidate permits only you to access your own user-owned nodes
   using check mode, including root/existing non-root accounts. Review that
   privilege explicitly; preserve or tighten an existing SSH policy as needed.
   This PR does not enable Tailscale SSH, change host keys or install Unix users.
5. In the Tailscale Access controls editor, prepare the reviewed replacement with
   the real identities. Remove the default broad ACL/grant **in the same change**.
   Use the editor's validation/preview and included tests before saving. Tailscale,
   not the local renderer, evaluates actual identity/sharing/access semantics.
   If an external friend is not yet recognized, consult the editor's identity
   validation; do not substitute an allow-all source just to pass validation.
6. Only after the narrow policy validates and is saved, send/accept the single-use
   sharing invitation. If resolving an external identity requires accepting the
   invite first, first save a temporary owner-only policy with no friend grant or
   friend tests (the owner grant/SSH rule and owner tests only), then share and add
   the narrow grant/tests. Never create the share while the default allow-all is
   active. Revoke the share if the intended restriction cannot be validated.
7. Confirm owner management access **before** any backup deployment. Have the
   friend test from their own user-owned device. Repeat the mirrored procedure
   on their side before enabling outbound backups.

Policy tests accept the friend's TCP backup connection and reject SSH, DNS, web,
SMB, photo/dashboard ports, UDP backup access and Valheim UDP ports. Representative
LAN-IP denials catch accidental subnet access; they are not an exhaustive scan.
Tests protect the rule structure on later edits, but do not prove a running
service exists or that the real sharing topology works. Unspecified access is
denied by the grants; the test list is not the complete list of protected ports.
Tailscale may permit ICMP for otherwise allowed peers; a successful ping does
not establish that another TCP/UDP service is reachable.

## Backups and acceptance

Policy changes do not create listeners. Keep both backup directions disabled
until policy review and TLS setup are complete. Then use the independently
authorized [off-site backup playbook](offsite-backup.md), with a certificate whose
SAN matches the endpoint. Use the receiving machine's full `.ts.net` name for
shared-machine MagicDNS, not its short hostname. Do not disable TLS verification.
The recipient-visible IPv4 can differ if there is an address collision; verify
the endpoint shown in the recipient's Tailscale client rather than guessing.

On a disposable repository, verify from the friend's real device:

- HTTPS/authenticated backup + restore works on TCP 8000; a wrong HTTP password
  still fails. A successful handshake alone does not prove backup/restore works.
- Connections to Homelab TCP 22/443/445/8080 fail, and the friend cannot reach LAN
  services through Homelab. Test from outside the LAN so ordinary LAN reachability
  cannot mask the tailnet result. Do not interpret an absent listener alone as
  proof of a policy denial; also inspect Tailscale's policy preview/tests.
- Your existing SSH path still works, unrelated household services remain as
  intended, and there are no new public listeners, routes or port forwards.
- In an approved reboot window, confirm tailscaled is logged in before the
  receiver binds. The existing backup unit does not yet have Tailscale-specific
  address readiness/recovery automation; a slow/offline client may require an
  owner-authorized receiver start after diagnosing network readiness. Do not
  automatically restart/reconfigure tailscaled or networking as a workaround.

To revoke access, revoke the machine share and remove the friend's grant/tests;
also rotate their HTTP credential if appropriate. Repeat on the other tailnet
to end reciprocal access. Preserve stored repositories and encryption keys.

There is no new dashboard listener or service to list. The existing Home Tools,
Immich and Valheim management LAN restrictions are deliberately unchanged; a
future request to make them accessible remotely needs both network and
application-layer review. No tailnet policy is applied by CI or `site.yml`.

References: [sharing](https://tailscale.com/kb/1084/sharing),
[grants](https://tailscale.com/kb/1324/grants),
[policy syntax and tests](https://tailscale.com/kb/1337/policy-syntax).
