"""Isolated Ansible/Compose regressions; never deploy or invoke the Docker daemon."""

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
ROLE = ROOT / "ansible/roles/opencode"


class OpenCodeProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="opencode-ansible-", dir="/tmp/opencode"
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.storage = self.root / "storage"
        self.password = "Synthetic-${literal}-$money-'quote'-back\\slash#"
        self.variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text()) | {
            "ansible_python_interpreter": sys.executable,
            "opencode_project_dir": str(self.project),
            "opencode_storage_dir": str(self.storage),
            "vault_opencode_server_password": self.password,
        }

    def prepare(self, *, validation_only=False, check=False):
        tasks = yaml.safe_load((ROLE / "tasks/prepare.yml").read_text())
        if validation_only:
            tasks = [task for task in tasks if "ansible.builtin.assert" in task]
        for task in tasks:
            for module in ["ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template"]:
                if module not in task:
                    continue
                args = task[module]
                # Render the real role, but only mutate this temporary fixture.
                target = args.get("path", args.get("dest"))
                task.setdefault("vars", {})["opencode_test_target"] = target
                task["when"] = [task.get("when", True), f"opencode_test_target is match('^{self.root}/')"]
                args["owner"] = str(os.getuid())
                args["group"] = str(os.getgid())
                if "src" in args:
                    directory = "templates" if module.endswith("template") else "files"
                    args["src"] = str(ROLE / directory / args["src"])
        play = self.root / "play.yml"
        play.write_text(yaml.safe_dump([{
            "hosts": "localhost", "gather_facts": False,
            "vars": self.variables, "tasks": tasks,
        }]))
        command = ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)]
        if check:
            command.append("--check")
        result = subprocess.run(
            command,
            cwd=self.root, text=True, capture_output=True,
            env=dict(os.environ, ANSIBLE_NOCOLOR="1"),
        )
        self.assertNotIn(self.password, result.stdout + result.stderr)
        return result

    def provision(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def compose(self):
        command = shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose"))
        if not shutil.which(command[0]) or subprocess.run(command + ["version"], capture_output=True).returncode:
            self.skipTest("Docker Compose v2 required; set COMPOSE_COMMAND for a standalone binary")
        result = subprocess.run(
            command + ["-f", str(self.project / "compose.yaml"), "config", "--format", "json"],
            cwd=self.root, text=True, capture_output=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_rendered_stack_is_private_unprivileged_and_resource_limited(self):
        self.provision()
        config = self.compose()
        self.assertEqual(set(config["services"]), {"opencode"})
        service = config["services"]["opencode"]
        self.assertEqual(service["user"], "10001:10001")
        self.assertFalse(service.get("privileged", False))
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertTrue(service["init"])
        self.assertEqual(service["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(service["ports"][0]["target"], 4096)
        # Compose re-escapes literal dollars in its serialized config output.
        self.assertEqual(service["environment"]["OPENCODE_PASSWORD"].replace("$$", "$"), self.password)
        self.assertEqual(int(service["mem_limit"]), 4 * 1024**3)
        self.assertEqual(int(service["memswap_limit"]), int(service["mem_limit"]))
        self.assertEqual(float(service["cpus"]), 2)
        self.assertEqual(service["pids_limit"], 512)
        self.assertIn("/api/info", service["healthcheck"]["test"][1])
        self.assertNotIn("network_mode", service)
        self.assertNotIn("devices", service)
        self.assertEqual(service["logging"]["options"]["max-file"], "3")

    def test_only_dedicated_storage_is_persistent_and_settings_survive_reprovision(self):
        self.provision()
        service = self.compose()["services"]["opencode"]
        expected = {
            "workspace": "/workspace", "config": "/home/opencode/.config",
            "data": "/home/opencode/.local/share", "state": "/home/opencode/.local/state",
        }
        self.assertEqual(len(service["volumes"]), 4)
        for mount in service["volumes"]:
            name = Path(mount["source"]).name
            self.assertEqual(mount["source"], str(self.storage / name))
            self.assertEqual(mount["target"], expected[name])
            self.assertFalse(mount["bind"].get("create_host_path", False))
            self.assertEqual(mount["type"], "bind")
        self.assertEqual((self.project / "compose.yaml").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.storage.stat().st_mode & 0o777, 0o700)
        settings = self.storage / "config/opencode/opencode.json"
        self.assertEqual(json.loads(settings.read_text())["update"], "disable")
        settings.write_text('{"update":"disable","model":"openai/example"}\n')
        sentinel = self.storage / "workspace" / "keep.txt"
        sentinel.write_text("user work")
        database = self.storage / "data" / "opencode.db"
        database.write_bytes(b"synthetic persistent database fixture")
        self.provision()
        self.assertEqual(json.loads(settings.read_text())["model"], "openai/example")
        self.assertEqual(sentinel.read_text(), "user work")
        self.assertEqual(database.read_bytes(), b"synthetic persistent database fixture")
        self.assertEqual((self.project / ".dockerignore").read_text(), "*\n!Dockerfile\n")

    def test_reprovision_does_not_follow_agent_created_config_symlinks(self):
        self.provision()
        settings = self.storage / "config/opencode/opencode.json"
        settings.unlink()
        settings.parent.rmdir()
        outside = self.root / "outside-config"
        outside.mkdir(mode=0o755)
        sentinel = outside / "opencode.json"
        sentinel.write_text("do not overwrite")
        sentinel.chmod(0o644)
        settings.parent.symlink_to(outside, target_is_directory=True)
        self.provision()
        self.assertEqual(sentinel.read_text(), "do not overwrite")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertEqual(sentinel.stat().st_mode & 0o777, 0o644)

    def test_bad_storage_port_and_password_are_rejected_before_mutation(self):
        cases = [
            {"opencode_project_dir": "/"},
            {"opencode_storage_dir": "/var"},
            {"opencode_storage_dir": "/var/lib/../lib/opencode"},
            {"opencode_storage_dir": str(self.project / "nested")},
            {"opencode_port": 80},
            {"opencode_cpus": 0},
            {"vault_opencode_server_password": ""},
            {"vault_opencode_server_password": "a" * 24 + "\n"},
        ]
        original = dict(self.variables)
        for overrides in cases:
            with self.subTest(overrides=list(overrides)):
                self.variables = original | overrides
                result = self.prepare(validation_only=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.project.exists())
                self.assertFalse(self.storage.exists())

    def test_deployment_is_separate_and_check_mode_never_contacts_docker(self):
        site = yaml.safe_load((ROOT / "ansible/site.yml").read_text())
        self.assertNotIn("opencode", str(site))
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        deploy = next(task for task in tasks if "community.docker.docker_compose_v2" in task)
        self.assertEqual(deploy["when"], "not ansible_check_mode")
        self.assertTrue(deploy["no_log"])
        self.assertEqual(deploy["community.docker.docker_compose_v2"]["project_name"], "homelab-opencode")

    def test_fresh_check_mode_does_not_create_storage_or_config(self):
        result = self.prepare(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.project.exists())
        self.assertFalse(self.storage.exists())


if __name__ == "__main__":
    unittest.main()
