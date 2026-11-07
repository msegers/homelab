"""Local Ansible/template regressions; never start services or touch real storage."""

import json
import base64
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "ansible/roles"
UUID = "3b5ea8e0-2a29-42b5-bf10-84323fd768f1"


def named(role, file, name):
    return next(task for task in yaml.safe_load((ROLES / role / f"tasks/{file}.yml").read_text())
                if task["name"] == name)


class StorageServiceTests(unittest.TestCase):
    def run_play(self, directory, tasks, variables, success=True):
        path = directory / "play.yml"
        path.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
                                         "vars": variables, "tasks": tasks}]))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def temporary(self):
        return tempfile.TemporaryDirectory(dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)

    def render(self, role, templates, **overrides):
        variables = yaml.safe_load((ROLES / role / "defaults/main.yml").read_text())
        variables.update(nas_storage_expected_uuid=UUID, immich_db_password="fixturepassword123456789012345",
                         samba_bind_ip="192.168.68.111", samba_allowed_networks=["192.168.68.0/24"])
        variables.update(overrides)
        with self.temporary() as temporary:
            directory = Path(temporary)
            tasks = [{"ansible.builtin.template": {"src": str(ROLES / role / f"templates/{name}.j2"),
                                                   "dest": str(directory / name)}} for name in templates]
            self.run_play(directory, tasks, variables)
            rendered = {name: (directory / name).read_text() for name in templates}
            if role == "immich" and "compose.yaml" in templates:
                (directory / ".env").write_text(rendered["env"])
                command = shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose"))
                available = shutil.which(command[0]) and subprocess.run(command + ["version"], capture_output=True).returncode == 0
                if available:
                    checked = subprocess.run(command + ["-f", str(directory / "compose.yaml"), "config", "--format", "json"],
                                             cwd=directory, capture_output=True, text=True)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    parsed = json.loads(checked.stdout)
                    self.assertEqual(parsed["services"]["database"]["environment"]["POSTGRES_PASSWORD"], variables["immich_db_password"])
            return rendered

    def test_consumer_mount_guard_rejects_ssd_wrong_fs_and_readonly(self):
        guard = named("nas_storage", "verify_mount", "Refuse consumer writes to missing foreign or read-only storage")
        for change, success in [({}, True), ({"uuid": "ssd"}, False), ({"target": "/"}, False),
                                ({"fstype": "nfs"}, False), ({"options": "ro,relatime"}, False)]:
            with self.subTest(change=change), self.temporary() as temporary:
                mounted = dict(uuid=UUID, target="/srv/storage", fstype="ext4", options="rw,relatime")
                mounted.update(change)
                self.run_play(Path(temporary), [guard], {"nas_storage_expected_uuid": UUID,
                    "storage_consumer_mount": {"stdout": json.dumps({"filesystems": [mounted]})}}, success)

    def test_mount_preflights_are_static_so_service_tags_cannot_skip_them(self):
        for role, name in [("samba", "Verify storage before configuring Samba"),
                           ("immich", "Verify RAID before provisioning any media or backup paths")]:
            self.assertEqual(named(role, "configure", name)["ansible.builtin.import_role"],
                             {"name": "nas_storage", "tasks_from": "verify_mount"})

    def test_samba_exports_only_shared_and_requires_authentication(self):
        rendered = self.render("samba", ["smb.conf", "storage.conf"])
        source = rendered["smb.conf"]
        self.assertEqual([line for line in source.splitlines() if line.startswith("[")], ["[global]", "[shared]"])
        for setting in ["path = /srv/storage/shared", "guest ok = no", "map to guest = Never",
                        "valid users = shared", "hosts deny = ALL", "hosts allow = 192.168.68.0/24",
                        "interfaces = 192.168.68.111", "bind interfaces only = yes", "smb ports = 445",
                        "create mask = 0660", "force directory mode = 2770", "follow symlinks = no"]:
            self.assertIn(setting, source)
        self.assertNotIn("0777", source)
        command = shlex.split(os.environ.get("TESTPARM_COMMAND", "testparm"))
        if shutil.which(command[0]):
            with self.temporary() as temporary:
                config = Path(temporary) / "smb.conf"
                config.write_text(source)
                result = subprocess.run(command + ["--suppress-prompt", str(config)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unmanaged_samba_configuration_is_not_overwritten(self):
        guard = named("samba", "configure", "Refuse to replace unmanaged Samba configuration")
        for contents, success in [("[old-share]\npath=/srv\n", False), ("# Managed by Ansible homelab samba\n", True)]:
            with self.temporary() as temporary:
                self.run_play(Path(temporary), [guard], {
                    "samba_existing_config": {"stat": {"exists": True, "isreg": True, "islnk": False}},
                    "samba_existing_content": {"content": base64.b64encode(contents.encode()).decode()}}, success)

    def test_samba_does_not_take_over_interactive_or_shared_accounts(self):
        guard = named("samba", "configure", "Refuse takeover of an interactive privileged or shared account")
        account = ["x", "991", "991", "", "/nonexistent", "/usr/sbin/nologin"]
        group = ["x", "991", ""]
        for existing, members, success in [(None, None, True), (account, group, True),
            (account[:-1] + ["/bin/bash"], group, False),
            (account, ["x", "991", "another-user"], False)]:
            with self.subTest(success=success), self.temporary() as temporary:
                self.run_play(Path(temporary), [guard], {
                    "samba_user": "shared", "samba_group": "shared",
                    "samba_existing_user": {"ansible_facts": {"getent_passwd": {"shared": existing}}},
                    "samba_existing_group": {"ansible_facts": {"getent_group": {"shared": members}}},
                }, success)

    def test_storage_directory_creation_is_idempotent_and_never_recursive(self):
        create = named("nas_storage", "directories", "Create missing bulk storage directories")
        self.assertNotIn("recurse", create["ansible.builtin.file"])
        create["ansible.builtin.file"].update(owner=str(os.getuid()), group=str(os.getgid()))
        with self.temporary() as temporary:
            directory = Path(temporary)
            create["ansible.builtin.file"]["path"] = str(directory / "{{ item.item }}")
            variables = {"storage_bulk_paths": {"results": [
                {"item": name, "stat": {"exists": False}} for name in ["shared", "immich", "valheim", "backups"]]}}
            self.run_play(directory, [create], variables)
            result = self.run_play(directory, [create], variables)
            self.assertIn("changed=0", result.stdout)
            for name in ["shared", "immich", "valheim", "backups"]:
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o700)

    def test_repeat_provisioning_preserves_postgres_and_valkey_ownership(self):
        root_dirs = named("immich", "configure", "Create private Immich project and state directories")
        self.assertNotIn("{{ immich_state_dir }}/postgres", root_dirs["loop"])
        self.assertNotIn("{{ immich_state_dir }}/redis", root_dirs["loop"])
        create = named("immich", "configure", "Create only missing container-owned directories")
        self.assertEqual(create["when"], "not item.stat.exists")

    def test_immich_release_storage_isolation_and_memory(self):
        rendered = self.render("immich", ["compose.yaml", "env", "immich.service"])
        compose = yaml.safe_load(rendered["compose.yaml"])
        services = compose["services"]
        self.assertEqual(set(services), {"immich-server", "immich-machine-learning", "database", "redis"})
        self.assertIn("IMMICH_VERSION=v3.3.6", rendered["env"])
        self.assertIn("UPLOAD_LOCATION=/srv/storage/immich", rendered["env"])
        self.assertIn("DB_DATA_LOCATION=/var/lib/homelab-immich/postgres", rendered["env"])
        for service in services.values():
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("ports", service)
            for volume in service.get("volumes", []):
                if isinstance(volume, dict):
                    self.assertFalse(volume["bind"]["create_host_path"])
                    self.assertNotIn("docker.sock", volume["source"])
        self.assertEqual(services["database"]["mem_limit"], "2g")
        self.assertEqual(compose["networks"]["proxy"], {"external": True, "name": "homelab_default"})
        self.assertNotIn("networks", services["database"])
        self.assertNotIn("networks", services["redis"])
        self.assertIn("homelab-immich", services["immich-server"]["networks"]["proxy"]["aliases"])

    def test_immich_rejects_foreign_nested_mounts_and_network_database(self):
        guard = named("immich", "configure", "Refuse nested foreign mounts or a database on unsupported storage")
        ssd = "11111111-1111-1111-1111-111111111111"
        for path, uuid, fstype, success in [
            ("/var/lib/homelab-immich/postgres", ssd, "ext4", True),
            ("/var/lib/homelab-immich/postgres", UUID, "ext4", False),
            ("/var/lib/homelab-immich/postgres", ssd, "nfs", False),
            ("/srv/storage/immich", UUID, "ext4", True),
            ("/srv/storage/immich", ssd, "ext4", False),
        ]:
            with self.subTest(path=path, uuid=uuid, fstype=fstype), self.temporary() as temporary:
                mounted = dict(uuid=uuid, fstype=fstype, options="rw,relatime")
                self.run_play(Path(temporary), [guard], {
                    "nas_storage_expected_uuid": UUID,
                    "immich_ssd_mount": {"stdout": json.dumps({"filesystems": [{"uuid": ssd}]})},
                    "immich_actual_filesystems": {"results": [{"stdout": json.dumps({"filesystems": [mounted]}),
                                                               "item": {"item": path}}]},
                }, success)

    def test_immich_refuses_unmanaged_files_and_silent_database_password_rotation(self):
        guard = named("immich", "configure", "Refuse to overwrite unmanaged Immich configuration or silently rotate PostgreSQL credentials")
        for content, regular, success in [
            ("# Managed by Ansible\nDB_PASSWORD=original\n", True, True),
            ("# Managed by Ansible\nDB_PASSWORD=changed\n", True, False),
            ("DB_PASSWORD=original\n", True, False),
            ("# Managed by Ansible\nDB_PASSWORD=original\n", False, False),
        ]:
            with self.subTest(regular=regular, success=success), self.temporary() as temporary:
                result = self.run_play(Path(temporary), [guard], {
                    "immich_db_password": "original",
                    "immich_existing_content": {"results": [{
                        "content": base64.b64encode(content.encode()).decode(),
                        "item": {"item": ".env", "stat": {"isreg": regular, "islnk": not regular}},
                    }]},
                }, success)
                self.assertNotIn("original", result.stdout + result.stderr)

    def test_units_bind_lifetime_to_exact_uuid_and_do_not_gate_docker_globally(self):
        units = [self.render("samba", ["storage.conf"])["storage.conf"],
                 self.render("immich", ["immich.service"])["immich.service"]]
        for unit in units:
            self.assertIn("RequiresMountsFor=/srv/storage", unit)
            self.assertIn("BindsTo=srv-storage.mount", unit)
            self.assertIn("--source UUID=" + UUID, unit)
            self.assertIn("--types ext4 --options rw", unit)
        self.assertNotIn("--volumes", units[1])
        self.assertIn("--abort-on-container-failure", units[1])
        wifi = self.render("samba", ["storage.conf"], samba_network_ready_unit="ifup@wlp0s20f3.service")["storage.conf"]
        self.assertIn("Requires=ifup@wlp0s20f3.service", wifi)
        self.assertIn("After=ifup@wlp0s20f3.service", wifi)

    def test_independent_disable_preserves_all_data(self):
        tasks = yaml.safe_load((ROLES / "immich/tasks/main.yml").read_text())
        disable = tasks[1]
        self.assertEqual(disable["ansible.builtin.systemd_service"]["state"], "stopped")
        self.assertFalse(disable["ansible.builtin.systemd_service"]["enabled"])
        self.assertFalse(any("ansible.builtin.file" in task for task in tasks))

    def test_systemd_parses_storage_dependencies_and_lifecycle(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze unavailable")
        immich = self.render("immich", ["immich.service"])["immich.service"]
        samba = self.render("samba", ["storage.conf"])["storage.conf"]
        with self.temporary() as temporary:
            root = Path(temporary)
            (root / "homelab-immich.service").write_text(immich)
            (root / "smbd.service").write_text("[Service]\nExecStart=/usr/bin/true\n" + samba)
            (root / "docker.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
            (root / "srv-storage.mount").write_text("[Mount]\nWhat=/dev/null\nWhere=/srv/storage\nType=ext4\n")
            verified = subprocess.run(["systemd-analyze", "verify", "--man=no",
                str(root / "homelab-immich.service"), str(root / "smbd.service"),
                str(root / "docker.service"), str(root / "srv-storage.mount")],
                capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_valheim_only_backup_destination_moves_and_runtime_guards_are_rendered(self):
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        self.assertTrue(host["valheim_backups_enabled"])
        self.assertEqual(host["valheim_backups_dir"], "/srv/storage/backups/valheim")
        self.assertNotIn("valheim_worlds_dir", host)
        self.assertEqual(host["valheim_required_mount_uuids"], {"/srv/storage": "{{ nas_storage_expected_uuid }}"})


if __name__ == "__main__":
    unittest.main()
