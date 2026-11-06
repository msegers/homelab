# Two-disk NAS mirror

This is a whole-disk **mdadm RAID 1**, ext4, mounted at `/srv/storage`. Usable
capacity is approximately one 1 TB disk (931 GiB before filesystem overhead),
not the combined capacity. RAID is not a backup. No Samba, LVM, ZFS, backup
jobs, or changes to Docker/Pi-hole/Caddy/Valheim are included.

`main` was updated before implementation. Read-only inspection confirmed:

| Purpose | Model / serial | Stable whole-disk path |
| --- | --- | --- |
| Mirror member | WDC WD10EZEX-08WN4A0 / WD-WCC6Y0UNLFLH | `/dev/disk/by-id/ata-WDC_WD10EZEX-08WN4A0_WD-WCC6Y0UNLFLH` |
| Mirror member | ST1000NM0011 / Z1N245KW | `/dev/disk/by-id/ata-ST1000NM0011_Z1N245KW` |
| **Protected OS SSD** | Samsung SSD 850 EVO 500GB / S21JNXAG412018R | `/dev/disk/by-id/ata-Samsung_SSD_850_EVO_500GB_S21JNXAG412018R` |

Both HDDs were **1,000,204,886,016 bytes**, each with one unmounted NTFS
partition. The SSD contains `/`, `/boot/efi`, and swap. Device letters are not
used for writes. SMART extended-test results were supplied by the owner, not
retested by this role.

## Safety and scope

The `nas_storage` role is integrated into `ansible/site.yml`, tagged `storage`.
Reusable defaults are disabled. The committed homelab inventory enables the
role and pins both member paths, models, serials, capacities, and the protected
SSD path. **Initialization defaults to false and is not enabled in inventory.**
Until initialized, an ordinary full deployment fails at the storage gate before
other roles run. Use the targeted commands below; they do not run other roles or
their restart handlers.

Preflight rejects missing disks, identity/capacity mismatches, aliases to the
same disk or OS SSD, mounted members/swap, kernel holders, old fstab references,
unexpected partitions, existing RAID/ext4/LVM/encryption signatures, an existing
named array, or an occupied/nonempty/symlinked mountpoint. Only empty disks or
the inspected single-NTFS-partition layout can be initialized. Existing mount
and mdadm entries for unrelated storage are not replaced.

Before erasure, the role writes `/var/lib/homelab-storage/identity.json` with
`phase: initializing`. After creating the array and filesystem it records
`phase: ready`, the hardware identities, md UUID, and ext4 UUID. This file is
root-only; **do not remove or edit it to bypass a failed initialization**.
An interrupted initialization stops subsequent runs for manual diagnosis even
if the opt-in flag is supplied again. Existing RAID signatures also block
creation when the record is absent. Do not run concurrent initialization or
change/mount the disks while it is running.

Routine deployments skip **all** wipe/create/format tasks, even if someone
leaves the initialization opt-in enabled. They verify both members' metadata,
array UUID/type/member count, actual active members, degraded state, and the
recorded ext4 UUID. They may assemble an **existing** inactive array using only
its recorded UUID and exact two members (no `--force`), then maintain its
configuration and mount. A missing disk, degraded mirror, foreign filesystem,
or incomplete identity causes failure: no add/remove/replace/reinitialize or
filesystem repair is attempted. Missing members require a separate reviewed
replacement procedure, not another initialization run.

Persistence is one UUID-specific `ARRAY` line in `/etc/mdadm/mdadm.conf`, an
initramfs refresh when that line changes, and a UUID-based ext4 `/etc/fstab`
entry. The OS SSD is **never wiped, partitioned, RAID-created, or formatted**.
The requested persistence necessarily installs packages and changes normal OS
configuration files/initramfs; it does not alter the SSD's partition layout or
existing filesystems. No live service restart or reboot is requested by the role.

The mount uses `defaults,nofail,x-systemd.device-timeout=30s`, fsck pass 2.
Missing storage must not prevent unrelated homelab services from booting.
Normal Linux RAID assembly/resync and boot-time ext4 checks remain available;
this is not an automatic disk-replacement mechanism. Boot may make a degraded
mirror available; the Ansible health checks still report degraded as failure.
If the volume is unavailable, `/srv/storage` is just an underlying directory:
future consumers must require the mount, not write there blindly. No existing
service is redirected to this storage.

## Exact destructive operations — REVIEW ONLY

These are the commands initialization will issue for the currently inspected
NTFS layout. **Do not run this block separately**; Ansible performs safety checks
and records the one-time operation before executing them.

```bash
# Destroy the NTFS signatures on both approved old partitions.
wipefs --all -- /dev/disk/by-id/ata-WDC_WD10EZEX-08WN4A0_WD-WCC6Y0UNLFLH-part1
wipefs --all -- /dev/disk/by-id/ata-ST1000NM0011_Z1N245KW-part1

# Destroy the partition-table signatures on those same two HDDs.
wipefs --all -- /dev/disk/by-id/ata-WDC_WD10EZEX-08WN4A0_WD-WCC6Y0UNLFLH
wipefs --all -- /dev/disk/by-id/ata-ST1000NM0011_Z1N245KW
udevadm settle

# Create the mirror. No --force or --assume-clean: initial resync is required.
mdadm --create /dev/md/storage --metadata=1.2 --level=1 --raid-devices=2 \
  --name=storage --bitmap=internal --run \
  /dev/disk/by-id/ata-WDC_WD10EZEX-08WN4A0_WD-WCC6Y0UNLFLH \
  /dev/disk/by-id/ata-ST1000NM0011_Z1N245KW

# Format ONLY the new mirror, never an individual member or the SSD.
mkfs.ext4 -L storage -m 0 /dev/md/storage
```

`wipefs` removes signatures, not every old data sector. It is destructive
initialization, not a secure erase. Array creation and filesystem creation also
overwrite HDD metadata. No partition creation is needed for whole-disk members.

## Owner-run deployment

**Nothing is deployed by repository tests or by reading these instructions.**
Install controller-side collection dependencies from `ansible/`:

```bash
ansible-galaxy collection install -r requirements.yml
```

First preview the initialization. This reads target state and predicts package
changes, but does **not** wipe/create/format, create the identity record, mount,
or assemble an array:

```bash
ansible-playbook site.yml --limit homelab --tags storage --check \
  -e '{"nas_storage_initialize": true}' --ask-vault-pass --ask-become-pass
```

Review the disk IDs, serials, sizes, preflight output, and destructive plan.
If any check fails, stop; do not relax the checks or erase metadata manually.
When you explicitly decide to erase **both HDDs**, run the same targeted play
**without `--check`** (this is the destructive authorization):

```bash
ansible-playbook site.yml --limit homelab --tags storage \
  -e '{"nas_storage_initialize": true}' --ask-vault-pass --ask-become-pass
```

Use a JSON boolean as shown, not a string-valued `-e nas_storage_initialize=true`.
After initialization, omit the opt-in permanently:

```bash
ansible-playbook site.yml --limit homelab --tags storage --ask-vault-pass --ask-become-pass
```

The repeat run should report no changes once configuration is maintained. The
first resync runs in the kernel; Ansible reports `sync_action` but does not poll
or wait for it. Do not test disk removal or power off during initial setup.
Allow resync to finish and confirm both members are healthy first.

## Health and reboot acceptance

In a privileged host shell, these are read-only checks:

```bash
cat /proc/mdstat
mdadm --detail --test /dev/md/storage
mdadm --detail /dev/md/storage
findmnt --mountpoint /srv/storage --output SOURCE,UUID,FSTYPE,TARGET,OPTIONS
df -hT /srv/storage
cat /var/lib/homelab-storage/identity.json
```

Require two active members (`[UU]`), no failed devices, RAID 1, ext4, and a mount
UUID matching the recorded filesystem UUID. After initial resync, require idle
synchronization. A read-only health check is not a hardware-failure simulation.

After successful checks and completed resync, arrange a maintenance window for
all services and **manually** reboot with local-console access available. No
playbook reboots automatically. Before running Ansible or any repair afterward,
repeat the read-only checks to prove boot-time assembly and mounting worked.
Then repeat the routine targeted playbook and check idempotence. Confirm existing
OS mounts and other homelab services are unchanged. A local test/syntax pass is
not real-disk or reboot acceptance.

If anything fails, retain the identity file and diagnostics. Do **not** run
`mdadm --create`, `wipefs`, `mkfs`, `--force`, remove a record, or recreate the
array to recover it. Diagnose and plan recovery separately.
