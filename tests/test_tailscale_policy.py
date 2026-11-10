"""Controller-only Ansible policy rendering; no tailnet API or live host access."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "ansible/tailscale-policy.yml"


class TailscalePolicyTests(unittest.TestCase):
    def render(self, directory, overrides=None, success=True):
        values = dict(tailscale_policy_owner="owner@example.com",
                      tailscale_policy_friend="friend@example.net",
                      tailscale_policy_receiver_ip="100.64.12.34",
                      tailscale_policy_output_dir=str(directory / "output"))
        values.update(overrides or {})
        inputs = directory / "inputs.json"
        inputs.write_text(json.dumps(values))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(PLAYBOOK),
                                 "-e", f"@{inputs}"], capture_output=True, text=True,
                                cwd=ROOT / "ansible", timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def temporary(self):
        return tempfile.TemporaryDirectory(dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)

    def test_renders_only_explicit_owner_and_backup_grants_idempotently(self):
        with self.temporary() as tmp:
            directory = Path(tmp)
            self.render(directory)
            path = directory / "output/policy.candidate.json"
            policy = json.loads(path.read_text())
            self.assertEqual(policy["grants"], [
                {"src": ["owner@example.com"], "dst": ["*"], "ip": ["*"]},
                {"src": ["friend@example.net"], "dst": ["homelab-backup"], "ip": ["tcp:8000"]}])
            self.assertEqual(policy["hosts"], {"homelab-backup": "100.64.12.34"})
            self.assertNotIn("acls", policy)
            self.assertNotIn("autoApprovers", policy)
            self.assertNotIn("tagOwners", policy)
            self.assertEqual(policy["ssh"], [{"action": "check", "src": ["owner@example.com"],
                                              "dst": ["autogroup:self"],
                                              "users": ["autogroup:nonroot", "root"]}])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn("changed=0", self.render(directory).stdout)

    def test_policy_carries_positive_and_negative_tailscale_assertions(self):
        with self.temporary() as tmp:
            directory = Path(tmp)
            self.render(directory)
            tests = json.loads((directory / "output/policy.candidate.json").read_text())["tests"]
            self.assertEqual(tests[0]["src"], "owner@example.com")
            self.assertIn("homelab-backup:22", tests[0]["accept"])
            self.assertEqual(tests[1]["accept"], ["homelab-backup:8000"])
            self.assertEqual(tests[1]["proto"], "tcp")
            for port in [22, 53, 80, 443, 445, 2283, 8080]:
                self.assertIn(f"homelab-backup:{port}", tests[1]["deny"])
            self.assertEqual(tests[2]["proto"], "udp")
            for port in [8000, 53, 2456, 2457]:
                self.assertIn(f"homelab-backup:{port}", tests[2]["deny"])
            self.assertIn("192.168.68.111:445", tests[1]["deny"])

    def test_custom_backup_endpoint_and_non_email_logins_stay_narrow(self):
        with self.temporary() as tmp:
            directory = Path(tmp)
            self.render(directory, dict(tailscale_policy_owner="owner@github",
                                        tailscale_policy_friend="friend@passkey",
                                        tailscale_policy_receiver_ip="100.127.255.254",
                                        tailscale_policy_backup_port=9000))
            policy = json.loads((directory / "output/policy.candidate.json").read_text())
            self.assertEqual(policy["hosts"]["homelab-backup"], "100.127.255.254")
            self.assertEqual(policy["grants"][1], {"src": ["friend@passkey"],
                                                   "dst": ["homelab-backup"], "ip": ["tcp:9000"]})
            self.assertEqual(policy["tests"][1]["accept"], ["homelab-backup:9000"])
            self.assertIn("homelab-backup:9000", policy["tests"][2]["deny"])

    def test_rejects_broad_invalid_or_overlapping_identities_before_writing(self):
        cases = [dict(tailscale_policy_owner="*"), dict(tailscale_policy_friend="autogroup:shared"),
                 dict(tailscale_policy_friend="friend@example.net\n"),
                 dict(tailscale_policy_friend="owner@example.com"),
                 dict(tailscale_policy_receiver_ip="192.168.68.111"),
                 dict(tailscale_policy_receiver_ip="100.128.1.2"),
                 dict(tailscale_policy_receiver_ip="100.64.256.2"),
                 dict(tailscale_policy_receiver_ip="100.64.001.2"),
                 dict(tailscale_policy_backup_port="8000,22"),
                 dict(tailscale_policy_backup_port=8080),
                 dict(tailscale_policy_backup_port=22), dict(tailscale_policy_backup_port=65536)]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.temporary() as tmp:
                directory = Path(tmp)
                self.render(directory, overrides, success=False)
                self.assertFalse((directory / "output").exists())

    def test_renderer_has_no_remote_provisioning_or_policy_apply(self):
        play = yaml.safe_load(PLAYBOOK.read_text())[0]
        self.assertEqual(play["hosts"], "localhost")
        self.assertEqual(play["connection"], "local")
        self.assertFalse(play["become"])
        self.assertFalse(play["gather_facts"])
        permitted = {"ansible.builtin.assert", "ansible.builtin.file", "ansible.builtin.template",
                     "ansible.builtin.debug"}
        for task in play["tasks"]:
            self.assertEqual(len(set(task) & permitted), 1)
        site = yaml.safe_load((ROOT / "ansible/site.yml").read_text())
        self.assertNotIn("tailscale-policy", str(site))
