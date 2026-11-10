"""Ansible-only regression checks. Runtime behavior is tested by cargo test."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/valheim"


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="valheim-ansible-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.password = "S3cret-${literal}-$money-'quote'-back\\slash#"
        self.variables = {
            "ansible_architecture": "x86_64",
            "ansible_all_ipv4_addresses": ["192.168.68.111"],
            "ansible_python_interpreter": sys.executable,
            "valheim_enabled": True,
            "valheim_home": str(self.root / "home"),
            "valheim_project_dir": str(self.project),
            "valheim_game_bind_ip": "192.168.68.111",
            "valheim_game_account": {"uid": os.getuid() or 1000, "group": os.getgid() or 1000},
            "vault_valheim_server_password": self.password,
        }

    def run_role(self, tasks_from="provision", expect_success=True):
        variables = self.variables
        tasks = [{"ansible.builtin.include_role": {"name": "valheim", "tasks_from": tasks_from}}]
        if tasks_from == "dashboard_render":
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text()) | self.variables
            tasks = [{"ansible.builtin.template": {
                "src": str(ROLE / "templates" / "{{ item }}.j2"),
                "dest": str(self.root / "{{ item }}"),
            }, "loop": ["valheim-dashboard.service", "valheim-dashboard-broker.service", "valheim-idle.service"]}]
        if tasks_from == "dashboard_proxy":
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text()) | self.variables
            tasks = yaml.safe_load((ROLE / "tasks/dashboard_proxy.yml").read_text())
            inspection = tasks[1]["block"][0]
            self.assertTrue(inspection["no_log"])
            self.assertEqual(inspection["community.docker.docker_container_info"]["name"], "{{ valheim_dashboard_proxy_container }}")
            tasks[1]["block"][0] = {"ansible.builtin.set_fact": {
                "valheim_dashboard_proxy_info": variables.get("proxy_fixture", {})}}
            tasks.append({"ansible.builtin.template": {
                "src": str(ROLE / "templates/valheim-dashboard.service.j2"),
                "dest": str(self.root / "valheim-dashboard.service")}})
        if tasks_from in ["provision", "account_preflight", "account_sudo"]:
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text()) | self.variables
            source = "account.yml" if tasks_from.startswith("account_") else "provision.yml"
            tasks = yaml.safe_load((ROLE / "tasks" / source).read_text())
            if tasks_from == "account_preflight":
                tasks = [task for task in tasks if "ansible.builtin.assert" in task]
            elif tasks_from == "account_sudo":
                tasks = [task for task in tasks if task.get("register") == "valheim_sudo_check"]
                tasks[0]["ansible.builtin.command"]["argv"][0] = str(self.root / "sudo")
            # A failing validation regression must never chmod/chown a host root.
            # Only paths in this test's temporary directory may be mutated.
            for task in tasks:
                for module in ["ansible.builtin.file", "ansible.builtin.template"]:
                    if module in task:
                        task.setdefault("vars", {})["valheim_test_mutation_path"] = task[module].get("path", task[module].get("dest"))
                        task["when"] = f"valheim_test_mutation_path is match('^{self.root}/')"
                        for key, value in [("owner", os.getuid()), ("group", os.getgid())]:
                            if task[module].get(key) == "root":
                                task[module][key] = str(value)
                if "ansible.builtin.template" in task:
                    task["ansible.builtin.template"]["src"] = str(ROLE / "templates" / task["ansible.builtin.template"]["src"])
        path = self.root / "play.yml"
        path.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": tasks}]))
        command = ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)]
        if tasks_from == "main":
            command.append("--check")
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True,
                                env=dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1"))
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)

    def config(self):
        return json.loads((self.root / "home/control/config.json").read_text())

    def test_dashboard_proxy_discovery_authorizes_exact_private_peers_only(self):
        self.variables.update(valheim_dashboard_proxy_container="homelab-proxy-1", proxy_fixture={
            "exists": True, "container": {"State": {"Running": True}, "NetworkSettings": {"Networks": {
                "private": {"IPAddress": "172.18.0.2"}, "other": {"IPAddress": "10.9.0.3"},
                "public": {"IPAddress": "8.8.8.8"}, "ipv6_only": {"IPAddress": ""}}}}})
        self.run_role("dashboard_proxy")
        unit = (self.root / "valheim-dashboard.service").read_text()
        self.assertIn("--trusted-proxy 172.18.0.2/32", unit)
        self.assertIn("--trusted-proxy 10.9.0.3/32", unit)
        self.assertNotIn("8.8.8.8", unit)
        self.assertNotIn("--allow-network 172.18", unit)
        self.variables["valheim_dashboard_proxy_container"] = ""
        self.run_role("dashboard_proxy")
        self.assertNotIn("--trusted-proxy", (self.root / "valheim-dashboard.service").read_text())

    def test_dashboard_proxy_discovery_fails_closed(self):
        self.variables["valheim_dashboard_proxy_container"] = "homelab-proxy-1"
        for fixture in [{"exists": False},
                        {"exists": True, "container": {"State": {"Running": False}}},
                        {"exists": True, "container": {"State": {"Running": True}, "NetworkSettings": {
                            "Networks": {"public": {"IPAddress": "8.8.8.8"}}}}}]:
            with self.subTest(fixture=fixture):
                self.variables["proxy_fixture"] = fixture
                self.run_role("dashboard_proxy", expect_success=False)

    def test_sudo_check_requires_explicit_denial_regardless_of_exit_status(self):
        denial = "User valheim-game is not allowed to run sudo on homelab.\n"
        cases = [(0, denial, "", True), (1, denial, "", True), (1, "", denial, True),
                 (0, "User valheim-game may run the following commands on homelab:\n    (ALL) ALL\n", "", False),
                 (1, "", "sudo: policy plugin failed\n", False), (1, "", "", False),
                 (2, denial, "", False), (0, denial + "    (ALL) ALL\n", "", False)]
        for rc, stdout, stderr, success in cases:
            with self.subTest(rc=rc, stdout=stdout, stderr=stderr):
                fake = self.root / "sudo"
                fake.write_text(f"#!{sys.executable}\nimport os, sys\n"
                                "assert os.environ['LC_ALL'] == 'C'\nassert os.environ['LANGUAGE'] == 'C'\n"
                                f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\nsys.exit({rc})\n")
                fake.chmod(0o700)
                self.run_role("account_sudo", success)

    def compose(self, *args):
        command = shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose"))
        if not shutil.which(command[0]) or subprocess.run(command + ["version"], capture_output=True).returncode:
            self.skipTest("Install Compose v2 or set COMPOSE_COMMAND")
        environment = dict(os.environ)
        environment.pop("VALHEIM_WORLD", None)
        result = subprocess.run(command + list(args), cwd=self.project, text=True, capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_disabled_default_needs_no_secret_or_docker(self):
        self.variables.pop("valheim_enabled")
        self.variables.pop("vault_valheim_server_password")
        self.run_role("main")
        self.assertFalse(self.project.exists())

    def test_selected_world_and_idle_state_survive_ansible(self):
        self.run_role()
        self.assertFalse(self.config()["idle_enabled"])
        self.assertTrue(self.config()["pre_change_backups"])
        state = self.root / "home/control"
        (state / "world.json").write_text('{"world":"ChosenThroughScript"}')
        (state / "idle.json").write_text('{"example":"untouched"}')
        self.variables["valheim_world_name"] = "NewBootstrapDefault"
        self.run_role()
        self.assertEqual(json.loads((state / "world.json").read_text())["world"], "ChosenThroughScript")
        self.assertEqual(json.loads((state / "idle.json").read_text()), {"example": "untouched"})
        self.assertEqual((self.project / "docker-compose.yml").stat().st_mode & 0o777, 0o600)

    def test_host_opt_in_reaches_controller(self):
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        for key in ["valheim_enabled", "valheim_idle_shutdown_enabled", "valheim_dashboard_enabled"]:
            self.assertIs(host[key], True)
        self.variables.update({key: value for key, value in host.items() if key.startswith("valheim_")})
        # Host RAID policy is checked in test_storage_services. Provision this
        # fixture only inside its temporary directory, never against real mounts.
        self.variables.update(valheim_backups_dir=str(self.root / "home/backups"),
                              valheim_required_mounts=[], valheim_required_mount_uuids={})
        self.run_role()
        self.assertIs(self.config()["idle_enabled"], True)
        self.assertEqual(self.config()["idle_timeout"], 3600)
        self.assertEqual(self.config()["query_address"], "192.168.68.111:2457")
        self.assertTrue(self.config()["pre_change_backups"])
        rendered = yaml.safe_load((self.project / "docker-compose.yml").read_text())
        self.assertEqual(rendered["services"]["valheim"]["environment"]["BACKUPS"], "true")
        # Native game backups keep their defaults: BACKUPS only controls the
        # image's scheduled worker, not Valheim's -backups/-backup* arguments.
        self.assertNotIn("-backup", rendered["services"]["valheim"]["environment"].get("SERVER_ARGS", ""))

    def test_scheduled_backups_do_not_control_pre_change_archives(self):
        self.variables["valheim_backups_enabled"] = False
        self.run_role()
        self.assertTrue(self.config()["pre_change_backups"])
        self.variables["valheim_pre_change_backups"] = False
        self.run_role()
        self.assertFalse(self.config()["pre_change_backups"])
        rendered = yaml.safe_load((self.project / "docker-compose.yml").read_text())
        self.assertEqual(rendered["services"]["valheim"]["environment"]["BACKUPS"], "false")

    def test_compose_profiles_ports_paths_and_literal_secrets(self):
        self.variables.update(valheim_server_port=30000, valheim_worlds_dir=str(self.root / "storage/saves"), valheim_backups_dir=str(self.root / "storage/backups"))
        self.run_role()
        self.assertEqual(self.compose("config", "--format", "json")["services"], {})
        full = self.compose("--profile", "game", "config", "--format", "json")
        self.assertEqual(set(full["services"]), {"valheim"})
        game = full["services"]["valheim"]
        self.assertEqual(game["restart"], "no")
        self.assertFalse(game.get("privileged", False))
        self.assertIn("no-new-privileges:true", game["security_opt"])
        self.assertEqual(game.get("cap_add", []), [])
        self.assertIn("NET_RAW", game["cap_drop"])
        for key, value in [("PUID", self.variables["valheim_game_account"]["uid"]), ("PGID", self.variables["valheim_game_account"]["group"])]:
            self.assertEqual(game["environment"][key], str(value))
        self.assertEqual(game["environment"]["WORLDS_DIRECTORY_PERMISSIONS"], "700")
        self.assertEqual({p["target"] for p in game["ports"]}, {30000, 30001, 30002})
        self.assertTrue(all(p["protocol"] == "udp" and p["host_ip"] == "192.168.68.111" for p in game["ports"]))
        self.assertEqual(game["environment"]["SERVER_PASS"].replace("$$", "$"), self.password)
        self.assertEqual(game["environment"]["UPDATE_CRON"], "")
        self.assertEqual(game["environment"]["RESTART_CRON"], "")
        self.assertIn("@sha256:", game["image"])
        mounts = {v["target"]: v["source"] for v in game["volumes"]}
        self.assertEqual(mounts["/config/worlds_local"], str(self.root / "storage/saves"))
        self.assertEqual(mounts["/config/backups"], str(self.root / "storage/backups"))
        backup_mount = next(v for v in game["volumes"] if v["target"] == "/config/backups")
        self.assertFalse(backup_mount["bind"]["create_host_path"])
        backup = Path(mounts["/config/backups"])
        self.assertEqual(backup.stat().st_uid, self.variables["valheim_game_account"]["uid"])
        self.assertEqual(backup.stat().st_gid, self.variables["valheim_game_account"]["group"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(game["environment"]["BACKUPS_DIRECTORY_PERMISSIONS"], "700")
        self.assertEqual(game["environment"]["BACKUPS_FILE_PERMISSIONS"], "600")
        self.assertFalse(any("docker.sock" in value for value in mounts.values()))

    def test_backup_path_change_preserves_worlds_and_old_archives(self):
        self.run_role()
        saves = Path(self.config()["worlds_dir"])
        old_backups = Path(self.config()["backups_dir"])
        (saves / "Home.db").write_bytes(b"keep world")
        (old_backups / "manual-existing.tar.gz").write_bytes(b"keep archive")
        new_backups = self.root / "raid/backups/valheim"
        self.variables["valheim_backups_dir"] = str(new_backups)
        self.run_role()
        self.assertEqual(self.config()["backups_dir"], str(new_backups))
        self.assertEqual((saves / "Home.db").read_bytes(), b"keep world")
        self.assertEqual((old_backups / "manual-existing.tar.gz").read_bytes(), b"keep archive")
        self.assertEqual(new_backups.stat().st_uid, self.variables["valheim_game_account"]["uid"])
        self.assertEqual(new_backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(new_backups.iterdir()), [])

    def test_game_mount_cannot_expose_administrative_files(self):
        for directory in [str(self.project), str(self.root / "home"), "/var/run", "/run"]:
            self.variables["valheim_config_dir"] = directory
            self.run_role(expect_success=False)
            self.assertFalse(self.project.exists())

    def test_home_cannot_be_a_storage_root(self):
        for directory in ["/", "/var", "/var/lib", "/home", "/mnt", "/opt", "/etc", "/usr", "/run", "/srv"]:
            self.variables["valheim_home"] = directory
            self.run_role(expect_success=False)
            self.assertFalse(self.project.exists())

    def test_storage_aliases_cannot_bypass_administrative_isolation(self):
        actual = self.root / "administrative"
        actual.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        self.variables["valheim_state_dir"] = str(actual / "control")
        for directory in [str(alias), str(actual) + "/", str(actual) + "//", str(alias / "new-config")]:
            self.variables["valheim_config_dir"] = directory
            self.run_role(expect_success=False)
            self.assertFalse(self.project.exists())
        self.assertFalse((actual / "new-config").exists())

    def test_required_storage_mount_must_exist(self):
        self.variables["valheim_required_mounts"] = [str(self.root)]
        self.run_role(expect_success=False)
        self.assertFalse(self.project.exists())

    def test_wrong_backup_mount_uuid_blocks_provisioning_before_directory_creation(self):
        self.variables["valheim_required_mount_uuids"] = {"/": "00000000-0000-0000-0000-000000000000"}
        self.run_role(expect_success=False)
        self.assertFalse(self.project.exists())
        self.assertFalse((self.root / "home/backups").exists())

    def test_backup_destination_cannot_contain_configuration(self):
        for suffix in ["", "/config"]:
            self.variables.update(valheim_backups_dir=str(self.root / "storage"), valheim_config_dir=str(self.root / "storage") + suffix)
            self.run_role(expect_success=False)
            self.assertFalse(self.project.exists())

    def test_existing_nonempty_storage_requires_explicit_owner_migration(self):
        saves = self.root / "home/saves"
        saves.mkdir(parents=True)
        world = saves / "world.dat"
        world.write_bytes(b"keep my world")
        self.variables["valheim_game_account"]["uid"] = (os.getuid() or 1000) + 1
        self.run_role(expect_success=False)
        self.assertEqual(world.read_bytes(), b"keep my world")
        self.assertEqual(saves.stat().st_uid, os.getuid())
        self.assertFalse(self.project.exists())

    def test_account_preflight_accepts_stable_ids_but_rejects_takeover_and_renumbering(self):
        account, group = ["x", "995", "994", "", "/nonexistent", "/usr/sbin/nologin"], ["x", "994", ""]
        self.variables.update(valheim_existing_user={"ansible_facts": {"getent_passwd": {"valheim-game": account}}}, valheim_existing_group={"ansible_facts": {"getent_group": {"valheim-game": group}}}, valheim_existing_memberships={"stdout": "994"})
        self.run_role("account_preflight")
        self.run_role("account_preflight")
        for variable, value in [("valheim_uid", 996), ("valheim_gid", 997)]:
            self.variables[variable] = value
            self.run_role("account_preflight", False)
            self.variables.pop(variable)
        account[5] = "/bin/bash"
        self.run_role("account_preflight", False)
        account[5] = "/usr/sbin/nologin"
        self.variables["valheim_existing_memberships"]["stdout"] = "994 966"
        self.run_role("account_preflight", False)

    def test_rust_units_and_dashboard_privilege_boundary(self):
        for name in ["valheim-idle.service", "valheim-dashboard.service", "valheim-dashboard-broker.service"]:
            text = (ROLE / "templates" / (name + ".j2")).read_text()
            self.assertNotIn("python", text)
            self.assertIn("/usr/local/libexec/homelab-valheim/valheim-", text)
        web = (ROLE / "templates/valheim-dashboard.service.j2").read_text()
        self.assertIn("User=valheim-dashboard", web)
        self.assertIn("NoNewPrivileges=true", web)
        self.assertIn("InaccessiblePaths=-/run/docker.sock", web)
        self.assertNotIn("sudo", web)
        broker = (ROLE / "templates/valheim-dashboard-broker.service.j2").read_text()
        self.assertIn("RuntimeDirectoryMode=0750", broker)
        self.assertIn("Group=valheim-dashboard", broker)
        install = (ROLE / "tasks/install_control.yml").read_text()
        self.assertIn("--locked", install)
        self.assertIn("delegate_to: localhost", install)
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults["valheim_rust_target"], "x86_64-unknown-linux-musl")
        self.assertFalse(defaults["valheim_dashboard_enabled"])
        self.assertEqual(defaults["valheim_dashboard_bind_ip"], "127.0.0.1")
        self.assertEqual(defaults["valheim_dashboard_allowed_networks"], ["127.0.0.0/8"])
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        self.assertEqual(host["valheim_dashboard_bind_ip"], "{{ compose_bind_ip }}")
        self.assertFalse((ROLE / "files/control.py").exists())

    def test_dashboard_units_render_with_separate_runtime_user_and_custom_paths(self):
        self.variables["valheim_dashboard_port"] = 9898
        self.variables["valheim_dashboard_bind_ip"] = "192.168.68.111"
        self.variables["valheim_dashboard_allowed_networks"] = ["192.168.68.0/24"]
        self.run_role("dashboard_render")
        web = (self.root / "valheim-dashboard.service").read_text()
        self.assertIn("web --port 9898", web)
        self.assertIn("--bind 192.168.68.111 --allow-network 192.168.68.0/24", web)
        self.assertIn("User=valheim-dashboard", web)
        broker = (self.root / "valheim-dashboard-broker.service").read_text()
        self.assertIn(f"broker --config {self.root}/home/control/config.json", broker)
        self.assertNotIn("{{", web + broker)
        binary = ROOT / "tools/homelab/target/x86_64-unknown-linux-musl/release"
        if shutil.which("systemd-analyze") and (binary / "valheim-dashboard").exists():
            # Verify rendered directives without installing units or requiring
            # production executables on the test machine.
            units = list(self.root.glob("*.service"))
            for unit in units:
                unit.write_text(unit.read_text().replace("/usr/local/libexec/homelab-valheim/", str(binary) + "/"))
            result = subprocess.run(["systemd-analyze", "verify", "--man=no", *map(str, units)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ansible_never_automatically_starts_game(self):
        for path in (ROLE / "tasks").glob("*.yml"):
            text = path.read_text()
            self.assertNotIn("docker_compose_v2", text)
            self.assertNotIn("valheimctl start", text)
            self.assertNotIn("scope: user", text)

    def test_retirement_targets_only_valheim_and_preserves_files(self):
        tasks = yaml.safe_load((ROLE / "tasks/disable.yml").read_text())
        self.assertEqual(tasks[0]["ansible.builtin.import_tasks"], "dashboard_disable.yml")
        units = [task["ansible.builtin.systemd_service"]["name"] for task in tasks if "ansible.builtin.systemd_service" in task]
        self.assertEqual(units, ["valheim-idle.timer", "valheim-idle.service"])
        command = next(task["ansible.builtin.command"] for task in tasks if "ansible.builtin.command" in task)
        self.assertEqual(command["argv"], ["/usr/local/sbin/valheimctl", "stop"])
        self.assertNotIn("state: absent", (ROLE / "tasks/disable.yml").read_text())


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="valheim-wrapper-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / "ansible-playbook"
        executable.write_text(f"#!{sys.executable}\nimport json, os, sys\nprint(json.dumps({{'cwd':os.getcwd(),'args':sys.argv[1:]}}))\n")
        executable.chmod(0o755)

    def invoke(self, *args, password_file=False):
        environment = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])
        environment.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
        environment.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
        if password_file:
            environment["ANSIBLE_VAULT_PASSWORD_FILE"] = "/outside-git/vault-password"
        return subprocess.run([str(ROOT / "scripts/valheim"), *args], env=environment, text=True, capture_output=True)

    def test_uses_existing_ansible_directory_and_forwards_options(self):
        result = self.invoke("world", "NewWorld", "--limit", "homelab")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual(call["cwd"], str(ROOT / "ansible"))
        for arg in ["valheim_action=world valheim_selected_world=NewWorld", "--ask-become-pass", "--ask-vault-pass"]:
            self.assertIn(arg, call["args"])
        self.assertEqual(call["args"][-2:], ["--limit", "homelab"])

    def test_standard_vault_password_file_avoids_extra_prompt(self):
        result = self.invoke("status", password_file=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--ask-vault-pass", json.loads(result.stdout)["args"])

    def test_retire_is_an_explicit_ansible_action(self):
        self.assertIn("valheim_action=retire", json.loads(self.invoke("retire").stdout)["args"])

    def test_rejects_invalid_actions_and_world_names(self):
        for args in [("delete",), ("world",), ("world", "--limit"), ("world", "name;whoami")]:
            self.assertEqual(self.invoke(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
