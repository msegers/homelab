//! Lifecycle operations shared by the CLI and the private dashboard broker.
use crate::{a2s::Observation, archive, engine::Engine, world_seed};
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeSet, HashMap},
    fs::{self, OpenOptions},
    io::Write,
    net::SocketAddr,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Config {
    pub project_dir: PathBuf,
    pub project_name: String,
    pub container: String,
    pub default_world: String,
    pub worlds_dir: PathBuf,
    pub config_dir: PathBuf,
    pub backups_dir: PathBuf,
    #[serde(default)]
    pub required_mount_uuids: HashMap<PathBuf, String>,
    #[serde(default = "backups_enabled")]
    pub pre_change_backups: bool,
    pub state_dir: PathBuf,
    pub stop_timeout: u64,
    pub idle_enabled: bool,
    pub idle_timeout: u64,
    pub poll_seconds: u64,
    pub query_address: SocketAddr,
}

fn backups_enabled() -> bool {
    true
}

#[derive(Deserialize)]
struct StorageMount {
    uuid: Option<String>,
    fstype: String,
    options: String,
}

fn storage_mount(path: &Path, exact: bool) -> Result<StorageMount> {
    let output = std::process::Command::new("/usr/bin/findmnt")
        .args(["--json", if exact { "--mountpoint" } else { "--target" }])
        .arg(path)
        .args(["--output", "UUID,FSTYPE,OPTIONS"])
        .output()
        .context("Cannot inspect storage filesystem")?;
    ensure!(output.status.success(), "Storage filesystem is unavailable");
    let mounts: Value = serde_json::from_slice(&output.stdout)?;
    let mounted = mounts["filesystems"]
        .as_array()
        .and_then(|entries| entries.first())
        .context("Storage filesystem was not found")?;
    Ok(serde_json::from_value(mounted.clone())?)
}

fn verify_storage_mounts(
    required: &HashMap<PathBuf, String>,
    backups: &Path,
    mut probe: impl FnMut(&Path, bool) -> Result<StorageMount>,
) -> Result<()> {
    for (path, uuid) in required {
        let mount = probe(path, true).context("Cannot verify required storage mount")?;
        ensure!(
            mount.uuid.as_ref() == Some(uuid) && mount.fstype == "ext4",
            "Required storage filesystem is missing or has the wrong UUID/type"
        );
        // ProtectSystem=strict makes the array root read-only in the broker's
        // namespace. Only its explicitly allowed backup subdirectory is writable.
    }
    if !required.is_empty() {
        let mount =
            probe(backups, false).context("Cannot inspect backup destination filesystem")?;
        let expected = required
            .iter()
            .filter(|(path, _)| backups.starts_with(path))
            .max_by_key(|(path, _)| path.components().count());
        if let Some((_, uuid)) = expected {
            ensure!(
                mount.uuid.as_ref() == Some(uuid) && mount.fstype == "ext4",
                "Backup destination is not on the required storage filesystem"
            );
        }
        ensure!(
            mount.options.split(',').any(|option| option == "rw")
                && !mount.options.split(',').any(|option| option == "ro"),
            "Backup destination filesystem is read-only"
        );
    }
    Ok(())
}

impl Config {
    /// Fail closed before starting a container or creating a backup. Missing
    /// mounts must never turn a RAID backup path into an SSD directory.
    pub fn verify_storage(&self) -> Result<()> {
        verify_storage_mounts(&self.required_mount_uuids, &self.backups_dir, storage_mount)
    }

    pub fn load(path: &Path) -> Result<Self> {
        let config: Self =
            serde_json::from_slice(&fs::read(path).context("Cannot read control settings")?)
                .context("Invalid control settings; reprovision with Ansible")?;
        ensure!(
            config.stop_timeout > 0 && config.stop_timeout <= 3600,
            "Invalid stop timeout"
        );
        ensure!(
            (15..=600).contains(&config.poll_seconds) && config.idle_timeout >= 60,
            "Invalid idle settings"
        );
        Ok(config)
    }
}

#[cfg(test)]
mod storage_tests {
    use super::*;

    const UUID: &str = "12345678-1234-1234-1234-123456789abc";

    fn mount(uuid: &str, fstype: &str, options: &str) -> StorageMount {
        serde_json::from_value(json!({"uuid": uuid, "fstype": fstype, "options": options})).unwrap()
    }

    fn required() -> HashMap<PathBuf, String> {
        HashMap::from([("/srv/storage".into(), UUID.into())])
    }

    #[test]
    fn sandbox_readonly_array_root_allows_only_writable_backup_submount() {
        let backups = Path::new("/srv/storage/backups/valheim");
        for root_options in ["rw,relatime", "ro,nosuid,relatime"] {
            let mut calls = Vec::new();
            verify_storage_mounts(&required(), backups, |path, exact| {
                calls.push((path.to_path_buf(), exact));
                if exact {
                    assert_eq!(path, Path::new("/srv/storage"));
                    Ok(mount(UUID, "ext4", root_options))
                } else {
                    assert_eq!(path, backups);
                    Ok(mount(UUID, "ext4", "rw,nosuid,relatime"))
                }
            })
            .unwrap();
            assert_eq!(calls.len(), 2);
        }
    }

    #[test]
    fn readonly_or_foreign_backup_destination_is_rejected() {
        for (uuid, fstype, options) in [
            (UUID, "ext4", "ro,relatime"),
            (UUID, "ext4", "rw,relatime,ro"),
            ("ssd-uuid", "ext4", "rw,relatime"),
            (UUID, "nfs", "rw,relatime"),
        ] {
            let result = verify_storage_mounts(
                &required(),
                Path::new("/srv/storage/backups/valheim"),
                |_, exact| {
                    Ok(if exact {
                        mount(UUID, "ext4", "ro,nosuid,relatime")
                    } else {
                        mount(uuid, fstype, options)
                    })
                },
            );
            assert!(result.is_err(), "{uuid} {fstype} {options}");
        }
    }

    #[test]
    fn missing_or_wrong_array_cannot_fall_back_to_a_writable_destination() {
        for missing in [true, false] {
            let result = verify_storage_mounts(
                &required(),
                Path::new("/srv/storage/backups/valheim"),
                |_, exact| {
                    assert!(exact, "Must fail before examining a possible SSD fallback");
                    if missing {
                        bail!("Mount missing");
                    }
                    Ok(mount("ssd-uuid", "ext4", "rw,relatime"))
                },
            );
            assert!(result.is_err());
        }
    }

    #[test]
    fn explicit_existing_ssd_backup_path_is_not_implicitly_migrated() {
        verify_storage_mounts(
            &required(),
            Path::new("/var/lib/homelab-valheim/backups"),
            |_, exact| {
                Ok(if exact {
                    mount(UUID, "ext4", "ro,nosuid,relatime")
                } else {
                    mount("ssd-uuid", "ext4", "rw,relatime")
                })
            },
        )
        .unwrap();
    }
}

#[derive(Clone, Debug, Default)]
pub struct Container {
    pub id: String,
    pub started_at: String,
    pub running: bool,
    pub restarting: bool,
    pub exit_code: i64,
    pub env: HashMap<String, String>,
}

pub fn valid_world(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || byte == b'_' || (index > 0 && byte == b'-')
        })
}

pub fn save_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut temporary =
        tempfile::NamedTempFile::new_in(path.parent().context("Missing state directory")?)?;
    serde_json::to_writer(&mut temporary, value)?;
    temporary.flush()?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}

pub struct Controller<E> {
    pub config: Config,
    pub engine: E,
}

#[derive(Deserialize, Serialize)]
struct IdleState {
    token: [String; 3],
    zero_since: f64,
    last_check: f64,
    last_wall: f64,
}

impl<E: Engine> Controller<E> {
    fn idle_file(&self) -> PathBuf {
        self.config.state_dir.join("idle.json")
    }

    pub fn world(&self) -> Result<String> {
        let path = self.config.state_dir.join("world.json");
        let name = match fs::read(path) {
            Ok(bytes) => serde_json::from_slice::<Value>(&bytes)?["world"]
                .as_str()
                .context("Invalid selected world")?
                .to_owned(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                self.config.default_world.clone()
            }
            Err(error) => return Err(error.into()),
        };
        ensure!(valid_world(&name), "Invalid selected world");
        Ok(name)
    }

    pub fn reset_idle(&self) -> Result<()> {
        match fs::remove_file(self.idle_file()) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    pub fn start(&self, recreate: bool) -> Result<()> {
        self.config.verify_storage()?;
        self.engine.inspect()?; // Refuse another project's container before Compose.
        let world = self.world()?;
        save_json(
            &self.config.state_dir.join("world.json"),
            &json!({"world": world}),
        )?;
        let mut args = vec!["up", "-d", "--no-deps"];
        if recreate {
            args.push("--force-recreate");
        }
        args.push("valheim");
        self.engine.compose(&args, &world, 900)?;
        let info = self
            .engine
            .inspect()?
            .context("Valheim container did not stay running")?;
        ensure!(
            info.running && !info.restarting,
            "Valheim container did not stay running; inspect status/logs"
        );
        ensure!(
            !matches!(self.engine.game_state()?.as_str(), "FATAL" | "BACKOFF"),
            "Valheim game process failed to start; inspect status/logs"
        );
        self.reset_idle()
    }

    pub fn stop(&self) -> Result<()> {
        if self.engine.inspect()?.is_some_and(|info| info.running) {
            self.engine.stop(self.config.stop_timeout)?;
            if let Some(info) = self.engine.inspect()? {
                ensure!(
                    !info.running,
                    "Valheim is still running after stop; refusing to back up live saves"
                );
                ensure!(
                    info.exit_code != 137,
                    "Valheim required SIGKILL; verify world saves before taking a consistent backup"
                );
            }
        }
        self.reset_idle()
    }

    pub fn restart(&self) -> Result<String> {
        self.config.verify_storage()?;
        self.stop()
            .context("Restart aborted: Valheim could not be stopped cleanly")?;
        self.start(true)
            .context("Restart failed after stopping Valheim; check status/logs")?;
        Ok("Valheim container restarted; game initialization may still be in progress".into())
    }

    pub fn status(&self) -> Result<Value> {
        let info = self.engine.inspect()?;
        let mut result = json!({"running": info.as_ref().is_some_and(|i| i.running),
            "selected_world": self.world()?, "idle_enabled": self.config.idle_enabled,
            "idle_timeout_seconds": self.config.idle_timeout});
        result["pre_change_backups"] = self.config.pre_change_backups.into();
        if let Some(info) = info.filter(|i| i.running) {
            result["game_state"] = self
                .engine
                .game_state()
                .unwrap_or_else(|_| "unknown".into())
                .into();
            result["running_world"] = info
                .env
                .get("WORLD_NAME")
                .map(String::as_str)
                .unwrap_or("unknown")
                .into();
            result["players"] = "unknown".into();
            if let Ok(counts) = self.engine.players(&info) {
                result["a2s"] = json!({"info_players": counts.info_players, "player_list_entries": counts.player_list_entries});
                if let Ok(count) = counts.count() {
                    result["players"] = count.into();
                }
            }
        }
        Ok(result)
    }

    pub fn archive(&self) -> Result<String> {
        archive::create(&self.config, &self.world()?)
    }

    fn while_stopped(
        &self,
        label: &str,
        operation: impl FnOnce() -> Result<String>,
    ) -> Result<String> {
        self.config.verify_storage()?;
        let was_running = self.engine.inspect()?.is_some_and(|i| i.running);
        if was_running {
            self.stop()?;
        }
        let result = operation();
        if was_running && let Err(resume) = self.start(false) {
            let outcome = match &result {
                Ok(path) => format!("{label} completed: {path}"),
                Err(error) => format!("{label} failed ({error}); existing backups preserved"),
            };
            bail!("{outcome}; resuming Valheim also failed ({resume}); check status/logs");
        }
        result.map_err(|error| {
            anyhow::anyhow!(
                "{label} failed ({error}); existing backups preserved; {}",
                if was_running {
                    "Valheim resumed"
                } else {
                    "Valheim remains stopped"
                }
            )
        })
    }

    pub fn backup(&self) -> Result<String> {
        self.config.verify_storage()?;
        self.while_stopped("Backup", || self.archive())
    }

    pub fn select_world(&self, name: &str) -> Result<String> {
        ensure!(
            valid_world(name),
            "World names use letters, numbers, underscores, and hyphens"
        );
        if name == self.world()? {
            return Ok(format!("World {name} is already selected"));
        }
        self.while_stopped("World switch", || {
            let backup = self.pre_change_backup()?;
            save_json(
                &self.config.state_dir.join("world.json"),
                &json!({"world": name}),
            )?;
            self.reset_idle()?;
            Ok(format!(
                "Selected world {name}; {backup}; new worlds generate on start"
            ))
        })
    }

    fn pre_change_backup(&self) -> Result<String> {
        if self.config.pre_change_backups {
            Ok(format!("backup: {}", self.archive()?))
        } else {
            Ok("automatic pre-change backup disabled".into())
        }
    }

    /// The control lock covers validation, publication, selection, and resume.
    pub fn create_world(&self, name: &str, seed: Option<&str>) -> Result<String> {
        if let Some(seed) = seed {
            ensure!(
                world_seed::valid_seed(seed),
                world_seed::CreationError::InvalidSeed
            );
        }
        ensure!(
            !name.eq_ignore_ascii_case(&self.world()?),
            world_seed::CreationError::Selected
        );
        if let Some(info) = self
            .engine
            .inspect()?
            .filter(|info| info.running || info.restarting)
        {
            ensure!(
                !info
                    .env
                    .get("WORLD_NAME")
                    .is_some_and(|world| world.eq_ignore_ascii_case(name)),
                world_seed::CreationError::Running
            );
        }
        world_seed::require_new(&self.config.worlds_dir, name)?;
        self.while_stopped("World creation", || {
            let backup = self.pre_change_backup()?;
            world_seed::require_new(&self.config.worlds_dir, name)?;
            if let Some(seed) = seed {
                world_seed::create(&self.config.worlds_dir, name, seed)?;
            }
            // If selection fails, retain the newly created metadata for recovery,
            // never remove or overwrite files on an uncertain failure path.
            save_json(
                &self.config.state_dir.join("world.json"),
                &json!({"world": name}),
            )?;
            self.reset_idle()?;
            Ok(format!(
                "Selected new world {name}; {}; {backup}; {}",
                if seed.is_some() {
                    "custom seed prepared"
                } else {
                    "random seed on first start"
                },
                "terrain generates on start; a previously stopped server stays stopped"
            ))
        })
    }

    /// The caller holds control.lock. Never remove the selected or live world.
    pub fn delete_world(&self, name: &str) -> Result<String> {
        ensure!(valid_world(name), "Invalid world name");
        ensure!(
            !name.contains("_backup_"),
            "Refusing to delete a native world backup"
        );
        ensure!(name != self.world()?, "Cannot delete the selected world");
        if let Some(info) = self
            .engine
            .inspect()?
            .filter(|info| info.running || info.restarting)
        {
            let live = info
                .env
                .get("WORLD_NAME")
                .context("Cannot determine running world")?;
            ensure!(live != name, "Cannot delete the running world");
        }
        // Exact primary-save paths only. Never remove prefix matches or backups.
        let mut paths = Vec::new();
        for suffix in ["", ".db", ".fwl"] {
            let path = self.config.worlds_dir.join(format!("{name}{suffix}"));
            match fs::symlink_metadata(&path) {
                Ok(metadata) => {
                    ensure!(
                        !metadata.file_type().is_symlink(),
                        "Refusing symlink world saves"
                    );
                    ensure!(
                        if suffix.is_empty() {
                            metadata.is_dir()
                        } else {
                            metadata.is_file()
                        },
                        "Unexpected world save type"
                    );
                    paths.push(path);
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) => return Err(error.into()),
            }
        }
        ensure!(!paths.is_empty(), "World not found");
        self.while_stopped("World deletion", || {
            // Never delete the archive destination along with the world, even
            // with hand-edited settings that bypass Ansible's storage checks.
            let destination = fs::canonicalize(&self.config.backups_dir)?;
            for path in &paths {
                let target = fs::canonicalize(path)?;
                ensure!(
                    !destination.starts_with(&target) && !target.starts_with(&destination),
                    "World save overlaps the backup destination; refusing deletion"
                );
                if path.is_dir() {
                    for entry in walkdir::WalkDir::new(path).follow_links(false) {
                        let entry = entry?;
                        let filename = entry.file_name().to_string_lossy();
                        ensure!(
                            !filename.contains("_backup_") && !filename.ends_with(".old"),
                            "World contains backup files; preserve them outside the world directory before deleting"
                        );
                    }
                }
            }
            // Mandatory, regardless of the optional world-switch backup flag.
            // Publication/sync must succeed before any save is removed.
            let backup = self.archive()?;
            for path in paths {
                if path.is_dir() {
                    fs::remove_dir_all(path)?;
                } else {
                    fs::remove_file(path)?;
                }
            }
            Ok(format!(
                "Deleted world {name}; backup: {backup}; existing backups preserved"
            ))
        })
    }

    fn worlds(&self) -> Result<String> {
        let mut names = BTreeSet::from([self.world()?]);
        let backup = regex::Regex::new(r"_backup_(?:auto-)?\d{8}")?;
        for entry in fs::read_dir(&self.config.worlds_dir)? {
            let entry = entry?;
            let path = entry.path();
            let name = if entry.file_type()?.is_dir() {
                path.file_name()
            } else if entry.file_type()?.is_file()
                && path.extension().is_some_and(|e| e == "fwl" || e == "db")
            {
                path.file_stem()
            } else {
                None
            };
            if let Some(name) = name.and_then(|n| n.to_str())
                && valid_world(name)
                && !backup.is_match(name)
            {
                names.insert(name.to_owned());
            }
        }
        Ok(serde_json::to_string(&names)?)
    }

    /// Call while holding control.lock. Clear state before queries so a killed
    /// or timed-out check can never leave a stale countdown for the next poll.
    pub fn idle_at(&self, now: f64, wall: f64, boot: &str) -> String {
        let mut stopping = false;
        let result = (|| -> Result<String> {
            let previous = fs::read(self.idle_file())
                .ok()
                .and_then(|data| serde_json::from_slice::<IdleState>(&data).ok());
            self.reset_idle()?;
            let info = self.engine.inspect()?;
            let Some(info) = info.filter(|i| i.running && self.config.idle_enabled) else {
                return Ok("Idle check: disabled/stopped".into());
            };
            if self.engine.game_state()? != "RUNNING" {
                return Ok(
                    "Idle check: game process unavailable/not running; countdown reset".into(),
                );
            }
            let count = self.engine.players(&info)?.count()?;
            if count > 0 {
                return Ok(format!("Idle check: {count} player(s)"));
            }
            let token = [info.id.clone(), info.started_at.clone(), boot.to_owned()];
            let continuous = previous.filter(|p| {
                let gap = now - p.last_check;
                p.token == token
                    && p.zero_since.is_finite()
                    && p.last_check.is_finite()
                    && p.last_wall.is_finite()
                    && 0.0 <= p.zero_since
                    && p.zero_since <= p.last_check
                    && (0.0..=self.config.poll_seconds as f64 * 3.0).contains(&gap)
                    && (gap - (wall - p.last_wall)).abs() < 5.0
            });
            let since = continuous
                .as_ref()
                .map(|p| p.last_wall)
                .unwrap_or(wall - self.config.poll_seconds as f64);
            if self.engine.recent_activity(since)? {
                return Ok("Idle check: recent connection activity".into());
            }
            let zero_since = continuous.map(|p| p.zero_since).unwrap_or(now);
            if now - zero_since >= self.config.idle_timeout as f64 {
                let latest = self.engine.inspect()?;
                let unchanged = latest.as_ref().is_some_and(|i| {
                    i.running && i.id == info.id && i.started_at == info.started_at
                });
                if !unchanged
                    || self.engine.game_state()? != "RUNNING"
                    || self
                        .engine
                        .players(latest.as_ref().expect("checked above"))?
                        .count()?
                        > 0
                    || self.engine.recent_activity(wall)?
                {
                    return Ok("Idle check: activity/restart during final check".into());
                }
                stopping = true;
                self.stop()?;
                return Ok("Stopped Valheim after confirmed inactivity".into());
            }
            save_json(
                &self.idle_file(),
                &IdleState {
                    token,
                    zero_since,
                    last_check: now,
                    last_wall: wall,
                },
            )?;
            Ok(format!(
                "Idle check: empty for {} seconds",
                (now - zero_since) as u64
            ))
        })();
        match result {
            Ok(message) => message,
            Err(_) => {
                let _ = self.reset_idle();
                if stopping { "Idle shutdown failed; verify container status/logs; no successful shutdown confirmed" }
                else { "Idle check unavailable/uncertain; countdown reset, server left running" }.into()
            }
        }
    }

    pub fn run(&self, action: &str, world: Option<&str>) -> Result<String> {
        self.run_with_seed(action, world, None)
    }

    pub fn run_with_seed(
        &self,
        action: &str,
        world: Option<&str>,
        seed: Option<&str>,
    ) -> Result<String> {
        ensure!(
            seed.is_none() || action == "create-world",
            "Seeds are only allowed when creating a new world"
        );
        ensure!(
            matches!(action, "world" | "delete-world" | "create-world") == world.is_some(),
            "Supply a world name only with world, create-world, or delete-world"
        );
        let lock = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(self.config.state_dir.join("control.lock"))?;
        if action == "idle" {
            match lock.try_lock_exclusive() {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    self.reset_idle()?;
                    return Ok("Idle check skipped: another operation is in progress".into());
                }
                Err(error) => return Err(error.into()),
            }
        } else {
            lock.lock_exclusive()?;
        }
        match action {
            "status" => Ok(serde_json::to_string(&self.status()?)?),
            "start" => {
                self.start(false)?;
                Ok("Started; first installation/download may take several minutes".into())
            }
            "stop" => {
                self.stop()?;
                Ok("Stopped".into())
            }
            "restart" => self.restart(),
            "backup" => self.backup(),
            "world" => self.select_world(world.context("Missing world")?),
            "create-world" => self.create_world(world.context("Missing world")?, seed),
            "delete-world" => self.delete_world(world.context("Missing world")?),
            "worlds" => self.worlds(),
            "update" => {
                let running = self.engine.inspect()?.is_some_and(|i| i.running);
                self.engine
                    .compose(&["pull", "valheim"], &self.world()?, 900)
                    .context("Image pull failed; the existing game was left unchanged")?;
                if running {
                    self.restart()?;
                }
                Ok("Configured image pulled; game binaries update on start".into())
            }
            "idle" => {
                let clocks = (|| -> Result<_> {
                    let uptime = fs::read_to_string("/proc/uptime")?;
                    let now = uptime
                        .split_whitespace()
                        .next()
                        .context("Missing uptime")?
                        .parse::<f64>()?;
                    let wall = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
                    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
                    Ok((now, wall, boot))
                })();
                let (now, wall, boot) = clocks.inspect_err(|_| {
                    let _ = self.reset_idle();
                })?;
                Ok(self.idle_at(now, wall, boot.trim()))
            }
            _ => bail!("Unknown action"),
        }
    }
}

pub fn supported_observation(info: &Container) -> Result<()> {
    let env = |key: &str| {
        info.env
            .get(key)
            .map(|v| v.to_ascii_lowercase())
            .unwrap_or_default()
    };
    ensure!(
        matches!(env("SERVER_PUBLIC").as_str(), "true" | "1")
            && !matches!(env("CROSSPLAY").as_str(), "true" | "1")
            && !env("SERVER_ARGS").contains("-crossplay"),
        "Idle checking requires Steam-only/public-listing A2S queries"
    );
    Ok(())
}

// Keep the query result type visible to test engines without coupling them to UDP.
pub type PlayerObservation = Observation;
