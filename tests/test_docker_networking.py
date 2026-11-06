"""Local-only checks for boot ordering and read-only network verification."""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "ansible/roles/docker"
COMPOSE = ROOT / "ansible/roles/compose"


class DockerNetworkingTests(unittest.TestCase):
    def run_tasks(self, tasks, variables):
        with tempfile.TemporaryDirectory(
            prefix="docker-network-test-",
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        ) as directory:
            play = Path(directory) / "play.yml"
            play.write_text(yaml.safe_dump([{
                "hosts": "localhost", "gather_facts": False,
                "vars": variables, "tasks": tasks,
            }]))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
                cwd=directory, capture_output=True, text=True,
            )

    def test_verification_rejects_detached_even_if_healthy(self):
        tasks = yaml.safe_load((COMPOSE / "tasks/verify_networks.yml").read_text())
        guard = next(t for t in tasks if t["name"] == "Require running containers with usable homelab default-network endpoints")
        endpoint = {"NetworkID": "network", "EndpointID": "endpoint",
                    "IPAddress": "172.18.0.2", "Gateway": "172.18.0.1"}
        cases = [
            (True, {"homelab_default": endpoint}, True),
            (True, {}, False),
            (True, None, False),
            (True, {"wrong_network": endpoint}, False),
            (False, {"homelab_default": endpoint}, False),
        ]
        for field in endpoint:
            cases.append((True, {"homelab_default": dict(endpoint, **{field: ""})}, False))
        for running, networks, success in cases:
            with self.subTest(running=running, networks=networks):
                result = self.run_tasks([guard], {"compose_network_inspections": {"results": [{
                    "item": "pihole", "stdout": json.dumps({
                        "running": running, "networks": networks, "health": "healthy",
                    }),
                }]}})
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                if not success:
                    self.assertIn("No repair was attempted", result.stdout)

    def test_verifier_is_read_only_and_does_not_inspect_secrets(self):
        tasks = yaml.safe_load((COMPOSE / "tasks/verify_networks.yml").read_text())
        self.assertEqual(tasks[0]["ansible.builtin.command"]["argv"],
                         ["systemctl", "is-active", "docker.service"])
        inspect = tasks[1]
        argv = inspect["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:4], ["docker", "--context", "default", "inspect"])
        self.assertNotIn(".Config", argv[5])
        self.assertEqual(inspect["loop"], ["proxy", "website", "pihole"])
        self.assertFalse(inspect["changed_when"])
        self.assertIn(".NetworkSettings.Ports", argv[5])
        self.assertEqual(len(tasks), 4)
        deploy = yaml.safe_load((COMPOSE / "tasks/main.yml").read_text())
        self.assertEqual(deploy[-1]["ansible.builtin.import_tasks"], "verify_networks.yml")

    def test_published_ports_require_actual_mappings_on_the_exact_lan_address(self):
        tasks = yaml.safe_load((COMPOSE / "tasks/verify_networks.yml").read_text())
        address = "192.168.68.111"
        correct = [{"HostIp": address, "HostPort": "53"}]
        for bindings, success in [
            (correct, True), ([], False), (None, False),
            ([{"HostIp": "0.0.0.0", "HostPort": "53"}], False),
            ([{"HostIp": "192.168.68.112", "HostPort": "53"}], False),
            ([{"HostIp": address, "HostPort": "5353"}], False),
            (correct + [{"HostIp": "::", "HostPort": "53"}], False),
        ]:
            with self.subTest(bindings=bindings):
                results = [{"item": "proxy", "stdout": json.dumps({"ports": {
                    "80/tcp": [{"HostIp": address, "HostPort": "80"}],
                    "443/tcp": [{"HostIp": address, "HostPort": "443"}],
                }})}, {"item": "pihole", "stdout": json.dumps({"ports": {
                    "53/tcp": correct, "53/udp": bindings,
                }})}]
                result = self.run_tasks(tasks[-1:], {
                    "compose_bind_ip": address,
                    "compose_network_inspections": {"results": results},
                })
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_published_port_verification_rejects_missing_maps_and_keys(self):
        tasks = yaml.safe_load((COMPOSE / "tasks/verify_networks.yml").read_text())
        for ports in [None, {}]:
            result = self.run_tasks(tasks[-1:], {
                "compose_bind_ip": "192.168.68.111",
                "compose_network_inspections": {"results": [
                    {"item": service, "stdout": json.dumps({"ports": ports})}
                    for service in ["proxy", "pihole"]
                ]},
            })
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("No repair was attempted", result.stdout)

    def test_native_waiter_is_interface_specific_and_does_not_restart_services(self):
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        self.assertEqual(host["docker_ifupdown_wait_online_interface"], "wlp0s20f3")
        defaults = yaml.safe_load((DOCKER / "defaults/main.yml").read_text())
        self.assertEqual(defaults["docker_ifupdown_wait_online_interface"], "")
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        settings = next(t for t in tasks if "ansible.builtin.lineinfile" in t)
        self.assertEqual(settings["loop"][0], {"key": "WAIT_ONLINE_METHOD", "value": "ifup"})
        self.assertEqual(settings["loop"][1]["key"], "WAIT_ONLINE_IFACE")
        copies = [t["ansible.builtin.copy"] for t in tasks if "ansible.builtin.copy" in t]
        instance = next(t for t in copies if "ifup@" in t["dest"])
        self.assertEqual(instance["content"], "[Service]\nType=oneshot\n")
        dropin = next(t for t in copies if "docker.service.d" in t["dest"])
        self.assertIn("Requires=ifupdown-wait-online.service", dropin["content"])
        self.assertIn("After=ifupdown-wait-online.service", dropin["content"])
        self.assertIn("ifup@{{ docker_ifupdown_wait_online_interface }}.service", dropin["content"])
        self.assertIn("ExecCondition=", dropin["content"])
        self.assertNotIn("ExecStartPre=", dropin["content"])
        for task in tasks:
            service = task.get("ansible.builtin.systemd_service", {})
            self.assertNotIn("state", service)
            self.assertNotIn("until", task)
            self.assertNotIn("ansible.builtin.shell", task)

    def test_stack_guard_rejects_other_network_managers(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        guard = next(t for t in tasks if t["name"] == "Require the confirmed ifupdown networking stack")
        for manager in [None, "NetworkManager.service", "systemd-networkd.service"]:
            with self.subTest(manager=manager):
                services = {"networking.service": {"state": "stopped"}}
                if manager:
                    services[manager] = {"state": "running"}
                result = self.run_tasks([guard], {
                    "ansible_facts": {"services": services},
                    "docker_ifupdown_active_state": {"stdout": "active\n"},
                    "docker_ifupdown_wait_online_interface": "wlp0s20f3",
                    "docker_ifupdown_wait_online_address": "192.168.68.111",
                })
                self.assertEqual(result.returncode == 0, manager is None,
                                 result.stdout + result.stderr)

    def test_stack_guard_uses_systemd_active_state_for_oneshot_service(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        query = next(t for t in tasks if t["name"] == "Read the ifupdown systemd active state")
        self.assertEqual(query["ansible.builtin.command"]["argv"],
                         ["systemctl", "show", "networking.service", "--property=ActiveState", "--value"])
        self.assertFalse(query["changed_when"])
        self.assertFalse(query["check_mode"])
        guard = next(t for t in tasks if t["name"] == "Require the confirmed ifupdown networking stack")
        for active_state in ["active", "inactive", "failed", "activating", ""]:
            with self.subTest(active_state=active_state):
                result = self.run_tasks([guard], {
                    "ansible_facts": {"services": {"networking.service": {"state": "stopped"}}},
                    "docker_ifupdown_active_state": {"stdout": active_state},
                    "docker_ifupdown_wait_online_interface": "wlp0s20f3",
                    "docker_ifupdown_wait_online_address": "192.168.68.111",
                })
                self.assertEqual(result.returncode == 0, active_state == "active",
                                 result.stdout + result.stderr)

    def test_interface_guard_accepts_hotplug_but_rejects_missing_interface(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        guard = next(t for t in tasks if t["name"] == "Check the selected ifupdown interface")
        for hotplug, success in [(["wlp0s20f3"], True), (["enp1s0"], False)]:
            result = self.run_tasks([guard], {
                "docker_ifupdown_wait_online_interface": "wlp0s20f3",
                "docker_ifupdown_interfaces": {"results": [
                    {"stdout_lines": ["lo"]}, {"stdout_lines": hotplug},
                ]},
            })
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_networking_defaults_edit_preserves_other_settings_and_is_idempotent(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        edit = next(t for t in tasks if "ansible.builtin.lineinfile" in t)
        with tempfile.TemporaryDirectory(
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        ) as directory:
            defaults = Path(directory) / "networking"
            defaults.write_text("VERBOSE=yes\n#WAIT_ONLINE_METHOD=ifup\n#WAIT_ONLINE_IFACE=\n")
            edit["ansible.builtin.lineinfile"]["path"] = str(defaults)
            for attempt in range(2):
                result = self.run_tasks([edit], {
                    "docker_ifupdown_wait_online_interface": "wlp0s20f3",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if attempt:
                    self.assertIn("changed=0", result.stdout)
            self.assertEqual(defaults.read_text(),
                             "VERBOSE=yes\nWAIT_ONLINE_METHOD=ifup\nWAIT_ONLINE_IFACE=wlp0s20f3\n")

    def test_rendered_start_condition_checks_exact_ipv4_without_retrying(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        copy = next(t for t in tasks if "docker.service.d/ifupdown-online.conf" in
                    t.get("ansible.builtin.copy", {}).get("dest", ""))
        with tempfile.TemporaryDirectory(
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        ) as directory:
            root = Path(directory)
            dest = root / "online.conf"
            copy["ansible.builtin.copy"].update(dest=str(dest), owner=None, group=None)
            result = self.run_tasks([copy], {
                "docker_ifupdown_wait_online_interface": "wlp0s20f3",
                "docker_ifupdown_wait_online_address": "192.168.68.111",
            })
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = dest.read_text()
            self.assertIn("Requires=ifupdown-wait-online.service ifup@wlp0s20f3.service", rendered)
            self.assertIn("After=ifupdown-wait-online.service ifup@wlp0s20f3.service", rendered)
            if shutil.which("systemd-analyze"):
                # Parse dependencies and ExecCondition with real systemd tooling;
                # synthetic ExecStart commands are never executed by verify.
                docker_unit = root / "docker.service"
                docker_unit.write_text(
                    "[Unit]\nWants=network-online.target\nAfter=network-online.target\n"
                    "[Service]\nType=simple\nExecStart=/usr/bin/true\nRestart=always\n" + rendered)
                ifup_unit = root / "ifup@wlp0s20f3.service"
                ifup_unit.write_text(
                    "[Unit]\nBefore=network-online.target\n"
                    "[Service]\nType=simple\nExecStart=/usr/bin/true\nRemainAfterExit=true\n"
                    "TimeoutStartSec=5min\n[Service]\nType=oneshot\n")
                waiter_unit = root / "ifupdown-wait-online.service"
                waiter_unit.write_text(
                    "[Unit]\nBefore=network-online.target\n"
                    "[Service]\nType=oneshot\nExecStart=/usr/bin/true\n")
                verified = subprocess.run(
                    ["systemd-analyze", "verify", "--man=no", str(docker_unit),
                     str(ifup_unit), str(waiter_unit)],
                    capture_output=True, text=True,
                )
                self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            condition = next(line.removeprefix("ExecCondition=") for line in rendered.splitlines()
                             if line.startswith("ExecCondition="))
            fake_ip = root / "ip"
            argv = shlex.split(condition.replace("/usr/sbin/ip", str(fake_ip)))
            for address, success in [("192.168.68.111", True), ("192.168.68.11", False),
                                     ("192.168.68.1111", False), (None, False)]:
                with self.subTest(address=address):
                    output = f"3: wlp0s20f3 inet {address}/24 scope global wlp0s20f3" if address else ""
                    fake_ip.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(output) + "\n")
                    fake_ip.chmod(0o700)
                    checked = subprocess.run(argv, capture_output=True, text=True, timeout=5)
                    self.assertEqual(checked.returncode, 0 if success else 1,
                                     checked.stdout + checked.stderr)

    def test_configured_address_must_belong_to_selected_interface(self):
        tasks = yaml.safe_load((DOCKER / "tasks/ifupdown.yml").read_text())
        guard = next(t for t in tasks if t["name"] == "Require the intended bind address on the selected interface")
        for addresses, success in [(["192.168.68.111"], True), (["192.168.68.112"], False), ([], False)]:
            result = self.run_tasks([guard], {
                "docker_ifupdown_wait_online_address": "192.168.68.111",
                "docker_ifupdown_addresses": {"stdout": json.dumps([{
                    "addr_info": [{"local": value} for value in addresses],
                }])},
            })
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
