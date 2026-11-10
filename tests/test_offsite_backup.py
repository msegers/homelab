"""Off-site Ansible regressions; no host provisioning or live service changes."""

from pathlib import Path
import shutil
import os
import shlex
import socket
import ssl
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import yaml

import test_storage_services as storage
from test_storage_services import named

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible/roles/offsite_backup"


class OffsiteBackupTests(unittest.TestCase):
    render = storage.StorageServiceTests.render
    temporary = storage.StorageServiceTests.temporary
    run_play = storage.StorageServiceTests.run_play

    def defaults(self, **overrides):
        values = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        values.update(nas_storage_expected_uuid="3b5ea8e0-2a29-42b5-bf10-84323fd768f1",
                      offsite_backup_repository="rest:https://peer.example:8000/homelab/",
                      offsite_backup_password="fixture-encryption-password",
                      offsite_backup_username="homelab",
                      offsite_backup_http_password='fixture-http-"password\\with$characters',
                      offsite_backup_send_enabled=True)
        values.update(overrides)
        return values

    def test_disabled_by_default_and_no_secret_requirements_on_opt_out(self):
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertFalse(defaults["offsite_backup_send_enabled"])
        self.assertFalse(defaults["offsite_backup_receive_enabled"])
        with self.temporary() as tmp:
            tasks = [{"ansible.builtin.include_role": {"name": str(ROLE)}}]
            with patch.dict(os.environ, {"ANSIBLE_ROLES_PATH": str(ROLE.parent)}):
                result = self.run_play(Path(tmp), tasks, defaults, check_mode=True)
            self.assertIn("changed=0", result.stdout)

    def test_sender_validation_rejects_unsafe_endpoints_and_secrets(self):
        guard = named("offsite_backup", "configure", "Validate outgoing backup configuration")
        for overrides, success in [({}, True), ({"offsite_backup_repository": "rest:http://public/foo"}, False),
                                    ({"offsite_backup_repository": "rest:https://user:secret@peer/foo"}, False),
                                    ({"offsite_backup_password": "short"}, False),
                                    ({"offsite_backup_http_password": "secret\ninjected=environment"}, False),
                                    ({"offsite_backup_calendar": "daily\nUnit=evil"}, False)]:
            with self.subTest(overrides=overrides), self.temporary() as tmp:
                self.run_play(Path(tmp), [guard], self.defaults(**overrides), success)

    def test_receiver_rejects_public_wildcard_and_lan_addresses(self):
        guard = named("offsite_backup", "configure", "Validate private receiver configuration")
        for address, success in [("127.0.0.1", True), ("100.64.1.2", True), ("0.0.0.0", False),
                                 ("192.168.68.111", False), ("8.8.8.8", False), ("100.128.1.2", False)]:
            with self.subTest(address=address), self.temporary() as tmp:
                values = self.defaults(offsite_backup_receive_enabled=True,
                    offsite_backup_listen_ip=address, ansible_all_ipv4_addresses=[address],
                    ansible_architecture="x86_64", offsite_backup_htpasswd="fixture",
                    offsite_backup_tls_cert="BEGIN CERTIFICATE", offsite_backup_tls_key="PRIVATE KEY")
                self.run_play(Path(tmp), [guard], values, success)

    def test_source_selection_cannot_include_peer_repositories_or_live_database(self):
        guard = named("offsite_backup", "configure", "Limit source paths to the agreed RAID data and never the incoming repositories")
        for path, success in [("/srv/storage/shared", True), ("/srv/storage/immich", True),
                              ("/srv/storage/backups/valheim", True), ("/srv/storage/backups/peers", False),
                              ("/srv/storage/shared/../backups/peers", False),
                              ("/var/lib/homelab-immich/postgres", False)]:
            with self.subTest(path=path), self.temporary() as tmp:
                self.run_play(Path(tmp), [guard], self.defaults(offsite_backup_sources=[path]), success)

    def test_receiver_rejects_plaintext_auth_and_unrelated_accounts(self):
        auth_guard = named("offsite_backup", "configure", "Require bcrypt receiver authentication entries")
        for entry, success in [("friend:plaintext", False),
                               ("friend:$2b$04$8kgwC7rTtZJuz4zZp02XQeRCAPeGS.x4LTsv6zKSaDwP/URx1UYFq", True)]:
            with self.temporary() as tmp:
                self.run_play(Path(tmp), [auth_guard], self.defaults(
                    offsite_backup_receive_enabled=True, offsite_backup_htpasswd=entry), success)
        account_guard = named("offsite_backup", "receive", "Refuse to take over an unrelated receiver account")
        for account, success in [(None, True), (["x", "990", "990", "", "/nonexistent", "/usr/sbin/nologin"], True),
                                 (["x", "0", "0", "", "/nonexistent", "/usr/sbin/nologin"], False),
                                 (["x", "1000", "1000", "", "/home/friend", "/bin/bash"], False)]:
            with self.temporary() as tmp:
                self.run_play(Path(tmp), [account_guard], {
                    "ansible_facts": {"getent_passwd": {"homelab-rest-server": account}}}, success)

    def test_secret_files_suppress_diffs_and_do_not_restart_applications(self):
        for file, task_name in [("send", "Write outgoing backup secrets without logging or diff output"),
                                ("receive", "Write receiver credentials and TLS material")]:
            task = named("offsite_backup", file, task_name)
            self.assertTrue(task["no_log"])
            self.assertFalse(task["diff"])
        handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
        self.assertEqual([handler["ansible.builtin.systemd_service"].get("name") for handler in handlers],
                         [None, "homelab-rest-server.service"])

    def test_existing_peer_tree_requires_ownership_and_partial_install_can_resume(self):
        guard = named("offsite_backup", "configure", "Refuse to adopt an existing unmanaged incoming repository tree")
        for exists, unit, marker, owner, success in [
                (False, False, False, "root", True),
                (True, False, False, "root", False),
                (True, False, True, "root", False),
                (True, False, True, "homelab-rest-server", True),
                (True, True, True, "homelab-rest-server", True)]:
            with self.subTest(exists=exists, unit=unit, marker=marker, owner=owner), self.temporary() as tmp:
                values = self.defaults(offsite_backup_receive_enabled=True,
                    offsite_backup_existing_peers={"stat": {"exists": exists, "pw_name": owner, "mode": "0700"}},
                    offsite_backup_units={"results": [{"item": "homelab-rest-server.service", "stat": {"exists": unit}}]},
                    offsite_backup_config_ownership={"results": [{}, {"stat": {"isreg": marker}}]})
                self.run_play(Path(tmp), [guard], values, success)

    def test_rendered_units_keep_receive_and_send_permissions_separate(self):
        rendered = self.render("offsite_backup", ["service", "timer", "receiver.service", "client.env", "sources"],
                               **self.defaults())
        sender = rendered["service"]
        receiver = rendered["receiver.service"]
        self.assertIn("--files-from-verbatim", sender)
        self.assertIn("--one-file-system", sender)
        self.assertIn("ExecStartPre=/usr/bin/findmnt", sender)
        self.assertNotIn("--options rw", sender)  # strict sandbox deliberately reads sources only
        self.assertIn("Persistent=false", rendered["timer"])
        for text in [sender, receiver]:
            self.assertIn("BindsTo=srv-storage.mount", text)
            self.assertIn("ProtectSystem=strict", text)
            self.assertNotIn("docker", text)
            self.assertNotIn("forget", text.split("ExecStart=")[1])
            self.assertNotIn("prune", text.split("ExecStart=")[1])
        for flag in ["--append-only", "--private-repos", "--htpasswd-file", "--tls-min-ver 1.3",
                     "--listen 127.0.0.1:8000", "User=homelab-rest-server", "LoadCredential=server.key:",
                     "BindPaths=/srv/storage/backups/peers:/run/homelab-rest-server/repositories"]:
            self.assertIn(flag, receiver)
        self.assertNotIn("--no-auth", receiver)
        self.assertNotIn("PRIVATE KEY", receiver)
        self.assertNotIn("peers", rendered["sources"])
        if shutil.which("systemd-analyze"):
            with self.temporary() as tmp:
                directory = Path(tmp)
                (directory / "srv-storage.mount").write_text(
                    "[Mount]\nWhat=/dev/fixture\nWhere=/srv/storage\nType=ext4\n")
                for source, dest in [("service", "homelab-offsite-backup.service"),
                                     ("timer", "homelab-offsite-backup.timer"),
                                     ("receiver.service", "homelab-rest-server.service")]:
                    (directory / dest).write_text(rendered[source])
                result = subprocess.run(["systemd-analyze", "verify", *map(str, directory.glob("*"))],
                                        capture_output=True, text=True)
                # Binaries need not be installed on the test controller.
                errors = [line for line in result.stderr.splitlines()
                          if "not executable" not in line and "No such file or directory" not in line]
                self.assertEqual(errors, [], result.stderr)

    def test_existing_unmanaged_units_are_not_adopted_even_on_disable(self):
        guard = named("offsite_backup", "main", "Refuse to adopt unmanaged backup units")
        import base64
        for contents, success in [("[Unit]\nDescription=mine\n", False),
                                  ("# Managed by Ansible homelab offsite backup\n", True)]:
            with self.temporary() as tmp:
                values = {"offsite_backup_unit_contents": {"results": [{
                    "item": {"item": "fixture.service", "stat": {"exists": True}},
                    "content": base64.b64encode(contents.encode()).decode()}]}}
                self.run_play(Path(tmp), [guard], values, success)

    def test_real_tls_backup_restore_and_append_only_boundary(self):
        restic = shutil.which(os.environ.get("RESTIC_COMMAND", "restic"))
        server = shutil.which(os.environ.get("REST_SERVER_COMMAND", "rest-server"))
        if not restic or not server or not shutil.which("openssl"):
            self.skipTest("set RESTIC_COMMAND and REST_SERVER_COMMAND for local binary integration")
        with self.temporary() as tmp:
            directory = Path(tmp)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            cert, key = directory / "cert.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1"],
                           check=True, capture_output=True)
            auth = directory / "htpasswd"
            # Public fixture bcrypt hash for fixture-http-password; never a real credential.
            auth.write_text("homelab:$2b$04$8kgwC7rTtZJuz4zZp02XQeRCAPeGS.x4LTsv6zKSaDwP/URx1UYFq\n")
            repo = directory / "repositories"
            repo.mkdir()
            rendered = self.render("offsite_backup", ["receiver.service"], offsite_backup_listen_port=port)
            command = shlex.split(next(line.removeprefix("ExecStart=") for line in
                                      rendered["receiver.service"].splitlines() if line.startswith("ExecStart=")))
            command[0] = server
            command = [str(repo) if arg == "/run/homelab-rest-server/repositories" else
                       str(auth) if arg == "%d/htpasswd" else str(cert) if arg == "%d/server.crt" else
                       str(key) if arg == "%d/server.key" else arg for arg in command]
            with (directory / "server.log").open("w+") as log:
                process = subprocess.Popen(command, stdout=log, stderr=log)
                try:
                    url = f"https://127.0.0.1:{port}"
                    context = ssl.create_default_context(cafile=str(cert))
                    for attempt in range(100):
                        try:
                            urllib.request.urlopen(url, context=context, timeout=1)
                        except urllib.error.HTTPError as error:
                            self.assertEqual(error.code, 401)
                            break
                        except OSError:
                            if process.poll() is not None:
                                log.seek(0)
                                self.fail(log.read())
                            time.sleep(0.05)
                    else:
                        self.fail("receiver did not become ready")
                    env = dict(os.environ, RESTIC_REPOSITORY=f"rest:{url}/homelab/",
                               RESTIC_REST_USERNAME="homelab", RESTIC_REST_PASSWORD="fixture-http-password",
                               RESTIC_PASSWORD="fixture-encryption-password", RESTIC_CACERT=str(cert),
                               RESTIC_CACHE_DIR=str(directory / "cache"))

                    def run(*args, success=True, environment=None):
                        result = subprocess.run([restic, *args], env=environment or env,
                                                capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                        return result

                    run("init")
                    source = directory / "source"
                    source.mkdir()
                    (source / "fixture.txt").write_text("encrypted off-site restore fixture\n")
                    run("backup", str(source))
                    run("check", "--read-data")
                    restored = directory / "restored"
                    run("restore", "latest", "--target", str(restored))
                    self.assertEqual((restored / source.relative_to("/") / "fixture.txt").read_text(),
                                     (source / "fixture.txt").read_text())
                    import json
                    snapshot = json.loads(run("snapshots", "--json").stdout)[0]["id"]
                    # Explicit snapshot ID guarantees an actual delete attempt (keep-last 1
                    # on a one-snapshot repository would be a misleading no-op).
                    result = run("forget", snapshot, success=False)
                    self.assertIn("403", result.stderr)
                    run("snapshots", environment=dict(env, RESTIC_REPOSITORY=f"rest:{url}/other/"), success=False)
                    run("snapshots", environment=dict(env, RESTIC_REST_PASSWORD="wrong"), success=False)
                    self.assertEqual(len(json.loads(run("snapshots", "--json").stdout)), 1)
                finally:
                    process.terminate()
                    process.wait(timeout=10)
