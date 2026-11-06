"""Local Ansible safety regressions. Never open disks, mount, or run mdadm/mkfs."""

import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/nas_storage"
HOST = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
DISKS = HOST["nas_storage_disks"]
ARRAY_UUID = "12345678:12345678:12345678:12345678"
FS_UUID = "12345678-1234-1234-1234-123456789abc"
IDENTITY = {"phase": "ready", "disks": DISKS,
            "protected_disk": HOST["nas_storage_protected_disk"],
            "array_uuid": ARRAY_UUID, "filesystem_uuid": FS_UUID}


def encoded(value):
    return {"content": base64.b64encode(value.encode()).decode()}


def task(file, name):
    return next(t for t in yaml.safe_load((ROLE / f"tasks/{file}.yml").read_text())
                if t["name"] == name)


class StorageTests(unittest.TestCase):
    def run_tasks(self, tasks, success=True, **variables):
        settings = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        settings.update(ansible_distribution="Debian", nas_storage_disks=DISKS,
                        nas_storage_protected_disk=HOST["nas_storage_protected_disk"])
        settings.update(variables)
        with tempfile.TemporaryDirectory(
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        ) as directory:
            path = Path(directory) / "play.yml"
            path.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
                                            "vars": settings, "tasks": tasks}]))
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            return result

    def test_role_is_noop_by_default_even_with_initialization_flag(self):
        self.run_tasks([{"ansible.builtin.include_role": {"name": str(ROLE)}}],
                       nas_storage_initialize=True)

    def test_first_run_requires_explicit_opt_in(self):
        guard = task("manage", "Require deliberate first initialization")
        for initialize, success in [(False, False), (True, True)]:
            self.run_tasks([guard], success=success, nas_storage_initialize=initialize,
                           nas_storage_record={"stat": {"exists": False}})

    def test_explicit_opt_in_still_cannot_initialize_an_existing_array_or_mount(self):
        guard = task("initialize", "Refuse to initialize over an existing array or mount")
        for exists, mounted, success in [(False, False, True), (True, False, False),
                                         (False, True, False)]:
            self.run_tasks([guard], success=success, nas_storage_initialize=True,
                           nas_storage_array={"stat": {"exists": exists}},
                           nas_storage_mount={"rc": 0 if mounted else 1})

    def test_incomplete_or_changed_record_cannot_be_bypassed_by_opt_in(self):
        guard = task("manage", "Validate the existing storage identity before any disk operations")
        for change, success in [({}, True), ({"phase": "initializing"}, False),
                                ({"disks": list(reversed(DISKS))}, False),
                                ({"array_uuid": ""}, False), ({"filesystem_uuid": ""}, False)]:
            self.run_tasks([guard], success=success, nas_storage_initialize=True,
                           nas_storage_record={"stat": {"exists": True}},
                           nas_storage_record_content=encoded(json.dumps(dict(IDENTITY, **change))))

    def test_missing_disk_is_fatal_even_with_initialization_enabled(self):
        guard = task("manage", "Refuse missing disks rather than rebuilding or initializing")
        for stat, success in [({"exists": True, "isblk": True}, True),
                              ({"exists": False}, False),
                              ({"exists": True, "isblk": False}, False)]:
            self.run_tasks([guard], success=success, nas_storage_initialize=True,
                           nas_storage_disk_stats={"results": [{"item": DISKS[1]["path"], "stat": stat}]})

    def test_os_disk_and_duplicate_aliases_are_rejected(self):
        guard = task("manage", "Reject aliases to the same disk or to the OS disk")
        for paths, success in [(["/dev/sdb", "/dev/sdc", "/dev/sda"], True),
                               (["/dev/sdb", "/dev/sdb", "/dev/sda"], False),
                               (["/dev/sda", "/dev/sdc", "/dev/sda"], False)]:
            self.run_tasks([guard], success=success,
                           nas_storage_resolved={"results": [{"stdout": p} for p in paths]})
        config = task("manage", "Validate the deliberately limited storage configuration")
        self.run_tasks([config], success=False, nas_storage_protected_disk=DISKS[0]["path"])

    def test_hardware_model_serial_capacity_and_readonly_are_checked(self):
        guard = task("manage", "Require the exact approved hardware")
        original = dict(type="disk", model=DISKS[0]["model"], serial=DISKS[0]["serial"],
                        size=DISKS[0]["size"], ro=False)
        for change, success in [({}, True), ({"model": "Samsung SSD 850 EVO 500GB"}, False),
                                ({"serial": "different"}, False), ({"size": 500107862016}, False),
                                ({"type": "part"}, False), ({"ro": True}, False)]:
            self.run_tasks([guard], success=success, nas_storage_layouts={"results": [{
                "item": DISKS[0], "stdout": json.dumps({"blockdevices": [dict(original, **change)]}),
            }]})

    def test_raw_device_letters_and_partition_member_paths_are_rejected(self):
        guard = task("manage", "Require stable whole-disk identities and expected hardware")
        for path, success in [(DISKS[0]["path"], True), ("/dev/sdb", False),
                               (DISKS[0]["path"] + "-part1", False)]:
            self.run_tasks([guard], success=success,
                           nas_storage_disks=[dict(DISKS[0], path=path)])

    def test_existing_files_and_mount_path_aliases_are_rejected(self):
        guard = task("manage", "Refuse a symlink or non-directory mount path")
        for canonical, stat, success in [
            ("/srv/storage", {"exists": False}, True),
            ("/srv/storage", {"exists": True, "isdir": True, "islnk": False}, True),
            ("/other", {"exists": True, "isdir": False, "islnk": True}, False),
        ]:
            self.run_tasks([guard], success=success,
                           nas_storage_canonical_mount={"stdout": canonical}, nas_storage_directory={"stat": stat})
        files = task("manage", "Refuse to hide existing files below a new mount")
        self.run_tasks([files], success=False, nas_storage_directory_entries={"matched": 1},
                       nas_storage_directory={"stat": {"exists": True}}, nas_storage_mount={"rc": 1})

    def test_initial_layout_refuses_existing_raid_ext4_and_extra_partitions(self):
        guard = task("initialize", "Restrict initial layout to empty disks or one old NTFS partition")
        disk = {"type": "disk", "fstype": None}
        part = {"type": "part", "fstype": "ntfs"}
        for nodes, success in [([disk], True), ([disk, part], True),
                               ([dict(disk, fstype="linux_raid_member")], False),
                               ([dict(disk, fstype="ext4")], False),
                               ([disk, dict(part, fstype="ext4")], False),
                               ([disk, part, part], False)]:
            self.run_tasks([guard], success=success, nas_storage_layouts={"results": [{
                "item": DISKS[0], "stdout": json.dumps({"blockdevices": nodes}),
            }]})

    def test_mount_swap_holders_and_signatures_block_erasure(self):
        mount_guard = task("initialize", "Refuse mounted filesystems or active swap on either disk")
        for mounts, success in [([], True), ([None], True), (["/"], False), (["[SWAP]"], False)]:
            self.run_tasks([mount_guard], success=success,
                           nas_storage_initial_nodes=[{"name": "/dev/test", "mountpoints": mounts}])
        holders_guard = task("initialize", "Refuse disks or partitions in use by another block device")
        for holders, success in [("", True), ("md127", False), ("dm-0", False)]:
            self.run_tasks([holders_guard], success=success, nas_storage_holders={"results": [{
                "item": {"name": "/dev/test"}, "stdout": holders,
            }]})
        signature_guard = task("initialize", "Refuse RAID, ext4, LVM, encryption, or any unapproved signature")
        for signatures, success in [([], True), (["gpt", "PMBR"], True), (["ntfs"], True),
                                    (["linux_raid_member"], False), (["ext4"], False),
                                    (["LVM2_member"], False), (["crypto_LUKS"], False)]:
            self.run_tasks([signature_guard], success=success, nas_storage_initial_signatures={"results": [{
                "item": {"name": "/dev/test"},
                "stdout": json.dumps({"signatures": [{"type": s} for s in signatures]}),
            }]})

    def test_fstab_ownership_is_checked_before_initialization(self):
        guard = task("manage", "Refuse an existing fstab entry for another filesystem at /srv/storage")
        for fstab, initialized, success in [
            ("UUID=os / ext4 defaults 0 1\n", False, True),
            ("UUID=old /srv/storage ext4 defaults 0 2\n", False, False),
            (f"UUID={FS_UUID} /srv/storage ext4 defaults 0 2\n", True, True),
            ("UUID=old /srv/storage ext4 defaults 0 2\n", True, False),
        ]:
            self.run_tasks([guard], success=success,
                           nas_storage_record={"stat": {"exists": initialized}},
                           nas_storage_identity=IDENTITY, nas_storage_fstab=encoded(fstab))
        old_guard = task("initialize", "Refuse initial devices still referenced by fstab")
        node = {"name": "/dev/test1", "uuid": "OLD", "label": None}
        self.run_tasks([old_guard], success=False,
                       nas_storage_initial_nodes=[node], nas_storage_fstab=encoded("UUID=OLD /media/old ntfs defaults 0 0\n"))

    def test_existing_members_and_active_array_must_match_recorded_uuid(self):
        guard = task("maintain", "Require both disks to belong to the recorded two-disk RAID 1")
        for uuid, level, count, success in [(ARRAY_UUID, "raid1", 2, True),
                                           ("another", "raid1", 2, False),
                                           (ARRAY_UUID, "raid0", 2, False),
                                           (ARRAY_UUID, "raid1", 1, False)]:
            self.run_tasks([guard], success=success, nas_storage_identity=IDENTITY,
                           nas_storage_member_metadata={"results": [{"item": DISKS[0], "stdout_lines": [
                               f"MD_UUID={uuid}", f"MD_LEVEL={level}", f"MD_DEVICES={count}",
                           ]}]})

    def test_reserved_array_name_and_conflicting_configuration_are_rejected(self):
        initial_guard = task("manage", "Refuse an already reserved array name before erasing any disk")
        routine_guard = task("maintain", "Refuse a conflicting named-array configuration")
        for config, first_success, routine_success in [
            ("MAILADDR root\nARRAY /dev/md/other UUID=other\n", True, True),
            ("ARRAY /dev/md/storage UUID=foreign\n", False, False),
            (f"  ARRAY /dev/md/storage UUID={ARRAY_UUID}\n", False, True),
        ]:
            self.run_tasks([initial_guard], success=first_success,
                           nas_storage_record={"stat": {"exists": False}},
                           nas_storage_mdadm_config_stat={"stat": {"exists": True}},
                           nas_storage_initial_mdadm_config=encoded(config))
            self.run_tasks([routine_guard], success=routine_success,
                           nas_storage_identity=IDENTITY, nas_storage_mdadm_config=encoded(config))

    def test_degraded_array_is_failure_not_a_reinitialization_trigger(self):
        guard = task("maintain", "Report a degraded array as a failure, never as a reason to initialize")
        for degraded, success in [(0, True), (1, False), (2, False)]:
            self.run_tasks([guard], success=success, nas_storage_initialize=True,
                           nas_storage_raid_state={"stdout_lines": [str(degraded), "idle"]})

    def test_filesystem_and_mount_uuid_cannot_fall_back_to_os_disk(self):
        fs_guard = task("maintain", "Require the recorded ext4 filesystem")
        mount_guard = task("maintain", "Require the expected mounted filesystem rather than an OS-disk fallback")
        for uuid, fstype, success in [(FS_UUID, "ext4", True), ("os-uuid", "ext4", False),
                                     (FS_UUID, "ntfs", False)]:
            self.run_tasks([fs_guard], success=success, nas_storage_identity=IDENTITY,
                           nas_storage_filesystem={"stdout_lines": [f"UUID={uuid}", f"TYPE={fstype}"]})
            self.run_tasks([mount_guard], success=success, nas_storage_identity=IDENTITY,
                           nas_storage_verified_mount={"stdout": json.dumps({"filesystems": [{
                               "uuid": uuid, "fstype": fstype, "target": "/srv/storage", "options": "rw,relatime",
                           }]})}, nas_storage_mount={"rc": 0})

        self.run_tasks([mount_guard], success=False, nas_storage_identity=IDENTITY,
                       nas_storage_verified_mount={"stdout": json.dumps({"filesystems": [{
                           "uuid": FS_UUID, "fstype": "ext4", "target": "/srv/storage", "options": "ro,relatime",
                       }]})}, nas_storage_mount={"rc": 0})

    def test_destructive_commands_are_only_in_guarded_first_initialization(self):
        manage = yaml.safe_load((ROLE / "tasks/manage.yml").read_text())
        init_import = next(t for t in manage if t.get("ansible.builtin.import_tasks") == "initialize.yml")
        self.assertEqual(init_import["when"], "not nas_storage_record.stat.exists")
        routine_tasks = yaml.safe_load((ROLE / "tasks/maintain.yml").read_text())
        self.assertEqual(routine_tasks[0]["register"], "nas_storage_array")
        self.assertEqual(routine_tasks[0]["ansible.builtin.stat"]["path"], "/dev/md/storage")
        init = yaml.safe_load((ROLE / "tasks/initialize.yml").read_text())
        block = init[-1]
        self.assertEqual(block["when"], "not ansible_check_mode")
        names = [t["name"] for t in block["block"]]
        self.assertLess(names.index("Record initialization in progress before the first destructive operation"),
                        names.index("Erase only the inspected old NTFS partition signatures"))
        routine = (ROLE / "tasks/maintain.yml").read_text()
        for forbidden in ["--create", "wipefs", "mkfs.ext4", "--force", "--assume-clean", "--add", "--remove"]:
            self.assertNotIn(forbidden, routine)
        for t in block["block"]:
            argv = t.get("ansible.builtin.command", {}).get("argv", [])
            if isinstance(argv, list) and argv and argv[0] in ["wipefs", "mkfs.ext4"]:
                self.assertNotIn("/dev/sd", str(argv))
                self.assertNotIn("Samsung", str(argv))

    def test_mdadm_config_edit_is_idempotent_and_preserves_other_arrays(self):
        edit = copy.deepcopy(task("maintain", "Persist assembly of only this array by its recorded UUID"))
        edit.pop("notify")
        with tempfile.TemporaryDirectory(
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        ) as directory:
            path = Path(directory) / "mdadm.conf"
            original = "# keep local settings\nMAILADDR root\nARRAY /dev/md/other UUID=other\n"
            path.write_text(original)
            edit["ansible.builtin.lineinfile"]["path"] = str(path)
            for attempt in range(2):
                result = self.run_tasks([edit], nas_storage_identity=IDENTITY)
                if attempt:
                    self.assertIn("changed=0", result.stdout)
            self.assertEqual(path.read_text(), original + f"ARRAY /dev/md/storage UUID={ARRAY_UUID}\n")

    def test_exact_destructive_argv_renders_using_only_stable_member_ids(self):
        init = yaml.safe_load((ROLE / "tasks/initialize.yml").read_text())[-1]["block"]
        preview = []
        for entry in init:
            if entry["name"] in ["Erase only the inspected old NTFS partition signatures",
                                 "Erase partition-table signatures on only the two approved whole disks",
                                 "Create the new two-disk RAID 1 mirror without force or assume-clean",
                                 "Create ext4 only on the array just created by this initialization"]:
                # Render production arguments, but NEVER invoke a command.
                entry = copy.deepcopy(entry)
                argv = entry.pop("ansible.builtin.command")["argv"]
                entry["ansible.builtin.debug"] = {"msg": argv}
                preview.append(entry)
        result = self.run_tasks(preview, nas_storage_old_partitions={"results": [
            {"item": {"item": disk}, "stdout": "/dev/test1"} for disk in DISKS
        ]})
        self.assertIn(DISKS[0]["path"] + "-part1", result.stdout)
        self.assertIn(DISKS[1]["path"], result.stdout)
        self.assertNotIn("/dev/sda", result.stdout)
        self.assertNotIn(HOST["nas_storage_protected_disk"], result.stdout)
        self.assertEqual(len(preview), 4)

    def test_mount_is_uuid_based_and_missing_storage_does_not_block_boot(self):
        mount = task("maintain", "Persist and mount only the verified filesystem by UUID")["ansible.posix.mount"]
        self.assertEqual(mount["path"], "/srv/storage")
        self.assertEqual(mount["src"], "UUID={{ nas_storage_identity.filesystem_uuid }}")
        self.assertEqual(mount["fstype"], "ext4")
        self.assertIn("nofail", mount["opts"])
        self.assertFalse(HOST.get("nas_storage_initialize", False))
