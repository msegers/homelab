"""Local checks: temporary provisioning, Compose validation, and fake game state."""

import importlib.util
import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/valheim"
spec = importlib.util.spec_from_file_location("valheim_control", ROLE / "files/control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        tmp = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="valheim-test-", dir=tmp)
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
        if tasks_from in ["provision", "account_preflight", "account_sudo"]:
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text()) | self.variables
            source = "account.yml" if tasks_from.startswith("account_") else "provision.yml"
            tasks = yaml.safe_load((ROLE / "tasks" / source).read_text())
            if tasks_from == "account_preflight":
                tasks = [task for task in tasks if "ansible.builtin.assert" in task]
            if tasks_from == "account_sudo":
                tasks = [task for task in tasks if task.get("register") == "valheim_sudo_check"]
                tasks[0]["ansible.builtin.command"]["argv"][0] = str(self.root / "sudo")
            # Exercise the production rendering without requiring root on the
            # test machine. Root ownership policy is asserted separately below.
            for task in tasks:
                for module in ["ansible.builtin.file", "ansible.builtin.template"]:
                    if module in task:
                        if task[module].get("owner") == "root":
                            task[module]["owner"] = str(os.getuid())
                        if task[module].get("group") == "root":
                            task[module]["group"] = str(os.getgid())
                if "ansible.builtin.template" in task:
                    task["ansible.builtin.template"]["src"] = str(ROLE / "templates" / task["ansible.builtin.template"]["src"])
        play = [{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": tasks}]
        path = self.root / "play.yml"
        path.write_text(yaml.safe_dump(play))
        command = ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)]
        if tasks_from == "main":
            command.append("--check")  # Never stop a real test host's installed services.
        result = subprocess.run(
            command,
            cwd=self.root, text=True, capture_output=True,
            env=dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1"),
        )
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_sudo_check_requires_explicit_denial_regardless_of_exit_status(self):
        denial = "User valheim-game is not allowed to run sudo on homelab.\n"
        cases = [
            (0, denial, "", True),
            (1, denial, "", True),
            (1, "", denial, True),
            (0, "User valheim-game may run the following commands on homelab:\n    (ALL) ALL\n", "", False),
            (1, "", "sudo: policy plugin failed\n", False),
            (1, "", "", False),
            (2, denial, "", False),
            (0, denial + "    (ALL) ALL\n", "", False),
        ]
        for rc, stdout, stderr, success in cases:
            with self.subTest(rc=rc, stdout=stdout, stderr=stderr):
                fake = self.root / "sudo"
                fake.write_text(
                    f"#!{sys.executable}\nimport os, sys\n"
                    "assert os.environ['LC_ALL'] == 'C'\n"
                    "assert os.environ['LANGUAGE'] == 'C'\n"
                    f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\nsys.exit({rc})\n"
                )
                fake.chmod(0o700)
                self.run_role("account_sudo", expect_success=success)

    def compose(self, *args):
        command = shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose"))
        if not shutil.which(command[0]):
            self.skipTest("Install Compose v2 or set COMPOSE_COMMAND")
        if subprocess.run(command + ["version"], capture_output=True).returncode:
            self.skipTest("Install Compose v2 or set COMPOSE_COMMAND")
        environment = dict(os.environ)
        environment.pop("VALHEIM_WORLD", None)
        result = subprocess.run(command + list(args), cwd=self.project, text=True,
                                capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_disabled_default_needs_no_secret_or_docker(self):
        self.variables.pop("valheim_enabled")
        self.variables.pop("vault_valheim_server_password")
        self.run_role("main")
        self.assertFalse(self.project.exists())

    def test_selected_world_and_idle_state_survive_ansible(self):
        self.run_role()
        config = json.loads((self.root / "home/control/config.json").read_text())
        self.assertNotIn("socket", config)
        self.assertFalse(config["idle_enabled"])
        controller = control.Controller(config)
        control.save_json(controller.world_file, {"world": "ChosenThroughScript"})
        control.save_json(controller.idle_file, {"example": "untouched"})
        self.variables["valheim_world_name"] = "NewBootstrapDefault"
        self.run_role()
        self.assertEqual(controller.world(), "ChosenThroughScript")
        self.assertEqual(json.loads(controller.idle_file.read_text()), {"example": "untouched"})
        self.assertEqual((self.project / "docker-compose.yml").stat().st_mode & 0o777, 0o600)

    def test_compose_profiles_ports_paths_and_literal_secrets(self):
        self.variables.update(valheim_server_port=30000,
                              valheim_worlds_dir=str(self.root / "storage/saves"),
                              valheim_backups_dir=str(self.root / "storage/backups"))
        self.run_role()
        self.assertEqual(self.compose("config", "--format", "json")["services"], {})
        full = self.compose("--profile", "game", "config", "--format", "json")
        self.assertEqual(set(full["services"]), {"valheim"})
        game = full["services"]["valheim"]
        self.assertEqual(game["restart"], "no")
        self.assertIs(game.get("privileged", False), False)
        self.assertIn("privileged: false", (self.project / "docker-compose.yml").read_text())
        self.assertIn("no-new-privileges:true", game["security_opt"])
        self.assertEqual(game.get("cap_add", []), [])
        self.assertIn("NET_RAW", game["cap_drop"])
        self.assertEqual(game["environment"]["PUID"], str(self.variables["valheim_game_account"]["uid"]))
        self.assertEqual(game["environment"]["PGID"], str(self.variables["valheim_game_account"]["group"]))
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
        self.assertFalse(any("docker.sock" in value for value in mounts.values()))

    def test_game_mount_cannot_expose_administrative_files(self):
        for directory in [str(self.project), "/var/run", "/run"]:
            with self.subTest(directory=directory):
                self.variables["valheim_config_dir"] = directory
                self.run_role(expect_success=False)
                self.assertFalse(self.project.exists())

    def test_required_storage_mount_must_exist(self):
        self.variables["valheim_required_mounts"] = [str(self.root)]
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
        account = ["x", "995", "994", "", "/nonexistent", "/usr/sbin/nologin"]
        group = ["x", "994", ""]
        self.variables.update(
            valheim_existing_user={"ansible_facts": {"getent_passwd": {"valheim-game": account}}},
            valheim_existing_group={"ansible_facts": {"getent_group": {"valheim-game": group}}},
            valheim_existing_memberships={"stdout": "994"},
        )
        self.run_role("account_preflight")
        self.run_role("account_preflight")
        for variable, value in [("valheim_uid", 996), ("valheim_gid", 997)]:
            self.variables[variable] = value
            self.run_role("account_preflight", expect_success=False)
            self.variables.pop(variable)
        account[5] = "/bin/bash"
        self.run_role("account_preflight", expect_success=False)
        account[5] = "/usr/sbin/nologin"
        self.variables["valheim_existing_memberships"]["stdout"] = "994 966"
        self.run_role("account_preflight", expect_success=False)

    def test_ansible_never_automatically_starts_game(self):
        for name in ["main.yml", "provision.yml", "install_control.yml", "idle_timer.yml"]:
            text = (ROLE / "tasks" / name).read_text()
            self.assertNotIn("docker_compose_v2", text)
            self.assertNotIn("valheimctl start", text)

    def test_system_timer_and_root_owned_control_files_without_extra_daemon(self):
        self.assertFalse((ROLE / "tasks/rootless.yml").exists())
        for path in (ROLE / "tasks").glob("*.yml"):
            text = path.read_text()
            self.assertNotIn("ansible.builtin.apt:", text)
            self.assertNotIn("scope: user", text)
        tasks = yaml.safe_load((ROLE / "tasks/idle_timer.yml").read_text())
        unit = tasks[0]["ansible.builtin.template"]
        self.assertEqual(unit["dest"], "/etc/systemd/system/{{ item }}")
        self.assertEqual(unit["owner"], "root")
        self.assertNotIn("scope", tasks[1]["ansible.builtin.systemd_service"])
        for task in yaml.safe_load((ROLE / "tasks/provision.yml").read_text()):
            if "ansible.builtin.template" in task:
                self.assertEqual(task["ansible.builtin.template"]["owner"], "root")
                self.assertEqual(task["ansible.builtin.template"]["mode"], "0600")
        account = yaml.safe_load((ROLE / "tasks/account.yml").read_text())
        user = next(task["ansible.builtin.user"] for task in account if "ansible.builtin.user" in task)
        self.assertTrue(user["system"])
        self.assertFalse(user["non_unique"])
        self.assertEqual(user["groups"], "")
        self.assertTrue(user["password_lock"])
        self.assertFalse(user["create_home"])
        self.assertEqual(user["shell"], "/usr/sbin/nologin")
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertNotIn("valheim_uid", defaults)
        self.assertNotIn("valheim_gid", defaults)

    def test_retirement_targets_only_valheim_and_preserves_files(self):
        tasks = yaml.safe_load((ROLE / "tasks/disable.yml").read_text())
        units = [task["ansible.builtin.systemd_service"]["name"] for task in tasks
                 if "ansible.builtin.systemd_service" in task]
        self.assertEqual(units, ["valheim-idle.timer", "valheim-idle.service"])
        command = next(task["ansible.builtin.command"] for task in tasks if "ansible.builtin.command" in task)
        self.assertEqual(command["argv"], ["/usr/local/sbin/valheimctl", "stop"])
        self.assertNotIn("state: absent", (ROLE / "tasks/disable.yml").read_text())


class FakeController(control.Controller):
    def __init__(self, config):
        super().__init__(config)
        self.info = {"Id": "container-1", "State": {"Running": True, "StartedAt": "start-1"}}
        self.players = 0
        self.activity = False
        self.stops = 0
        self.starts = 0
        self.count_sequence = []

    def inspect(self):
        return self.info

    def player_count(self, info):
        if self.count_sequence:
            return self.count_sequence.pop(0)
        if isinstance(self.players, Exception):
            raise self.players
        return self.players

    def recent_activity(self, since):
        return self.activity

    def game_state(self):
        return "RUNNING"

    def stop(self):
        self.stops += 1
        self.info["State"]["Running"] = False
        self.reset_idle()

    def start(self, recreate=False):
        self.starts += 1
        self.info["State"]["Running"] = True
        self.reset_idle()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        tmp = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="valheim-control-", dir=tmp)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = {"state_dir": str(root), "default_world": "Home", "idle_enabled": True,
                       "container": "valheim-server", "project_name": "homelab-valheim",
                       "poll_seconds": 60, "idle_timeout": 3600, "worlds_dir": str(root / "saves"),
                       "config_dir": str(root / "config"), "backups_dir": str(root / "backups"),
                       "project_dir": str(root / "stack"), "stop_timeout": 120}
        for name in ["worlds_dir", "config_dir", "backups_dir", "project_dir"]:
            Path(self.config[name]).mkdir()
        (Path(self.config["project_dir"]) / "docker-compose.yml").write_text("services: {}\n")
        self.controller = FakeController(self.config)

    def test_inspect_recognizes_missing_container_across_docker_versions(self):
        for message in [
            "Error: No such object: valheim-server",
            "error: no such object: valheim-server",
            "Error response from daemon: No such container: valheim-server",
            "error: no such container: valheim-server",
        ]:
            with self.subTest(message=message), patch.object(
                    self.controller, "docker", return_value=SimpleNamespace(
                        returncode=1, stdout="[]", stderr=message)):
                self.assertIsNone(control.Controller.inspect(self.controller))

    def test_inspect_does_not_treat_daemon_errors_as_missing_container(self):
        for message in ["Cannot connect to the Docker daemon", "permission denied", ""]:
            with self.subTest(message=message), patch.object(
                    self.controller, "docker", return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr=message)):
                with self.assertRaisesRegex(control.ControlError, "Docker Engine unavailable"):
                    control.Controller.inspect(self.controller)

    def tick(self, now):
        with patch.object(control.time, "monotonic", return_value=now), \
             patch.object(control.time, "time", return_value=1700000000 + now):
            return self.controller.idle()

    def test_stops_only_after_a_full_hour_of_confirmed_empty_checks(self):
        for now in range(0, 3600, 60):
            self.tick(now)
        self.assertEqual(self.controller.stops, 0)
        self.tick(3600)
        self.assertEqual(self.controller.stops, 1)

    def test_players_queries_and_connection_activity_reset_countdown(self):
        for condition in ["players", "query_failure", "connection"]:
            with self.subTest(condition=condition):
                self.controller.reset_idle()
                for now in range(0, 3540, 60):
                    self.tick(now)
                self.controller.players = 1 if condition == "players" else (
                    control.ControlError("unavailable") if condition == "query_failure" else 0)
                self.controller.activity = condition == "connection"
                self.tick(3540)
                self.controller.players, self.controller.activity = 0, False
                self.tick(3600)
                self.assertEqual(self.controller.stops, 0)

    def test_monitor_gap_and_container_restart_reset_countdown(self):
        self.tick(0)
        self.tick(4000)
        self.assertEqual(self.controller.stops, 0)
        self.controller.info["State"]["StartedAt"] = "new-start"
        self.tick(4060)
        self.assertEqual(json.loads(self.controller.idle_file.read_text())["zero_since"], 4060)

    def test_player_joining_during_final_query_prevents_stop(self):
        for now in range(0, 3600, 60):
            self.tick(now)
        self.controller.count_sequence = [0, 1]
        self.tick(3600)
        self.assertEqual(self.controller.stops, 0)

    def test_stopped_or_disabled_game_is_never_started(self):
        self.controller.info["State"]["Running"] = False
        self.tick(0)
        self.controller.info["State"]["Running"] = True
        self.config["idle_enabled"] = False
        self.tick(4000)
        self.assertEqual(self.controller.starts, 0)
        self.assertEqual(self.controller.stops, 0)

    def test_interrupted_check_cannot_leave_a_stale_countdown(self):
        self.tick(0)
        with patch.object(self.controller, "player_count", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(60)
        self.assertFalse(self.controller.idle_file.exists())

    def test_corrupt_countdown_cannot_cause_premature_shutdown(self):
        self.tick(0)
        previous = json.loads(self.controller.idle_file.read_text())
        previous["zero_since"] = -10000
        control.save_json(self.controller.idle_file, previous)
        self.tick(60)
        self.assertEqual(self.controller.stops, 0)
        self.assertEqual(json.loads(self.controller.idle_file.read_text())["zero_since"], 60)

    def test_real_query_parser_rejects_unknown_counts_and_unsupported_modes(self):
        info = {"Config": {"Env": ["SERVER_PUBLIC=true", "CROSSPLAY=false"]}}
        for payload in [{"count": 0, "listed": 1}, {"count": -1, "listed": -1},
                        {"count": False, "listed": 0}, []]:
            with self.subTest(payload=payload), patch.object(
                    self.controller, "docker", return_value=SimpleNamespace(stdout=json.dumps(payload))):
                with self.assertRaises(control.ControlError):
                    control.Controller.player_count(self.controller, info)
        for entries in [["SERVER_PUBLIC=false"], ["SERVER_PUBLIC=true", "CROSSPLAY=true"]]:
            with patch.object(self.controller, "docker") as docker:
                with self.assertRaises(control.ControlError):
                    control.Controller.player_count(self.controller, {"Config": {"Env": entries}})
                docker.assert_not_called()

    def test_backup_captures_current_world_directories_and_preserves_stop_state(self):
        world = Path(self.config["worlds_dir"]) / "MyWorld"
        world.mkdir()
        (world / "_main.1.fwl2").write_text("metadata")
        (world / "chunk.1").write_text("world data")
        self.controller.info["State"]["Running"] = False
        archive = Path(self.controller.backup())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as data:
            self.assertIn("saves/MyWorld/chunk.1", data.getnames())
            self.assertEqual(json.load(data.extractfile("world.json")), {"world": "Home"})
        self.assertEqual(self.controller.starts, 0)

    def test_first_start_freezes_world_selection_against_ansible_default_changes(self):
        with patch.object(self.controller, "compose"):
            control.Controller.start(self.controller)
        self.config["default_world"] = "NewAnsibleDefault"
        self.assertEqual(self.controller.world(), "Home")

    def test_world_selection_persists_without_starting_stopped_server(self):
        self.controller.info["State"]["Running"] = False
        self.controller.select_world("NewWorld")
        self.assertEqual(self.controller.world(), "NewWorld")
        self.assertEqual(self.controller.starts, 0)

    def test_running_backup_resumes_only_previously_running_game(self):
        self.controller.backup()
        self.assertEqual(self.controller.stops, 1)
        self.assertEqual(self.controller.starts, 1)

    def test_archive_failure_preserves_existing_backups_and_resumes_game(self):
        old = Path(self.controller.archive())
        contents = old.read_bytes()
        with patch.object(control.tarfile.TarFile, "add", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(control.ControlError, "Backup failed.*No space left.*Valheim resumed"):
                self.controller.backup()
        self.assertEqual(old.read_bytes(), contents)
        self.assertEqual(self.controller.starts, 1)
        self.assertEqual(list(Path(self.config["backups_dir"]).glob("*.partial")), [])

    def test_archive_and_resume_failures_are_both_reported(self):
        with patch.object(self.controller, "archive", side_effect=OSError(errno.ENOSPC, "full")), \
             patch.object(self.controller, "start", side_effect=control.ControlError("cannot start")):
            with self.assertRaisesRegex(control.ControlError, "Backup failed.*resuming Valheim also failed") as failure:
                self.controller.backup()
        self.assertIsInstance(failure.exception.__cause__, OSError)

    def test_successful_archive_is_kept_if_resume_fails(self):
        with patch.object(self.controller, "start", side_effect=control.ControlError("cannot start")):
            with self.assertRaisesRegex(control.ControlError, "Backup completed.*resuming Valheim also failed"):
                self.controller.backup()
        files = list(Path(self.config["backups_dir"]).glob("*.tar.gz"))
        self.assertEqual(len(files), 1)
        with tarfile.open(files[0]) as archive:
            self.assertIn("world.json", archive.getnames())

    def test_backup_filename_collision_never_replaces_existing_archive(self):
        with patch.object(control.uuid, "uuid4", return_value=SimpleNamespace(hex="0" * 32)), \
             patch.object(control, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "fixed-time-"
            old = Path(self.controller.archive())
            contents = old.read_bytes()
            with self.assertRaises(FileExistsError):
                self.controller.archive()
        self.assertEqual(old.read_bytes(), contents)
        self.assertEqual(list(Path(self.config["backups_dir"]).glob("*.partial")), [])

    def test_failed_publication_preserves_existing_backups(self):
        old = Path(self.controller.archive())
        contents = old.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                self.controller.archive()
        self.assertEqual(old.read_bytes(), contents)
        self.assertEqual(list(Path(self.config["backups_dir"]).iterdir()), [old])

    def test_failed_restart_is_reported_after_stop(self):
        with patch.object(self.controller, "start", side_effect=control.ControlError("compose failed")):
            with self.assertRaisesRegex(control.ControlError, "Restart failed after stopping"):
                self.controller.run("restart")
        self.assertEqual(self.controller.stops, 1)

    def test_start_checks_container_and_supervisor_failures(self):
        for state in ["exited", "fatal"]:
            with self.subTest(state=state), patch.object(self.controller, "compose"), \
                 patch.object(self.controller, "game_state", return_value="FATAL" if state == "fatal" else "RUNNING"):
                self.controller.info["State"]["Running"] = state != "exited"
                with self.assertRaises(control.ControlError):
                    control.Controller.start(self.controller)

    def test_failed_image_pull_leaves_running_game_untouched(self):
        with patch.object(self.controller, "compose", side_effect=control.ControlError("pull failed")):
            with self.assertRaisesRegex(control.ControlError, "existing game was left unchanged"):
                self.controller.run("update")
        self.assertEqual(self.controller.stops, 0)
        self.assertEqual(self.controller.starts, 0)

    def test_failed_world_backup_keeps_old_selection(self):
        with patch.object(self.controller, "archive", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaisesRegex(control.ControlError, "World switch failed"):
                self.controller.select_world("OtherWorld")
        self.assertEqual(self.controller.world(), "Home")
        self.assertEqual(self.controller.starts, 1)

    def test_docker_uses_existing_engine_and_compose_targets_only_valheim(self):
        with patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            control.Controller.docker(self.controller, "version")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/docker", "--context", "default", "version"])
        with patch.object(self.controller, "docker") as docker:
            control.Controller.compose(self.controller, "up", "-d", "--no-deps", "valheim")
        args = docker.call_args.args
        self.assertEqual(args[-4:], ("up", "-d", "--no-deps", "valheim"))
        self.assertIn("homelab-valheim", args)
        self.assertIn(str(Path(self.config["project_dir"]) / "docker-compose.yml"), args)

    def test_refuses_to_control_container_from_another_compose_project_or_service(self):
        controller = control.Controller(self.config)
        for project, service in [("homelab", "proxy"), ("homelab", "pihole"),
                                 ("homelab", "website"), ("homelab-valheim", "other")]:
            info = {"Config": {"Labels": {"com.docker.compose.project": project,
                                          "com.docker.compose.service": service}},
                    "State": {"Running": True}}
            for action in ["start", "stop"]:
                with self.subTest(project=project, service=service, action=action), patch.object(
                        controller, "docker", return_value=SimpleNamespace(returncode=0, stdout=json.dumps([info]))) as docker:
                    with self.assertRaises(control.ControlError):
                        getattr(controller, action)()
                    self.assertEqual(docker.call_args.args, ("inspect", "valheim-server"))
                    self.assertEqual(docker.call_count, 1)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        tmp = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="valheim-wrapper-", dir=tmp)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / "ansible-playbook"
        executable.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                              "print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n")
        executable.chmod(0o755)

    def invoke(self, *args, password_file=False):
        environment = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])
        environment.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
        environment.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
        if password_file:
            environment["ANSIBLE_VAULT_PASSWORD_FILE"] = "/outside-git/vault-password"
        return subprocess.run([str(ROOT / "scripts/valheim"), *args], env=environment,
                              text=True, capture_output=True)

    def test_uses_existing_ansible_directory_and_forwards_options(self):
        result = self.invoke("world", "NewWorld", "--limit", "homelab")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual(call["cwd"], str(ROOT / "ansible"))
        self.assertIn("valheim_action=world valheim_selected_world=NewWorld", call["args"])
        self.assertIn("--ask-become-pass", call["args"])
        self.assertIn("--ask-vault-pass", call["args"])
        self.assertEqual(call["args"][-2:], ["--limit", "homelab"])

    def test_standard_vault_password_file_avoids_extra_prompt(self):
        result = self.invoke("status", password_file=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--ask-vault-pass", json.loads(result.stdout)["args"])

    def test_retire_is_an_explicit_ansible_action(self):
        result = self.invoke("retire")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valheim_action=retire", json.loads(result.stdout)["args"])

    def test_rejects_invalid_actions_and_world_names(self):
        for args in [("delete",), ("world",), ("world", "--limit"), ("world", "name;whoami")]:
            self.assertEqual(self.invoke(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
