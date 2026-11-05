"""Read-only Ansible deployment compatibility checks using synthetic secrets."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class VaultMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="vault-migration-",
            dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None,
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.password = self.root / "vault-password"
        self.password.write_text("synthetic-test-vault-password\n")
        self.password.chmod(0o600)
        self.values = {
            "vault_compose_pihole_password": "existing-pihole-test-secret",
            "vault_valheim_server_password": "existing-valheim-test-secret",
        }

    def encrypt(self, name, values):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(values))
        path.chmod(0o600)
        subprocess.run(
            ["ansible-vault", "encrypt", "--vault-password-file", str(self.password), str(path)],
            check=True, capture_output=True,
        )
        return path

    def compare(self, old, new):
        variables = self.root / "paths.json"
        variables.write_text(json.dumps({
            "previous_vault_file": str(old),
            "current_vault_file": str(new),
        }))
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,",
             str(ROOT / "ansible/verify-vault-migration.yml"),
             "--vault-password-file", str(self.password), "-e", "@" + str(variables)],
            cwd=self.root, capture_output=True, text=True,
            env=dict(os.environ, ANSIBLE_NOCOLOR="1"),
        )
        for secret in [*self.values.values(), "changed-test-secret"]:
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def test_moving_ciphertext_preserves_decryption_and_both_passwords(self):
        old = self.encrypt("elitedesk/vault.yml", self.values)
        original = old.read_bytes()
        new = self.root / "homelab/vault.yml"
        new.parent.mkdir()
        shutil.copy2(old, new)
        self.assertEqual(original, new.read_bytes())
        result = self.compare(old, new)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=0", result.stdout)
        self.assertEqual(original, old.read_bytes())
        self.assertEqual(original, new.read_bytes())

    def test_reencryption_and_new_keys_do_not_require_password_rotation(self):
        old = self.encrypt("old.yml", {"vault_compose_pihole_password": self.values["vault_compose_pihole_password"]})
        new = self.encrypt("new.yml", self.values)
        self.assertNotEqual(old.read_bytes(), new.read_bytes())
        result = self.compare(old, new)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_changed_or_missing_existing_password_fails_without_leaking_values(self):
        old = self.encrypt("old.yml", self.values)
        for key in self.values:
            for missing in [False, True]:
                with self.subTest(key=key, missing=missing):
                    values = dict(self.values)
                    if missing:
                        values.pop(key)
                    else:
                        values[key] = "changed-test-secret"
                    new = self.encrypt("new.yml", values)
                    before = new.read_bytes()
                    self.assertNotEqual(self.compare(old, new).returncode, 0)
                    self.assertEqual(before, new.read_bytes())


if __name__ == "__main__":
    unittest.main()
