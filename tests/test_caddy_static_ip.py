"""Ansible-only fixed proxy IP checks; no Docker daemon or live services used."""

import json
from pathlib import Path
import subprocess
import unittest

import yaml

import test_docker_networking as networking
import test_home_tools as home

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ansible/roles/compose"


def task(file, name):
    return next(item for item in yaml.safe_load((COMPOSE / f"tasks/{file}.yml").read_text())
                if item["name"] == name)


class CaddyStaticIpTests(unittest.TestCase):
    render = home.HomeToolsDnsTests.render
    run_tasks = networking.DockerNetworkingTests.run_tasks

    def check(self, guard, variables, success):
        result = self.run_tasks([guard], variables)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_opt_in_adds_only_proxy_bridge_and_preserves_shared_network_and_volumes(self):
        original = self.render(None)
        fixed = self.render(None, compose_caddy_static_ip_enabled=True)
        self.assertNotIn("networks", original)
        self.assertEqual(fixed["networks"]["default"], {})
        self.assertEqual(fixed["services"]["proxy"]["networks"], {
            "default": {}, "caddy_egress": {"ipv4_address": "172.30.255.2", "gw_priority": 100}})
        self.assertEqual(fixed["networks"]["caddy_egress"]["ipam"]["config"], [{
            "subnet": "172.30.255.0/28", "gateway": "172.30.255.1", "ip_range": "172.30.255.8/29"}])
        self.assertEqual(fixed["networks"]["caddy_egress"]["driver_opts"], {
            "com.docker.network.bridge.name": "br-hl-caddy"})
        for service in ["website", "pihole"]:
            self.assertEqual(original["services"][service], fixed["services"][service])
        del fixed["services"]["proxy"]["networks"]
        del fixed["networks"]
        self.assertEqual(fixed, original)

    def test_prefix_validation_rejects_public_noncanonical_and_invalid_addresses(self):
        guard = task("caddy_network", "Validate the private canonical Caddy network prefix")
        for prefix, success in [("172.30.255", True), ("10.45.3", True), ("192.168.200", True),
                                ("8.8.8", False), ("172.32.1", False), ("172.30.256", False),
                                ("172.030.1", False), ("172.30.1.2", False)]:
            with self.subTest(prefix=prefix):
                self.check(guard, {"compose_caddy_network_prefix": prefix}, success)

    def test_versions_must_support_real_gateway_priority(self):
        guard = task("caddy_network", "Require gateway-priority support rather than silently using a dynamic source IP")
        for compose, api, success in [("v2.33.1", "1.48", True), ("5.1.3", "1.52", True),
                                      ("2.33.0", "1.48", False), ("2.40.0", "1.47", False)]:
            with self.subTest(compose=compose, api=api):
                self.check(guard, {"compose_caddy_versions": {"results": [
                    {"stdout": compose}, {"stdout": api}]}}, success)

    def test_route_overlap_guards_cover_broad_routes_subnets_and_single_addresses(self):
        guard = task("caddy_network", "Refuse overlap with existing host LAN VPN or Docker routes")
        for route, existing, success in [
                ({"dst": "default", "dev": "eno1"}, False, True),
                ({"dst": "192.168.68.0/24", "dev": "eno1"}, False, True),
                ({"dst": "172.18.0.0/16", "dev": "br-app"}, False, True),
                ({"dst": "172.30.255.16/28", "dev": "br-other"}, False, True),
                ({"dst": "172.30.0.0/16", "dev": "tailscale0"}, False, False),
                ({"dst": "172.30.255.0/24", "dev": "eno1"}, False, False),
                ({"dst": "172.30.255.8/29", "dev": "br-other"}, False, False),
                ({"dst": "172.30.255.2", "dev": "lo"}, False, False),
                ({"dst": "172.30.255.0/28", "dev": "br-hl-caddy"}, False, False),
                ({"dst": "172.30.255.0/28", "dev": "br-hl-caddy"}, True, True),
                ({"dst": "172.30.255.1", "dev": "br-hl-caddy"}, True, True),
                ({"dst": "172.30.255.0/28", "dev": "br-other"}, True, False)]:
            with self.subTest(route=route, existing=existing):
                self.check(guard, {"compose_caddy_network_prefix": "172.30.255",
                                   "compose_caddy_network": {"exists": existing},
                                   "compose_caddy_host_routes": {"stdout": json.dumps([route])}}, success)

    def network(self):
        return {"Driver": "bridge", "Internal": False, "EnableIPv6": False,
                "Labels": {"com.docker.compose.project": "homelab",
                           "com.docker.compose.network": "caddy_egress",
                           "se.mathijs.homelab.caddy-static-ip": "true"},
                "Options": {"com.docker.network.bridge.name": "br-hl-caddy"},
                "IPAM": {"Driver": "default", "Config": [{"Subnet": "172.30.255.0/28", "Gateway": "172.30.255.1",
                                      "IPRange": "172.30.255.8/29"}]}}

    def test_existing_bridge_cannot_be_adopted_or_silently_reconfigured(self):
        guard = task("caddy_network", "Refuse to adopt or reconfigure a conflicting reserved Caddy network")
        for field, value in [(None, None), ("Labels", {}), ("Internal", True),
                             ("Options", {}), ("IPAM", {"Driver": "default", "Config": []})]:
            with self.subTest(field=field):
                network = self.network()
                if field:
                    network[field] = value
                self.check(guard, {"compose_caddy_network_prefix": "172.30.255",
                                   "compose_caddy_network": {"exists": True, "network": network}}, field is None)
        self.check(guard, {"compose_caddy_network_prefix": "172.30.255",
                           "compose_caddy_network": {"exists": False}}, True)

    def test_unexpected_or_conflicting_bridge_members_are_rejected(self):
        guard = task("caddy_network", "Reject unexpected endpoints on the Caddy-only bridge")
        for name, address, success in [("homelab-proxy-1", "172.30.255.2/28", True),
                                       ("unrelated", "172.30.255.2/28", False),
                                       ("homelab-proxy-1", "172.30.255.8/28", False)]:
            with self.subTest(name=name, address=address):
                self.check(guard, {"compose_caddy_network_prefix": "172.30.255",
                    "compose_caddy_network": {"exists": True, "network": {"Containers": {
                        "fixture": {"Name": name, "IPv4Address": address}}}}}, success)
        self.check(guard, {"compose_caddy_network": {"exists": False}}, True)

    def test_verification_requires_static_ip_and_actual_host_bound_source(self):
        endpoint = {"NetworkID": "net", "EndpointID": "endpoint", "IPAddress": "172.30.255.2",
                    "IPPrefixLen": 28, "Gateway": "172.30.255.1", "GwPriority": 100,
                    "IPAMConfig": {"IPv4Address": "172.30.255.2"}}
        guard = task("verify_caddy_ip", "Require Caddy's reserved endpoint and preferred default gateway")
        for change, success in [({}, True), ({"IPAddress": "172.30.255.8"}, False),
                                ({"GwPriority": 0}, False), ({"IPAMConfig": None}, False)]:
            with self.subTest(change=change):
                networks = {"homelab_caddy_egress": dict(endpoint, **change), "homelab_default": {}}
                self.check(guard, {"compose_caddy_network_prefix": "172.30.255",
                    "compose_network_inspections": {"results": [{"item": "proxy", "stdout": json.dumps({
                        "networks": networks})}]}}, success)
        route_guard = task("verify_caddy_ip", "Refuse a dynamic proxy source even when the containers are healthy")
        for source, success in [("172.30.255.2", True), ("172.18.0.3", False),
                                ("172.30.255.20", False), ("172x30x255.2", False)]:
            self.check(route_guard, {"compose_caddy_network_prefix": "172.30.255", "compose_caddy_route": {
                "stdout": f"192.168.68.111 via 172.30.255.1 dev eth0 src {source} uid 0\n"}}, success)

    def test_targeted_apply_recreates_only_proxy_and_preflight_never_writes(self):
        tasks = yaml.safe_load((COMPOSE / "tasks/main.yml").read_text())
        scoped = next(item for item in tasks if item["name"] == "Apply only the Caddy network change when explicitly requested")
        self.assertEqual(scoped["tags"], ["never", "caddy-ip"])
        module = scoped["community.docker.docker_compose_v2"]
        self.assertEqual(module["services"], ["proxy"])
        self.assertFalse(module["dependencies"])
        self.assertEqual(module["pull"], "never")
        self.assertEqual(module["build"], "never")
        preflight = next(i for i, item in enumerate(tasks) if item["name"].startswith("Preflight the reserved"))
        render = next(i for i, item in enumerate(tasks) if item["name"] == "Render Compose configuration from Ansible variables")
        self.assertLess(preflight, render)
        allowed = {"ansible.builtin.assert", "ansible.builtin.command", "community.docker.docker_network_info"}
        for item in yaml.safe_load((COMPOSE / "tasks/caddy_network.yml").read_text()):
            self.assertEqual(len(allowed & item.keys()), 1)
            if "ansible.builtin.command" in item:
                self.assertFalse(item["changed_when"])
                self.assertFalse(item["check_mode"])
        host = yaml.safe_load((ROOT / "ansible/host_vars/homelab/main.yml").read_text())
        defaults = yaml.safe_load((COMPOSE / "defaults/main.yml").read_text())
        self.assertTrue(host["compose_caddy_static_ip_enabled"])
        self.assertFalse(defaults["compose_caddy_static_ip_enabled"])

    def test_real_tag_selection_does_not_run_normal_stack_or_other_services(self):
        result = subprocess.run(["ansible-playbook", "site.yml", "--list-tasks", "--tags", "caddy-ip",
                                 "-i", "inventory.example.ini"], cwd=ROOT / "ansible",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Apply only the Caddy network change when explicitly requested", result.stdout)
        self.assertIn("Refuse overlap with existing host LAN VPN or Docker routes", result.stdout)
        self.assertIn("Refuse a dynamic proxy source even when the containers are healthy", result.stdout)
        for forbidden in ["Start homelab stack", "Apply only Pi-hole DNS changes", "docker :", "immich :", "samba :"]:
            self.assertNotIn(forbidden, result.stdout)

    def test_dns_only_first_migration_is_rejected_and_verifier_commands_are_read_only(self):
        guard = task("caddy_network", "Refuse a DNS-only first migration of the proxy network")
        for exists in [False, True]:
            result = self.run_tasks([dict(guard, tags=["pihole-dns"])],
                                    {"compose_caddy_network": {"exists": exists}}, tags="pihole-dns")
            self.assertEqual(result.returncode == 0, exists, result.stdout + result.stderr)
        for item in yaml.safe_load((COMPOSE / "tasks/verify_caddy_ip.yml").read_text()):
            self.assertTrue("ansible.builtin.assert" in item or "ansible.builtin.command" in item)
            if "ansible.builtin.command" in item:
                self.assertFalse(item["changed_when"])
                self.assertFalse(item["check_mode"])
                self.assertEqual(item["ansible.builtin.command"]["argv"][:8],
                                 ["docker", "--context", "default", "exec", "homelab-proxy-1", "ip", "-4", "route"])
