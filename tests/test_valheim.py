"""Local checks: temporary provisioning, Compose validation, and fake game state."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
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
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / "docker.sock"))
        self.addCleanup(self.sock.close)
        self.password = "S3cret-${literal}-$money-'quote'-back\\slash#"
        self.variables = {
            "ansible_architecture": "x86_64",
            "ansible_all_ipv4_addresses": ["192.168.68.111"],
            "ansible_python_interpreter": sys.executable,
            "valheim_enabled": True,
            "valheim_home": str(self.root / "home"),
            "valheim_project_dir": str(self.project),
            "valheim_game_bind_ip": "192.168.68.111",
            "valheim_docker_socket": str(self.root / "docker.sock"),
            "valheim_account": {"uid": os.getuid() or 1000, "group": os.getgid() or 1000},
            "vault_valheim_server_password": self.password,
        }

    def run_role(self, tasks_from="provision", expect_success=True):
        play = [{"hosts": "localhost", "gather_facts": False, "vars": self.variables,
                 "tasks": [{"ansible.builtin.include_role": {
                     "name": "valheim", "tasks_from": tasks_from}}]}]
        path = self.root / "play.yml"
        path.write_text(yaml.safe_dump(play))
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
            cwd=self.root, text=True, capture_output=True,
            env=dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1"),
        )
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)

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
        self.variables["valheim_docker_socket"] = str(self.root / "absent.sock")
        self.run_role("main")
        self.assertFalse(self.project.exists())

    def test_selected_world_and_idle_state_survive_ansible(self):
        self.run_role()
        config = json.loads((self.root / "home/control/config.json").read_text())
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

    def test_other_users_socket_is_rejected(self):
        self.variables["valheim_account"]["uid"] += 1
        self.run_role(expect_success=False)
        self.assertFalse(self.project.exists())

    def test_required_storage_mount_must_exist(self):
        self.variables["valheim_required_mounts"] = [str(self.root)]
        self.run_role(expect_success=False)
        self.assertFalse(self.project.exists())

    def test_ansible_never_automatically_starts_game(self):
        for name in ["main.yml", "provision.yml", "install_control.yml", "idle_timer.yml"]:
            text = (ROLE / "tasks" / name).read_text()
            self.assertNotIn("docker_compose_v2", text)
            self.assertNotIn("valheimctl start", text)

    def test_rootless_account_and_user_scoped_docker_service(self):
        tasks = yaml.safe_load((ROLE / "tasks/rootless.yml").read_text())
        account = next(t["ansible.builtin.user"] for t in tasks if "ansible.builtin.user" in t)
        self.assertEqual(account["groups"], "")
        self.assertEqual(account["shell"], "/usr/sbin/nologin")
        block = next(t for t in tasks if t.get("become_user") == "{{ valheim_user }}")
        service = next(t["ansible.builtin.systemd_service"] for t in block["block"]
                       if "ansible.builtin.systemd_service" in t)
        self.assertEqual(service["scope"], "user")
        self.assertTrue(any("name=rootless" in str(t) for t in block["block"]))


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
                       "container": "valheim-server",
                       "poll_seconds": 60, "idle_timeout": 3600, "worlds_dir": str(root / "saves"),
                       "config_dir": str(root / "config"), "backups_dir": str(root / "backups"),
                       "project_dir": str(root / "stack"), "stop_timeout": 120}
        for name in ["worlds_dir", "config_dir", "backups_dir", "project_dir"]:
            Path(self.config[name]).mkdir()
        self.controller = FakeController(self.config)

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
        result = self.invoke("world", "NewWorld", "--limit", "elitedesk")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual(call["cwd"], str(ROOT / "ansible"))
        self.assertIn("valheim_action=world valheim_selected_world=NewWorld", call["args"])
        self.assertIn("--ask-vault-pass", call["args"])
        self.assertEqual(call["args"][-2:], ["--limit", "elitedesk"])

    def test_standard_vault_password_file_avoids_extra_prompt(self):
        result = self.invoke("status", password_file=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--ask-vault-pass", json.loads(result.stdout)["args"])

    def test_rejects_invalid_actions_and_world_names(self):
        for args in [("delete",), ("world",), ("world", "--limit"), ("world", "name;whoami")]:
            self.assertEqual(self.invoke(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
