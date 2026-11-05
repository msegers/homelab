#!/usr/bin/env python3
"""Explicit Valheim operations and a fail-open, systemd-driven idle check."""

import argparse
from datetime import datetime, timezone
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import uuid


# Runs INSIDE the game image, which already includes python-a2s. Require both
# Steam responses to agree; private/crossplay servers are deliberately excluded.
PLAYER_QUERY = """
import a2s, json, os
address = ('127.0.0.1', int(os.environ.get('SERVER_PORT', '2456')) + 1)
info = a2s.info(address, timeout=3)
players = a2s.players(address, timeout=3)
print(json.dumps({'count': info.player_count, 'listed': len(players)}))
"""
ACTIVITY = re.compile(
    r"Got connection SteamID|Got character ZDOID|New connection|Closing socket|"
    r"player.*(?:joined|connected|disconnected)", re.IGNORECASE,
)


class ControlError(RuntimeError):
    pass


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(value, stream)
    temporary.replace(path)


class Controller:
    def __init__(self, config):
        self.config = config
        self.state_dir = Path(config["state_dir"])
        self.idle_file = self.state_dir / "idle.json"
        self.world_file = self.state_dir / "world.json"

    def world(self):
        selected = self.config["default_world"]
        if self.world_file.exists():
            selected = json.loads(self.world_file.read_text())["world"]
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", selected):
            raise ControlError("Invalid selected world")
        return selected

    def docker(self, *args, timeout=30, check=True, environment=None):
        command = ["/usr/bin/docker", "--context", "default", *args]
        try:
            result = subprocess.run(command, text=True, capture_output=True,
                                    timeout=timeout, env=environment, cwd=self.config["project_dir"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError("Docker command failed/timed out") from exc
        if check and result.returncode:
            # Do not leak captured inspect/config output, which can contain secrets.
            raise ControlError("Docker operation failed; check daemon/container logs")
        return result

    def compose(self, *args, timeout=300):
        environment = dict(os.environ, VALHEIM_WORLD=self.world())
        return self.docker(
            "compose", "--project-directory", self.config["project_dir"],
            "--project-name", self.config["project_name"], "--profile", "game",
            "-f", str(Path(self.config["project_dir"]) / "docker-compose.yml"),
            *args, timeout=timeout, environment=environment,
        )

    def inspect(self):
        result = self.docker("inspect", self.config["container"], check=False)
        if result.returncode:
            if "No such object" in result.stderr or "No such container" in result.stderr:
                return None
            raise ControlError("Docker Engine unavailable")
        info = json.loads(result.stdout)[0]
        labels = info.get("Config", {}).get("Labels") or {}
        if (labels.get("com.docker.compose.project") != self.config["project_name"]
                or labels.get("com.docker.compose.service") != "valheim"):
            raise ControlError("Container name belongs to another project/service; refusing to control it")
        return info

    def running(self):
        info = self.inspect()
        return bool(info and info["State"]["Running"])

    def reset_idle(self):
        self.idle_file.unlink(missing_ok=True)

    def start(self, recreate=False):
        self.inspect()  # Refuse a conflicting container owned by another project.
        # Freeze the selected world on first start, independently of future
        # Ansible default changes. Only the explicit world command changes it.
        save_json(self.world_file, {"world": self.world()})
        args = ["up", "-d", "--no-deps"]
        if recreate:
            args.append("--force-recreate")
        self.compose(*args, "valheim", timeout=900)
        self.reset_idle()

    def stop(self):
        if self.running():
            self.docker("stop", "--time", str(self.config["stop_timeout"]),
                        self.config["container"], timeout=self.config["stop_timeout"] + 30)
        self.reset_idle()

    def player_count(self, info):
        env = dict(entry.split("=", 1) for entry in info["Config"].get("Env", []) if "=" in entry)
        if (env.get("SERVER_PUBLIC", "").lower() not in ("true", "1")
                or env.get("CROSSPLAY", "false").lower() in ("true", "1")
                or "-crossplay" in env.get("SERVER_ARGS", "").lower()):
            raise ControlError("Idle checking requires Steam-only/public-listing A2S queries")
        result = self.docker("exec", self.config["container"], "python3", "-c", PLAYER_QUERY, timeout=15)
        counts = json.loads(result.stdout)
        if not isinstance(counts, dict):
            raise ControlError("Invalid player query response")
        count = counts.get("count")
        if type(count) is not int or count < 0 or count != counts.get("listed"):
            raise ControlError("Player query responses disagree")
        return count

    def recent_activity(self, since):
        timestamp = datetime.fromtimestamp(since, timezone.utc).isoformat()
        logs = self.docker("logs", "--timestamps", "--since", timestamp,
                           self.config["container"], timeout=15)
        return bool(ACTIVITY.search(logs.stdout + logs.stderr))

    def idle(self):
        try:
            try:
                previous = json.loads(self.idle_file.read_text())
            except (OSError, ValueError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            # A killed/timed-out check must not leave a stale countdown behind.
            self.reset_idle()
            info = self.inspect()
            if not self.config["idle_enabled"] or not info or not info["State"]["Running"]:
                self.reset_idle()
                return "Idle check: disabled/stopped"
            count = self.player_count(info)
            if count:
                self.reset_idle()
                return f"Idle check: {count} player(s)"
            now, wall = time.monotonic(), time.time()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            token = [info["Id"], info["State"]["StartedAt"], boot]
            values = [previous.get(key) for key in ("zero_since", "last_check", "last_wall")]
            if (not all(type(value) in (int, float) and math.isfinite(value) for value in values)
                    or not 0 <= values[0] <= values[1] <= now):
                previous = {}
            gap = now - previous.get("last_check", now)
            wall_gap = wall - previous.get("last_wall", wall)
            continuous = (previous.get("token") == token and 0 <= gap <= self.config["poll_seconds"] * 3
                          and abs(gap - wall_gap) < 5)
            since = previous["last_wall"] if continuous else wall - self.config["poll_seconds"]
            if self.recent_activity(since):
                self.reset_idle()
                return "Idle check: recent connection activity"
            zero_since = previous["zero_since"] if continuous else now
            if now - zero_since >= self.config["idle_timeout"]:
                # Recheck immediately before stopping; serialize against our user commands.
                latest = self.inspect()
                if (not latest or latest["Id"] != info["Id"]
                        or latest["State"]["StartedAt"] != info["State"]["StartedAt"]
                        or self.player_count(latest) or self.recent_activity(wall)):
                    self.reset_idle()
                    return "Idle check: activity/restart during final check"
                self.stop()
                return "Stopped Valheim after confirmed inactivity"
            save_json(self.idle_file, {"token": token, "zero_since": zero_since,
                                      "last_check": now, "last_wall": wall})
            return f"Idle check: empty for {int(now - zero_since)} seconds"
        except Exception:
            self.reset_idle()
            return "Idle check unavailable/uncertain; countdown reset, server left running"

    def archive(self):
        directory = Path(self.config["backups_dir"])
        filename = "manual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8] + ".tar.gz"
        path = directory / filename
        temporary = directory / (filename + ".partial")
        entries = [(Path(self.config["worlds_dir"]), "saves"),
                   (Path(self.config["config_dir"]), "config"),
                   (Path(self.config["project_dir"]) / "docker-compose.yml", "docker-compose.yml")]
        try:
            with open(temporary, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    selection = json.dumps({"world": self.world()}).encode()
                    metadata = tarfile.TarInfo("world.json")
                    metadata.size, metadata.mode = len(selection), 0o600
                    archive.addfile(metadata, io.BytesIO(selection))
                    for source, name in entries:
                        if source.exists():
                            def exclude_backups(member):
                                relative = member.name[len(name):].lstrip("/")
                                candidate = source / relative if source.is_dir() else source
                                return None if candidate.resolve().is_relative_to(directory.resolve()) else member
                            archive.add(source, arcname=name, filter=exclude_backups)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(path)

    def backup(self):
        was_running = self.running()
        if was_running:
            self.stop()
        try:
            return self.archive()
        finally:
            if was_running:
                self.start()

    def select_world(self, name):
        if not name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name):
            raise ControlError("World names use letters, numbers, underscores, and hyphens")
        was_running = self.running()
        if was_running:
            self.stop()
        try:
            self.archive()
            save_json(self.world_file, {"world": name})
            self.reset_idle()
        finally:
            if was_running:
                self.start()
        return f"Selected world {name}; new worlds generate on start"

    def status(self):
        info = self.inspect()
        result = {"running": bool(info and info["State"]["Running"]), "selected_world": self.world()}
        if result["running"]:
            env = dict(entry.split("=", 1) for entry in info.get("Config", {}).get("Env", []) if "=" in entry)
            result["running_world"] = env.get("WORLD_NAME", "unknown")
            try:
                result["players"] = self.player_count(info)
            except (ControlError, ValueError, KeyError):
                result["players"] = "unknown"
        return json.dumps(result)

    def run(self, action, world=None):
        with open(self.state_dir / "control.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if action == "idle" else 0))
            except BlockingIOError:
                return "Idle check skipped: another operation is in progress"
            if action == "idle":
                return self.idle()
            if action == "status":
                return self.status()
            if action == "worlds":
                paths = Path(self.config["worlds_dir"]).iterdir()
                names = {p.name if p.is_dir() else p.stem for p in paths
                         if (p.is_dir() or p.suffix == ".fwl")
                         and not re.search(r"_backup_(?:auto-)?\d{8}", p.name)}
                return json.dumps(sorted(names | {self.world()}))
            if action == "world":
                return self.select_world(world)
            if action == "backup":
                return self.backup()
            if action == "stop":
                self.stop()
                return "Stopped"
            if action == "start":
                self.start()
                return "Started; first installation/download may take several minutes"
            if action == "restart":
                self.stop()
                self.start(recreate=True)
                return "Restarted"
            if action == "update":
                was_running = self.running()
                if was_running:
                    self.stop()
                try:
                    self.compose("pull", "valheim", timeout=900)
                finally:
                    if was_running:
                        self.start(recreate=True)
                return "Configured image pulled; game binaries update on start"
            raise ControlError("Unknown action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "backup", "update", "world", "worlds", "idle"])
    parser.add_argument("world", nargs="?")
    args = parser.parse_args()
    if (args.action == "world") != (args.world is not None):
        parser.error("Supply a world name only with the world action")
    os.umask(0o077)
    try:
        controller = Controller(json.loads(Path(args.config).read_text()))
        print(controller.run(args.action, args.world))
    except (ControlError, OSError, ValueError, KeyError) as exc:
        print(f"Valheim command failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
