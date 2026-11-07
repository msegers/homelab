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

    def test_real_mount_probe_and_uuid_guard_prevent_directory_creation(self):
        if not shutil.which("findmnt"):
            self.skipTest("findmnt unavailable")
        source = yaml.safe_load((ROLES / "nas_storage/tasks/verify_mount.yml").read_text())
        for missing in [True, False]:
            with self.subTest(missing=missing), self.temporary() as temporary:
                directory = Path(temporary)
                tasks = yaml.safe_load(yaml.safe_dump(source))
                # Inspect only: no mount/umount command. '/' is an actual mount
                # but never our test RAID UUID; the temporary path isn't mounted.
                tasks[1]["ansible.builtin.command"]["argv"][3] = str(directory) if missing else "/"
                tasks.append({"ansible.builtin.file": {"path": str(directory / "unsafe-fallback"), "state": "directory"}})
                self.run_play(directory, tasks, {"nas_storage_expected_uuid": UUID}, success=False)
                self.assertFalse((directory / "unsafe-fallback").exists())

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
                    "samba_config_managed": contents.startswith("# Managed by Ansible homelab samba\n"),
                    "samba_stock_config": {"stat": {"exists": False}},
                    "samba_active": {"rc": 3}}, success)

    def samba_inspection(self, directory):
        tasks = yaml.safe_load((ROLES / "samba/tasks/inspect.yml").read_text())
        tasks[0]["ansible.builtin.stat"]["path"] = str(directory / "smb.conf")
        tasks[1]["ansible.builtin.slurp"]["src"] = str(directory / "smb.conf")
        return tasks

    def test_samba_first_install_partial_recovery_and_repeat_apply(self):
        guard = named("samba", "configure", "Refuse to replace unmanaged Samba configuration")
        stock = named("samba", "configure", "Inspect Debian's package-supplied Samba configuration template")
        render = named("samba", "configure", "Render the private SMB configuration")
        render.pop("notify")
        render["ansible.builtin.template"].update(
            src=str(ROLES / "samba/templates/smb.conf.j2"), owner=str(os.getuid()), group=str(os.getgid()))
        # testparm is independently exercised when available; this fixture also
        # runs on controllers without Samba installed and never installs packages.
        render["ansible.builtin.template"].pop("validate")
        variables = yaml.safe_load((ROLES / "samba/defaults/main.yml").read_text())
        variables["samba_active"] = {"rc": 3}
        with self.temporary() as temporary:
            directory = Path(temporary)
            stock_path = directory / "package-stock.conf"
            stock_path.write_text("# Debian package template fixture\n[global]\nworkgroup = WORKGROUP\n")
            stock["ansible.builtin.stat"]["path"] = str(stock_path)
            render["ansible.builtin.template"]["dest"] = str(directory / "smb.conf")
            preflight = self.samba_inspection(directory) + [stock, guard]
            # Fresh host has no smb.conf; package installation would create it.
            self.run_play(directory, preflight, variables)
            # Simulate an interruption after apt installed only its stock file.
            (directory / "smb.conf").write_bytes(stock_path.read_bytes())
            self.run_play(directory, preflight + [render], variables)
            self.assertTrue((directory / "smb.conf").read_text().startswith("# Managed by Ansible homelab samba\n"))
            # The managed service is now active; repeat apply must be unchanged.
            variables["samba_active"] = {"rc": 0}
            result = self.run_play(directory, preflight + [render], variables)
            self.assertIn("changed=0", result.stdout)

    def test_samba_stock_adoption_requires_exact_bytes_and_inactive_service(self):
        guard = named("samba", "configure", "Refuse to replace unmanaged Samba configuration")
        for checksum, active, regular, success in [("stock", 3, True, True), ("stock", 0, True, False),
            ("custom", 3, True, False), ("stock", 3, False, False)]:
            with self.subTest(checksum=checksum, active=active, regular=regular), self.temporary() as temporary:
                self.run_play(Path(temporary), [guard], {
                    "samba_existing_config": {"stat": {"exists": True, "isreg": regular, "islnk": not regular, "checksum": checksum}},
                    "samba_stock_config": {"stat": {"isreg": True, "islnk": False, "checksum": "stock"}},
                    "samba_config_managed": False, "samba_active": {"rc": active}}, success)

    def test_samba_disable_only_stops_managed_service_without_password_or_mount(self):
        self.assertEqual(len(yaml.safe_load((ROLES / "samba/tasks/disable.yml").read_text())), 1)
        disable = named("samba", "disable", "Stop and disable only the managed private Samba service")
        self.assertEqual(disable["ansible.builtin.systemd_service"],
                         {"name": "smbd.service", "state": "stopped", "enabled": False})
        # Mock only the mutating module, not the ownership inspection or guards.
        options = disable.pop("ansible.builtin.systemd_service")
        disable["ansible.builtin.debug"] = {"msg": options}
        main = yaml.safe_load((ROLES / "samba/tasks/main.yml").read_text())
        self.assertEqual(main[1]["when"], "not samba_enabled | bool")
        disable["when"] = [disable["when"], main[1]["when"]]
        for content, enabled, stopped in [(None, False, False), ("[custom]\n", False, False),
            ("# Managed by Ansible homelab samba\n", False, True),
            ("# Managed by Ansible homelab samba\n", True, False),
            ("# unrelated comment: # Managed by Ansible homelab samba\n", False, False)]:
            with self.subTest(content=content, enabled=enabled), self.temporary() as temporary:
                directory = Path(temporary)
                if content is not None:
                    (directory / "smb.conf").write_text(content)
                tasks = self.samba_inspection(directory) + [disable]
                for _ in range(2):
                    result = self.run_play(directory, tasks, {"samba_enabled": enabled})
                    self.assertEqual('"name": "smbd.service"' in result.stdout, stopped)
                if content is not None:
                    self.assertEqual((directory / "smb.conf").read_text(), content)
        with self.temporary() as temporary:
            directory = Path(temporary)
            target = directory / "unmanaged-symlink-target.conf"
            target.write_text("# Managed by Ansible homelab samba\n")
            (directory / "smb.conf").symlink_to(target)
            result = self.run_play(directory, self.samba_inspection(directory) + [disable], {"samba_enabled": False})
            self.assertNotIn('"name": "smbd.service"', result.stdout)
            self.assertTrue((directory / "smb.conf").is_symlink())

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

    def test_immich_directory_initialization_and_repeat_preserve_container_metadata(self):
        root_dirs = named("immich", "configure", "Create private Immich project and state directories")
        inspect = named("immich", "configure", "Inspect container-owned SSD directories")
        create = named("immich", "configure", "Create only missing container-owned directories")
        for task in [root_dirs, create]:
            task["ansible.builtin.file"].update(owner=str(os.getuid()), group=str(os.getgid()))
            self.assertNotIn("recurse", task["ansible.builtin.file"])
        with self.temporary() as temporary:
            directory = Path(temporary)
            variables = {"immich_project_dir": str(directory / "project"), "immich_state_dir": str(directory / "state"),
                         "immich_media_dir": str(directory / "media"), "immich_backup_dir": str(directory / "backup")}
            tasks = [root_dirs, inspect, create]
            self.run_play(directory, tasks, variables)
            before = {}
            for name in ["postgres", "redis"]:
                data = directory / "state" / name
                self.assertEqual(data.stat().st_mode & 0o777, 0o700)
                # Stand in for image-managed metadata without privileged chown.
                data.chmod(0o750)
                marker = data / "existing-data"
                marker.write_text("do not modify")
                marker.chmod(0o600)
                before[name] = (data.stat(), marker.stat())
            result = self.run_play(directory, tasks, variables)
            self.assertIn("changed=0", result.stdout)
            for name, (data_before, marker_before) in before.items():
                data = directory / "state" / name
                marker = data / "existing-data"
                for old, current in [(data_before, data.stat()), (marker_before, marker.stat())]:
                    self.assertEqual((old.st_uid, old.st_gid, old.st_mode, old.st_ino),
                                     (current.st_uid, current.st_gid, current.st_mode, current.st_ino))
                self.assertEqual(marker.read_text(), "do not modify")

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
        self.assertEqual(services["immich-server"]["depends_on"], {
            "redis": {"condition": "service_healthy"}, "database": {"condition": "service_healthy"}})

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
        self.assertIn("--abort-on-container-exit", units[1])
        self.assertIn("Restart=always", units[1])
        self.assertIn("StartLimitIntervalSec=300", units[1])
        self.assertIn("StartLimitBurst=3", units[1])
        self.assertIn("RestartSec=15", units[1])
        self.assertIn("ExecCondition=/usr/bin/findmnt", units[1])
        self.assertNotIn("ExecStartPre=", units[1])
        self.assertIn("PartOf=docker.service", units[1])
        self.assertIn("up --timeout 120", units[1])
        wifi = self.render("samba", ["storage.conf"], samba_network_ready_unit="ifup@wlp0s20f3.service")["storage.conf"]
        self.assertIn("Requires=ifup@wlp0s20f3.service", wifi)
        self.assertIn("After=ifup@wlp0s20f3.service", wifi)

    def test_immich_start_condition_rejects_unmounted_path_without_launching_compose(self):
        source = self.render("immich", ["immich.service"])["immich.service"]
        condition = next(line.removeprefix("ExecCondition=") for line in source.splitlines()
                         if line.startswith("ExecCondition="))
        with self.temporary() as temporary:
            argv = shlex.split(condition.replace("/srv/storage", temporary))
            result = subprocess.run(argv, capture_output=True, text=True)
            # systemd documents 1..254 as a skipped start, not a restart failure.
            self.assertEqual(result.returncode, 1, result.stderr)

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
