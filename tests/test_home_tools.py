"""Local-only Ansible rendering checks for Home Tools DNS integration."""

import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
from threading import Thread
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

    def render(self, local_hosts, with_caddy=False, **overrides):
        with tempfile.TemporaryDirectory(prefix="home-tools-dns-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None) as temporary:
            root = Path(temporary)
            variables = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
            variables.update(compose_bind_ip="192.168.68.111", compose_pihole_password="test-password",
                             compose_pihole_local_hosts=local_hosts)
            variables.update(overrides)
            play = [{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": [{
                "ansible.builtin.template": {"src": str(ROLE / "templates/{{ item }}.j2"),
                                            "dest": str(root / "{{ item }}")},
                "loop": ["compose.yaml", "Caddyfile"],
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
            return (rendered, (root / "Caddyfile").read_text()) if with_caddy else rendered

    def test_local_records_can_be_managed_without_changing_website_or_listeners(self):
        records = ["192.168.68.111 lab.mathijs.se", "192.168.68.20 nas.mathijs.se"]
        original = self.render(None)
        managed = self.render(records)
        self.assertNotIn("FTLCONF_dns_hosts", original["services"]["pihole"]["environment"])
        self.assertEqual(managed["services"]["pihole"]["environment"].pop("FTLCONF_dns_hosts"), ";".join(records))
        self.assertEqual(managed, original)

    def test_host_configuration_prepares_local_tools_and_pihole_records(self):
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        self.assertTrue(host["compose_private_tls_enabled"])
        self.assertEqual(host["compose_cloudflare_api_token"], "{{ vault_cloudflare_api_token }}")
        self.assertEqual(host["compose_pihole_local_hosts"], ["{{ compose_bind_ip }} lab.mathijs.se", "{{ compose_bind_ip }} pihole.mathijs.se", "{{ compose_bind_ip }} photos.mathijs.se", "{{ compose_bind_ip }} shared.mathijs.se"])
        self.assertEqual(host["compose_pihole_domain"], "pihole.mathijs.se")
        self.assertEqual(host["compose_pihole_allowed_networks"], ["192.168.68.0/24"])
        self.assertEqual(host["valheim_dashboard_allowed_hosts"], ["lab.mathijs.se"])
        self.assertEqual(host["valheim_dashboard_proxy_container"], "homelab-proxy-1")
        rendered, caddy = self.render(["192.168.68.111 pihole.mathijs.se"], with_caddy=True,
                               compose_home_tools_enabled=host["compose_home_tools_enabled"],
                               compose_home_tools_domain=host["compose_home_tools_domain"],
                               compose_home_tools_allowed_networks=host["compose_home_tools_allowed_networks"],
                               compose_pihole_domain=host["compose_pihole_domain"],
                               compose_pihole_allowed_networks=host["compose_pihole_allowed_networks"])
        proxy = rendered["services"]["proxy"]
        self.assertEqual(proxy["environment"]["PIHOLE_DOMAIN"], "pihole.mathijs.se")
        self.assertEqual(proxy["environment"]["PIHOLE_ALLOWED_NETWORKS"], "192.168.68.0/24")
        self.assertEqual(proxy["environment"]["HOME_TOOLS_DOMAIN"], "lab.mathijs.se")
        self.assertEqual(proxy["environment"]["HOME_TOOLS_ALLOWED_NETWORKS"], "192.168.68.0/24")
        self.assertEqual(proxy["environment"]["HOME_TOOLS_UPSTREAM"], "192.168.68.111:8787")
        self.assertIn("http://{$HOME_TOOLS_DOMAIN}", caddy)
        disabled, disabled_caddy = self.render(None, with_caddy=True)
        self.assertNotIn("HOME_TOOLS_DOMAIN", disabled["services"]["proxy"]["environment"])
        self.assertNotIn("HOME_TOOLS_DOMAIN", disabled_caddy)
        self.assertEqual(proxy["ports"], ["192.168.68.111:80:80", "192.168.68.111:443:443"])

    def test_caddy_private_routes_check_real_peer_and_preserve_portless_origin(self):
        self.check_private_routes(tls=False)

    def test_cloudflare_tls_covers_public_and_private_hosts_without_changing_routes(self):
        rendered, source = self.render(None, with_caddy=True, compose_home_tools_enabled=True,
                                      compose_private_tls_enabled=True, compose_cloudflare_api_token="test-$literal")
        proxy = rendered["services"]["proxy"]
        self.assertEqual(proxy["environment"]["CF_API_TOKEN"], "test-$$literal")
        self.assertEqual(proxy["build"], {"context": ".", "dockerfile": "Dockerfile.caddy"})
        self.assertEqual(source.count("dns cloudflare {env.CF_API_TOKEN}"), 3)
        self.assertIn("https://{$HOME_TOOLS_DOMAIN}", source)
        self.assertIn("https://{$PIHOLE_DOMAIN}", source)
        self.assertNotIn("test-$literal", source)
        website = source.split("\n\n", 1)[0]
        self.assertTrue(website.startswith("{$WEBSITE_DOMAIN}, {$WEBSITE_WWW_DOMAIN} {\n\ttls {"))
        self.assertIn("dns cloudflare {env.CF_API_TOKEN}", website)
        self.assertIn("\treverse_proxy website:80", website)
        self.assertNotIn("remote_ip", website)
        self.assertEqual((ROLE / "files/caddy.dockerignore").read_text(), "*\n!Dockerfile.caddy\n")

    def test_default_website_keeps_stock_caddy_and_existing_acme_behavior(self):
        rendered, source = self.render(None, with_caddy=True)
        self.assertTrue(source.startswith("{$WEBSITE_DOMAIN}, {$WEBSITE_WWW_DOMAIN} {\n\treverse_proxy website:80\n}"))
        self.assertNotIn("dns cloudflare", source)
        proxy = rendered["services"]["proxy"]
        self.assertEqual(proxy["image"], "caddy:2-alpine")
        self.assertNotIn("build", proxy)
        self.assertNotIn("CF_API_TOKEN", proxy["environment"])

    def test_private_tls_requires_token_without_logging_it(self):
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        validation = next(task for task in tasks if task["name"] == "Require a vaulted Cloudflare token for private trusted HTTPS")
        self.assertTrue(validation["no_log"])
        for token, success in [("", False), (None, False), ("fixture-secret-token", True)]:
            with self.subTest(token_present=bool(token)), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "play.yml"
                path.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
                    "vars": {"compose_private_tls_enabled": True, "compose_cloudflare_api_token": token},
                    "tasks": [validation]}]))
                result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertNotIn("fixture-secret-token", result.stdout + result.stderr)

    def test_caddy_https_private_routes_check_real_peer_and_forward_https(self):
        self.check_private_routes(tls=True)

    def check_private_routes(self, tls):
        command = shlex.split(os.environ.get("CADDY_COMMAND", "caddy"))
        if not shutil.which(command[0]):
            self.skipTest("Install Caddy or set CADDY_COMMAND for proxy integration checks")
        _, source = self.render(None, with_caddy=True, compose_home_tools_enabled=True, compose_immich_enabled=True, compose_private_tls_enabled=tls)
        _, disabled_source = self.render(None, with_caddy=True, compose_private_tls_enabled=tls)
        scheme = "https" if tls else "http"
        environment = dict(os.environ, WEBSITE_DOMAIN="website.example.test",
                           WEBSITE_WWW_DOMAIN="www.website.example.test",
                           PIHOLE_DOMAIN="pihole.mathijs.se", PIHOLE_ALLOWED_NETWORKS="192.168.68.0/24",
                           HOME_TOOLS_DOMAIN="lab.mathijs.se", HOME_TOOLS_ALLOWED_NETWORKS="192.168.68.0/24",
                             HOME_TOOLS_UPSTREAM="192.168.68.111:8787", CF_API_TOKEN="a" * 40,
                             IMMICH_DOMAIN="photos.mathijs.se", IMMICH_ALLOWED_NETWORKS="192.168.68.0/24")
        with tempfile.TemporaryDirectory(prefix="proxy-validate-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None) as temporary:
            config = Path(temporary) / "Caddyfile"
            config.write_text(source)
            adapted = subprocess.run(command + ["adapt", "--adapter", "caddyfile", "--config", str(config), "--validate"],
                                     env=environment, capture_output=True, text=True)
        self.assertEqual(adapted.returncode, 0, adapted.stderr)
        self.assertIn('"192.168.68.0/24"', adapted.stdout)
        self.assertNotIn("private_ranges", adapted.stdout)
        if tls:
            policies = json.loads(adapted.stdout)["apps"]["tls"]["automation"]["policies"]
            for domain_key in ["WEBSITE_DOMAIN", "WEBSITE_WWW_DOMAIN", "PIHOLE_DOMAIN", "HOME_TOOLS_DOMAIN", "IMMICH_DOMAIN"]:
                policy = next(policy for policy in policies if environment[domain_key] in policy.get("subjects", []))
                for issuer in policy["issuers"]:
                    self.assertEqual(issuer["module"], "acme")
                    self.assertEqual(set(issuer["challenges"]), {"dns"})
                    self.assertEqual(issuer["challenges"]["dns"]["provider"]["name"], "cloudflare")
        requests = []

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received = {"host": self.headers.get("Host"), "origin": self.headers.get("Origin"),
                            "token": self.headers.get("X-Valheim-Token"), "method": self.command, "path": self.path,
                            "scheme": self.headers.get("X-Forwarded-Proto")}
                requests.append(received)
                body = json.dumps(received).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        # Local provisioning fixture only; no real Docker, Pi-hole or game server.
        upstream = HTTPServer(("127.0.0.1", 0), Upstream)
        self.addCleanup(upstream.server_close)
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(upstream.shutdown)
        for prefix, route_source in [("PIHOLE", disabled_source), ("HOME_TOOLS", source), ("IMMICH", source)]:
            domain_key = f"{prefix}_DOMAIN"
            host = environment[domain_key]
            marker = scheme + "://{$" + domain_key + "}"
            route = marker + route_source.split(marker, 1)[1].split("\n}", 1)[0] + "\n}"
            route = route.replace("reverse_proxy pihole:80", "reverse_proxy {$HOME_TOOLS_UPSTREAM}")
            route = route.replace("reverse_proxy homelab-immich:2283", "reverse_proxy {$HOME_TOOLS_UPSTREAM}")
            route = route.replace("{\n", "{\n\tbind 127.0.0.1\n", 1)
            for network, expected in [("192.168.68.0/24", 403), ("127.0.0.1/32", 200)]:
                with self.subTest(route=prefix, network=network), tempfile.TemporaryDirectory(prefix="private-proxy-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None) as temporary:
                    root = Path(temporary)
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        port = listener.getsockname()[1]
                    config = root / "Caddyfile"
                    fixture_route = route
                    if tls:
                        # Local certificate fixture only: no ACME calls, DNS changes or installed CA.
                        cert, key = root / "cert.pem", root / "key.pem"
                        generated = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                                                    "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}"], capture_output=True, text=True)
                        self.assertEqual(generated.returncode, 0, generated.stderr)
                        fixture_route = re.sub(r"\ttls \{.*?\n\t\}", f"\ttls {cert} {key}", fixture_route, flags=re.S)
                    config.write_text("{\n admin off\n auto_https off\n persist_config off\n}\n" + fixture_route)
                    env = dict(environment, HOME_TOOLS_UPSTREAM=f"127.0.0.1:{upstream.server_port}")
                    host = environment[domain_key]
                    env[domain_key] = f"{host}:{port}"
                    env[f"{prefix}_ALLOWED_NETWORKS"] = network
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
                            for path in ["/", "/valheim", "/admin/", "/api/session", "/api/auth", "/api/action"]:
                                before = len(requests)
                                method = "POST" if path == "/api/action" else "GET"
                                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                                if tls:
                                    connection.connect()
                                    connection.sock = ssl._create_unverified_context().wrap_socket(
                                        connection.sock, server_hostname=host)
                                try:
                                    connection.request(method, path, body=b'{}', headers={"Host": host, "Origin": f"{scheme}://{host}",
                                                       "X-Forwarded-Proto": "forged",
                                                       "X-Valheim-Token": "test-token", "X-Forwarded-For": "192.168.68.42",
                                                       "Forwarded": "for=192.168.68.42"})
                                    response = connection.getresponse()
                                    self.assertEqual(response.status, expected)
                                    body = response.read()
                                    if expected == 200:
                                        self.assertEqual(json.loads(body), {"host": host, "origin": f"{scheme}://{host}",
                                                         "token": "test-token", "method": method, "path": path, "scheme": scheme})
                                    else:
                                        self.assertEqual(len(requests), before)
                                finally:
                                    connection.close()
                        finally:
                            process.terminate()
                            process.wait(timeout=5)
