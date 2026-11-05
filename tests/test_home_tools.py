"""Local-only Ansible rendering checks for Home Tools DNS integration."""

import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/compose"


class HomeToolsDnsTests(unittest.TestCase):
    def test_render_works_without_the_opencode_temporary_directory(self):
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.render(None)

    def render(self, local_hosts, **overrides):
        with tempfile.TemporaryDirectory(prefix="home-tools-dns-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None) as temporary:
            root = Path(temporary)
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
            variables.update(compose_bind_ip="192.168.68.111", compose_pihole_password="test-password",
                             compose_pihole_local_hosts=local_hosts)
            variables.update(overrides)
            play = [{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": [{
                "ansible.builtin.template": {"src": str(ROLE / "templates/compose.yaml.j2"),
                                            "dest": str(root / "compose.yaml")},
            }]}]
            path = root / "play.yml"
            path.write_text(yaml.safe_dump(play))
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = yaml.safe_load((root / "compose.yaml").read_text())
            command = shlex.split(os.environ.get("COMPOSE_COMMAND", "docker compose"))
            if shutil.which(command[0]) and not subprocess.run(command + ["version"], capture_output=True).returncode:
                checked = subprocess.run(command + ["-f", str(root / "compose.yaml"), "config", "--format", "json"],
                                         cwd=root, capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertEqual(json.loads(checked.stdout)["services"]["pihole"]["environment"],
                                 rendered["services"]["pihole"]["environment"])
            return rendered

    def test_local_records_can_be_managed_without_changing_website_or_listeners(self):
        records = ["192.168.68.111 lan.mathijs.se", "192.168.68.20 nas.mathijs.se"]
        original = self.render(None)
        managed = self.render(records)
        self.assertNotIn("FTLCONF_dns_hosts", original["services"]["pihole"]["environment"])
        self.assertEqual(managed["services"]["pihole"]["environment"].pop("FTLCONF_dns_hosts"), ";".join(records))
        self.assertEqual(managed, original)

    def test_host_configuration_prepares_local_tools_and_pihole_records(self):
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        self.assertEqual(host["compose_pihole_local_hosts"], ["{{ compose_bind_ip }} lan.mathijs.se", "{{ compose_bind_ip }} pihole.mathijs.se"])
        self.assertEqual(host["compose_pihole_domain"], "pihole.mathijs.se")
        self.assertEqual(host["compose_pihole_allowed_networks"], ["192.168.68.0/24"])
        self.assertEqual(host["valheim_dashboard_allowed_hosts"], ["lan.mathijs.se"])
        rendered = self.render(["192.168.68.111 pihole.mathijs.se"],
                               compose_pihole_domain=host["compose_pihole_domain"],
                               compose_pihole_allowed_networks=host["compose_pihole_allowed_networks"])
        proxy = rendered["services"]["proxy"]
        self.assertEqual(proxy["environment"]["PIHOLE_DOMAIN"], "pihole.mathijs.se")
        self.assertEqual(proxy["environment"]["PIHOLE_ALLOWED_NETWORKS"], "192.168.68.0/24")
        self.assertEqual(proxy["ports"], ["192.168.68.111:80:80", "192.168.68.111:443:443"])

    def test_caddy_pihole_gate_checks_real_peer_not_forwarded_headers(self):
        command = shlex.split(os.environ.get("CADDY_COMMAND", "caddy"))
        if not shutil.which(command[0]):
            self.skipTest("Install Caddy or set CADDY_COMMAND for proxy integration checks")
        source = (ROLE / "files/Caddyfile").read_text()
        environment = dict(os.environ, WEBSITE_DOMAIN="website.example.test",
                           WEBSITE_WWW_DOMAIN="www.website.example.test",
                           PIHOLE_DOMAIN="pihole.mathijs.se", PIHOLE_ALLOWED_NETWORKS="192.168.68.0/24")
        adapted = subprocess.run(command + ["adapt", "--adapter", "caddyfile", "--config", str(ROLE / "files/Caddyfile"), "--validate"],
                                 env=environment, capture_output=True, text=True)
        self.assertEqual(adapted.returncode, 0, adapted.stderr)
        self.assertIn('"192.168.68.0/24"', adapted.stdout)
        self.assertNotIn("private_ranges", adapted.stdout)
        # Exercise the shipped Pi-hole route on loopback with a harmless upstream
        # fixture. No Docker, real Pi-hole, LAN listener or certificate requests.
        route = "http://{$PIHOLE_DOMAIN}" + source.split("http://{$PIHOLE_DOMAIN}", 1)[1]
        route = route.replace("reverse_proxy pihole:80", 'respond "Pi-hole fixture" 200')
        route = route.replace("{\n", "{\n\tbind 127.0.0.1\n", 1)
        for network, expected in [("192.168.68.0/24", 403), ("127.0.0.1/32", 200)]:
            with self.subTest(network=network), tempfile.TemporaryDirectory(prefix="pihole-proxy-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None) as temporary:
                root = Path(temporary)
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                config = root / "Caddyfile"
                config.write_text("{\n admin off\n auto_https off\n persist_config off\n}\n" + route)
                env = dict(environment, PIHOLE_DOMAIN=f"pihole.mathijs.se:{port}", PIHOLE_ALLOWED_NETWORKS=network)
                with (root / "caddy.log").open("w+") as log:
                    process = subprocess.Popen(command + ["run", "--adapter", "caddyfile", "--config", str(config)],
                                               env=env, stdout=log, stderr=log)
                    try:
                        for attempt in range(100):
                            try:
                                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                    break
                            except OSError:
                                if process.poll() is not None or attempt == 99:
                                    log.seek(0)
                                    self.fail(log.read())
                                time.sleep(0.02)
                        for path in ["/", "/admin/", "/api/auth"]:
                            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                            try:
                                connection.request("GET", path, headers={"Host": f"pihole.mathijs.se:{port}",
                                                   "X-Forwarded-For": "192.168.68.42", "Forwarded": "for=192.168.68.42"})
                                response = connection.getresponse()
                                self.assertEqual(response.status, expected)
                                response.read()
                            finally:
                                connection.close()
                    finally:
                        process.terminate()
                        process.wait(timeout=5)
