//! Mutating/game-specific Docker operations, with explicit context/project/service.
//! The separate metrics module only invokes fixed read-only ps/stats commands.
use crate::{
    a2s,
    controller::{Config, Container, PlayerObservation, supported_observation},
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;
use std::{
    collections::HashMap,
    io::{Read, Seek},
    process::{Command, Stdio},
    time::Duration,
};
use wait_timeout::ChildExt;

pub trait Engine {
    fn inspect(&self) -> Result<Option<Container>>;
    fn compose(&self, args: &[&str], world: &str, timeout: u64) -> Result<()>;
    fn game_state(&self) -> Result<String>;
    fn stop(&self, timeout: u64) -> Result<()>;
    fn players(&self, info: &Container) -> Result<PlayerObservation>;
    fn recent_activity(&self, since: f64) -> Result<bool>;
}

pub struct DockerEngine(pub Config);

struct Output {
    success: bool,
    stdout: String,
    stderr: String,
}

impl DockerEngine {
    fn docker(&self, args: &[&str], timeout: u64, world: Option<&str>) -> Result<Output> {
        // Unlinked 0600 files avoid pipe deadlocks on large logs. Captured inspect
        // output may contain the game password and is never included in errors.
        let mut stdout = tempfile::tempfile()?;
        let mut stderr = tempfile::tempfile()?;
        let mut command = Command::new("/usr/bin/docker");
        command
            .args(["--context", "default"])
            .args(args)
            .current_dir(&self.0.project_dir)
            .env_clear()
            .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
            .env("HOME", "/root")
            .env("LANG", "C.UTF-8")
            .stdout(Stdio::from(stdout.try_clone()?))
            .stderr(Stdio::from(stderr.try_clone()?));
        if let Some(world) = world {
            command.env("VALHEIM_WORLD", world);
        }
        let mut child = command.spawn().context("Docker command unavailable")?;
        let status = match child.wait_timeout(Duration::from_secs(timeout))? {
            Some(status) => status,
            None => {
                let _ = child.kill();
                let _ = child.wait();
                bail!("Docker command timed out; verify status before retrying");
            }
        };
        const MAX_OUTPUT: u64 = 8 * 1024 * 1024;
        ensure!(
            stdout.metadata()?.len() <= MAX_OUTPUT && stderr.metadata()?.len() <= MAX_OUTPUT,
            "Docker response too large; countdown must reset"
        );
        stdout.rewind()?;
        stderr.rewind()?;
        let mut out = String::new();
        let mut err = String::new();
        stdout.read_to_string(&mut out)?;
        stderr.read_to_string(&mut err)?;
        Ok(Output {
            success: status.success(),
            stdout: out,
            stderr: err,
        })
    }

    fn checked(&self, args: &[&str], timeout: u64, world: Option<&str>) -> Result<Output> {
        let result = self.docker(args, timeout, world)?;
        ensure!(
            result.success,
            "Docker operation failed; check daemon/container logs"
        );
        Ok(result)
    }
}

pub fn parse_inspect(config: &Config, bytes: &str) -> Result<Container> {
    let entries: Vec<Value> =
        serde_json::from_str(bytes).context("Invalid Docker inspect response")?;
    let info = entries.first().context("Empty inspect response")?;
    let labels = &info["Config"]["Labels"];
    ensure!(
        labels["com.docker.compose.project"] == config.project_name
            && labels["com.docker.compose.service"] == "valheim",
        "Container name belongs to another project/service; refusing to control it"
    );
    let env = info["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|s| s.split_once('='))
        .map(|(key, value)| (key.into(), value.into()))
        .collect::<HashMap<_, _>>();
    Ok(Container {
        id: info["Id"].as_str().context("Missing container ID")?.into(),
        started_at: info["State"]["StartedAt"]
            .as_str()
            .context("Missing start timestamp")?
            .into(),
        running: info["State"]["Running"]
            .as_bool()
            .context("Missing running state")?,
        restarting: info["State"]["Restarting"].as_bool().unwrap_or(false),
        exit_code: info["State"]["ExitCode"].as_i64().unwrap_or(-1),
        env,
    })
}

impl Engine for DockerEngine {
    fn inspect(&self) -> Result<Option<Container>> {
        let result = self.docker(&["inspect", &self.0.container], 30, None)?;
        if !result.success {
            let message = result.stderr.to_ascii_lowercase();
            if message.contains("no such object") || message.contains("no such container") {
                return Ok(None);
            }
            bail!("Docker Engine unavailable");
        }
        Ok(Some(parse_inspect(&self.0, &result.stdout)?))
    }

    fn compose(&self, args: &[&str], world: &str, timeout: u64) -> Result<()> {
        let project = self
            .0
            .project_dir
            .to_str()
            .context("Invalid project path")?;
        let file = self.0.project_dir.join("docker-compose.yml");
        let mut command = vec![
            "compose",
            "--project-directory",
            project,
            "--project-name",
            &self.0.project_name,
            "--profile",
            "game",
            "-f",
            file.to_str().context("Invalid Compose path")?,
        ];
        command.extend_from_slice(args);
        self.checked(&command, timeout, Some(world))?;
        Ok(())
    }

    fn game_state(&self) -> Result<String> {
        let result = self.docker(
            &[
                "exec",
                &self.0.container,
                "supervisorctl",
                "status",
                "valheim-server",
            ],
            15,
            None,
        )?;
        let mut words = result.stdout.split_whitespace();
        Ok(if words.next() == Some("valheim-server") {
            words.next().unwrap_or("unknown")
        } else {
            "unknown"
        }
        .into())
    }

    fn stop(&self, timeout: u64) -> Result<()> {
        self.checked(
            &["stop", "--time", &timeout.to_string(), &self.0.container],
            timeout + 30,
            None,
        )?;
        Ok(())
    }

    fn players(&self, info: &Container) -> Result<PlayerObservation> {
        supported_observation(info)?;
        a2s::query(self.0.query_address)
    }

    fn recent_activity(&self, since: f64) -> Result<bool> {
        ensure!(since.is_finite() && since >= 0.0, "Invalid log timestamp");
        let result = self.checked(
            &[
                "logs",
                "--timestamps",
                "--since",
                &format!("{since:.6}"),
                &self.0.container,
            ],
            15,
            None,
        )?;
        let pattern = regex::Regex::new(
            r"(?i)Got connection SteamID|Got character ZDOID|New connection|Closing socket|player.*(?:joined|connected|disconnected)",
        )?;
        Ok(pattern.is_match(&result.stdout) || pattern.is_match(&result.stderr))
    }
}
