use anyhow::{Result, bail};
use homelab_valheim::{
    a2s::Observation,
    archive,
    controller::{Config, Container, Controller, save_json, supported_observation, valid_world},
    engine::{Engine, parse_inspect},
};
use serde_json::json;
use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, VecDeque},
    fs,
    os::unix::fs::PermissionsExt,
};

#[derive(Default)]
struct Fake {
    info: RefCell<Option<Container>>,
    players: Cell<u8>,
    counts: RefCell<VecDeque<Option<Observation>>>,
    games: RefCell<VecDeque<String>>,
    activity: Cell<bool>,
    starts: Cell<usize>,
    stops: Cell<usize>,
    compose_fail: Cell<bool>,
    stop_mode: Cell<u8>,
    start_mode: Cell<u8>,
}
impl Engine for Fake {
    fn inspect(&self) -> Result<Option<Container>> {
        Ok(self.info.borrow().clone())
    }
    fn compose(&self, args: &[&str], _: &str, _: u64) -> Result<()> {
        if self.compose_fail.get() {
            bail!("Injected Compose failure");
        }
        if args[0] == "up" {
            self.starts.set(self.starts.get() + 1);
            let mut info = self.info.borrow_mut();
            let info = info.as_mut().unwrap();
            info.running = self.start_mode.get() != 1;
            info.restarting = self.start_mode.get() == 2;
        }
        Ok(())
    }
    fn game_state(&self) -> Result<String> {
        Ok(self
            .games
            .borrow_mut()
            .pop_front()
            .unwrap_or_else(|| "RUNNING".into()))
    }
    fn stop(&self, _: u64) -> Result<()> {
        self.stops.set(self.stops.get() + 1);
        let mut info = self.info.borrow_mut();
        let info = info.as_mut().unwrap();
        info.running = self.stop_mode.get() == 1;
        info.exit_code = if self.stop_mode.get() == 2 { 137 } else { 0 };
        Ok(())
    }
    fn players(&self, _: &Container) -> Result<Observation> {
        if let Some(count) = self.counts.borrow_mut().pop_front() {
            return count.ok_or_else(|| anyhow::anyhow!("Injected query failure"));
        }
        Ok(Observation {
            info_players: self.players.get(),
            player_list_entries: self.players.get(),
        })
    }
    fn recent_activity(&self, _: f64) -> Result<bool> {
        Ok(self.activity.get())
    }
}

fn fixture() -> (tempfile::TempDir, Controller<Fake>) {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let config = Config {
        state_dir: root.join("control"),
        default_world: "Home".into(),
        idle_enabled: true,
        container: "valheim-server".into(),
        project_name: "homelab-valheim".into(),
        poll_seconds: 60,
        idle_timeout: 3600,
        worlds_dir: root.join("saves"),
        config_dir: root.join("config"),
        backups_dir: root.join("backups"),
        pre_change_backups: true,
        project_dir: root.join("stack"),
        stop_timeout: 120,
        query_address: "127.0.0.1:2457".parse().unwrap(),
    };
    for path in [
        &config.state_dir,
        &config.worlds_dir,
        &config.config_dir,
        &config.backups_dir,
        &config.project_dir,
    ] {
        fs::create_dir(path).unwrap();
    }
    fs::write(
        config.project_dir.join("docker-compose.yml"),
        "services: {}\n",
    )
    .unwrap();
    let engine = Fake {
        info: RefCell::new(Some(Container {
            id: "container-1".into(),
            started_at: "start-1".into(),
            running: true,
            ..Default::default()
        })),
        ..Default::default()
    };
    (directory, Controller { config, engine })
}
fn tick(controller: &Controller<Fake>, now: u64) -> String {
    controller.idle_at(now as f64, 1_700_000_000.0 + now as f64, "boot-1")
}

#[test]
fn seeded_creation_publishes_private_metadata_then_resumes_only_a_running_server() {
    use std::os::unix::fs::MetadataExt;
    for running in [false, true] {
        let (_tmp, controller) = fixture();
        controller
            .engine
            .info
            .borrow_mut()
            .as_mut()
            .unwrap()
            .running = running;
        fs::write(
            controller.config.worlds_dir.join("Home.db"),
            "previous world",
        )
        .unwrap();
        let message = controller
            .run_with_seed("create-world", Some("NewWorld"), Some("Potato"))
            .unwrap();
        assert!(message.contains("custom seed prepared"));
        assert_eq!(controller.world().unwrap(), "NewWorld");
        let path = controller.config.worlds_dir.join("NewWorld.fwl");
        let bytes = fs::read(&path).unwrap();
        assert_eq!(&bytes[4..8], &36_i32.to_le_bytes());
        assert_eq!(&bytes[8..24], b"\x08NewWorld\x06Potato");
        assert!(!controller.config.worlds_dir.join("NewWorld.db").exists());
        let owner = fs::metadata(&controller.config.worlds_dir).unwrap();
        let metadata = fs::metadata(&path).unwrap();
        assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!((metadata.uid(), metadata.gid()), (owner.uid(), owner.gid()));
        assert_eq!(controller.engine.stops.get(), usize::from(running));
        assert_eq!(controller.engine.starts.get(), usize::from(running));
        assert_eq!(
            controller.engine.info.borrow().as_ref().unwrap().running,
            running
        );
        assert_eq!(
            fs::read(controller.config.worlds_dir.join("Home.db")).unwrap(),
            b"previous world"
        );
        assert_eq!(
            fs::read_dir(&controller.config.backups_dir)
                .unwrap()
                .count(),
            1
        );
        // Starts and later switches must not rewrite seed metadata or state shape.
        controller.start(false).unwrap();
        controller.select_world("Home").unwrap();
        controller.select_world("NewWorld").unwrap();
        assert_eq!(fs::read(path).unwrap(), bytes);
        let state: serde_json::Value = serde_json::from_slice(
            &fs::read(controller.config.state_dir.join("world.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(state, json!({"world":"NewWorld"}));
    }
}

#[test]
fn random_creation_keeps_native_generation_and_optional_backup_policy() {
    let (_tmp, mut controller) = fixture();
    controller.config.pre_change_backups = false;
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    controller.run("create-world", Some("RandomWorld")).unwrap();
    assert_eq!(controller.world().unwrap(), "RandomWorld");
    assert_eq!(
        fs::read_dir(&controller.config.worlds_dir).unwrap().count(),
        0
    );
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        0
    );
    assert_eq!(controller.engine.starts.get(), 0);
    assert!(
        controller
            .run_with_seed("create-world", Some("RandomWorld"), Some("Potato"))
            .is_err()
    );
}

#[test]
fn creation_refuses_existing_saves_incomplete_metadata_and_symlinks_before_stop() {
    for suffix in ["", ".db", ".fwl", ".fwl2", ".db.old", ".symlink"] {
        for seed in [None, Some("Potato")] {
            let (_tmp, controller) = fixture();
            let path = controller
                .config
                .worlds_dir
                .join(format!("NewWorld{suffix}"));
            if suffix.is_empty() {
                fs::create_dir(&path).unwrap();
                fs::write(path.join("chunk.1"), "preserve").unwrap();
            } else if suffix == ".symlink" {
                std::os::unix::fs::symlink("missing", &path).unwrap();
            } else {
                fs::write(&path, "preserve").unwrap();
            }
            assert!(
                controller
                    .run_with_seed("create-world", Some("newworld"), seed)
                    .is_err()
            );
            assert!(fs::symlink_metadata(path).is_ok());
            assert_eq!(controller.world().unwrap(), "Home");
            assert_eq!(controller.engine.stops.get(), 0);
            assert_eq!(controller.engine.starts.get(), 0);
            assert_eq!(
                fs::read_dir(&controller.config.backups_dir)
                    .unwrap()
                    .count(),
                0
            );
        }
    }
}

#[test]
fn deleted_world_name_can_be_reused_without_touching_separately_named_backups() {
    for seed in [None, Some("DikkeTietn")] {
        let (_tmp, controller) = fixture();
        controller
            .engine
            .info
            .borrow_mut()
            .as_mut()
            .unwrap()
            .running = false;
        let saves = &controller.config.worlds_dir;
        let backup = saves.join("Skoltr_backup_auto-20261105-190438");
        fs::create_dir(&backup).unwrap();
        fs::write(backup.join("chunk.1"), "preserved native backup").unwrap();
        let backup_file = saves.join("Skoltr_backup_20261105.fwl");
        fs::write(&backup_file, "preserved backup metadata").unwrap();
        controller
            .run_with_seed("create-world", Some("Skoltr"), seed)
            .unwrap();
        assert_eq!(controller.world().unwrap(), "Skoltr");
        assert_eq!(
            fs::read(backup.join("chunk.1")).unwrap(),
            b"preserved native backup"
        );
        assert_eq!(fs::read(backup_file).unwrap(), b"preserved backup metadata");
        assert_eq!(saves.join("Skoltr.fwl").exists(), seed.is_some());
        assert!(!saves.join("Skoltr").exists());
        assert_eq!(controller.engine.starts.get(), 0);
    }
}

#[test]
fn invalid_creation_requests_never_stop_the_server_or_modify_saves() {
    let (_tmp, controller) = fixture();
    for seed in ["", "a-b", "$(id)", "12345678901", "a\n", "é"] {
        assert!(
            controller
                .run_with_seed("create-world", Some("NewWorld"), Some(seed))
                .is_err()
        );
    }
    for world in ["../escape", "Bad Name", "Home", "home", "X_backup_20261105"] {
        assert!(
            controller
                .run_with_seed("create-world", Some(world), Some("Potato"))
                .is_err()
        );
    }
    for action in ["world", "start", "delete-world", "status"] {
        assert!(
            controller
                .run_with_seed(action, Some("NewWorld"), Some("Potato"))
                .is_err()
        );
    }
    assert_eq!(controller.world().unwrap(), "Home");
    assert_eq!(controller.engine.stops.get(), 0);
    assert_eq!(
        fs::read_dir(&controller.config.worlds_dir).unwrap().count(),
        0
    );
}

#[test]
fn failed_creation_backup_or_unclean_stop_preserves_selection_and_never_publishes_seed() {
    for failure in ["backup", "stop"] {
        let (_tmp, controller) = fixture();
        if failure == "backup" {
            fs::remove_dir(&controller.config.backups_dir).unwrap();
            fs::write(&controller.config.backups_dir, "not a directory").unwrap();
        } else {
            controller.engine.stop_mode.set(2);
        }
        assert!(
            controller
                .run_with_seed("create-world", Some("NewWorld"), Some("Potato"))
                .is_err()
        );
        assert_eq!(controller.world().unwrap(), "Home");
        assert!(!controller.config.worlds_dir.join("NewWorld.fwl").exists());
    }
}

#[test]
fn failed_resume_preserves_completed_seeded_creation_for_retry() {
    let (_tmp, controller) = fixture();
    controller.engine.compose_fail.set(true);
    let error = controller
        .run_with_seed("create-world", Some("NewWorld"), Some("Potato"))
        .unwrap_err();
    assert!(error.to_string().contains("resuming Valheim also failed"));
    assert_eq!(controller.world().unwrap(), "NewWorld");
    let path = controller.config.worlds_dir.join("NewWorld.fwl");
    let bytes = fs::read(&path).unwrap();
    controller.engine.compose_fail.set(false);
    controller.start(false).unwrap();
    assert_eq!(fs::read(path).unwrap(), bytes);
}

#[test]
fn world_switch_can_skip_backups_but_deletion_always_creates_a_recoverable_archive() {
    let (_tmp, mut controller) = fixture();
    controller.config.pre_change_backups = false;
    let saves = &controller.config.worlds_dir;
    fs::write(saves.join("Home.db"), "home").unwrap();
    fs::write(saves.join("Other.db"), "other").unwrap();
    fs::write(saves.join("Other.fwl"), "metadata").unwrap();
    fs::write(saves.join("Other_backup_auto-20261105.db"), "backup").unwrap();
    fs::write(saves.join("Other.db.old"), "old database").unwrap();
    fs::write(saves.join("Other.fwl.old"), "old metadata").unwrap();
    let native_backup = saves.join("Other_backup_auto-20261105");
    fs::create_dir(&native_backup).unwrap();
    fs::write(native_backup.join("chunk.1"), "native backup").unwrap();
    let world_dir = saves.join("Other");
    fs::create_dir(&world_dir).unwrap();
    fs::write(world_dir.join("chunk.1"), "world data").unwrap();
    fs::write(saves.join("Other2.db"), "unrelated").unwrap();
    controller.select_world("NewWorld").unwrap();
    assert_eq!(fs::read(saves.join("Home.db")).unwrap(), b"home");
    assert!(controller.engine.info.borrow().as_ref().unwrap().running);
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        0
    );
    assert!(controller.run("worlds", None).unwrap().contains("Other"));
    // Fake engine must expose a known running world for deletion safety checks.
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .env
        .insert("WORLD_NAME".into(), "NewWorld".into());
    controller.run("delete-world", Some("Other")).unwrap();
    assert!(!saves.join("Other.db").exists());
    assert!(!saves.join("Other.fwl").exists());
    assert!(saves.join("Other2.db").exists());
    assert!(saves.join("Other_backup_auto-20261105.db").exists());
    assert_eq!(
        fs::read(native_backup.join("chunk.1")).unwrap(),
        b"native backup"
    );
    assert_eq!(
        fs::read(saves.join("Other.db.old")).unwrap(),
        b"old database"
    );
    assert_eq!(
        fs::read(saves.join("Other.fwl.old")).unwrap(),
        b"old metadata"
    );
    for backup in ["Other_backup_auto-20261105", "Other_backup_20261105"] {
        fs::write(saves.join(format!("{backup}.fwl")), "backup metadata").unwrap();
        assert!(controller.run("delete-world", Some(backup)).is_err());
        assert!(saves.join(format!("{backup}.fwl")).exists());
    }
    let archives: Vec<_> = fs::read_dir(&controller.config.backups_dir)
        .unwrap()
        .collect();
    assert_eq!(archives.len(), 1);
    let archive = fs::File::open(archives[0].as_ref().unwrap().path()).unwrap();
    let restored = tempfile::tempdir().unwrap();
    tar::Archive::new(flate2::read::GzDecoder::new(archive))
        .unpack(restored.path())
        .unwrap();
    assert_eq!(
        fs::read(restored.path().join("saves/Other.db")).unwrap(),
        b"other"
    );
    assert_eq!(
        fs::read(restored.path().join("saves/Other.fwl")).unwrap(),
        b"metadata"
    );
    assert_eq!(
        fs::read(restored.path().join("saves/Other/chunk.1")).unwrap(),
        b"world data"
    );
    assert!(!world_dir.exists());
    assert!(controller.engine.info.borrow().as_ref().unwrap().running);
}

#[test]
fn deletion_refuses_selected_running_unknown_and_symlink_worlds() {
    let (_tmp, controller) = fixture();
    fs::write(controller.config.worlds_dir.join("Other.db"), "keep").unwrap();
    assert!(controller.run("delete-world", Some("Home")).is_err());
    assert!(controller.run("delete-world", Some("Other")).is_err()); // unknown live world
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .env
        .insert("WORLD_NAME".into(), "Other".into());
    assert!(controller.run("delete-world", Some("Other")).is_err());
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    for name in ["../Other", "-flag", "", "Missing"] {
        assert!(controller.run("delete-world", Some(name)).is_err());
    }
    std::os::unix::fs::symlink(
        controller.config.worlds_dir.join("Other.db"),
        controller.config.worlds_dir.join("Link.db"),
    )
    .unwrap();
    assert!(controller.run("delete-world", Some("Link")).is_err());
    assert!(controller.config.worlds_dir.join("Other.db").exists());
}

#[test]
fn deletion_backs_up_first_and_aborts_if_backup_fails() {
    let (_tmp, mut controller) = fixture();
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    let save = controller.config.worlds_dir.join("Other.db");
    fs::write(&save, "keep").unwrap();
    let original = controller.config.backups_dir.clone();
    controller.config.backups_dir = original.join("missing");
    assert!(controller.run("delete-world", Some("Other")).is_err());
    assert!(save.exists());
    controller.config.backups_dir = original;
    controller.run("delete-world", Some("Other")).unwrap();
    assert!(!save.exists());
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn failed_mandatory_deletion_archive_preserves_saves_backups_and_selection() {
    for failure in [
        "missing destination",
        "invalid destination",
        "missing source",
    ] {
        let (_tmp, mut controller) = fixture();
        controller.config.pre_change_backups = false;
        controller
            .engine
            .info
            .borrow_mut()
            .as_mut()
            .unwrap()
            .env
            .insert("WORLD_NAME".into(), "Home".into());
        let save = controller.config.worlds_dir.join("Other.db");
        let metadata = controller.config.worlds_dir.join("Other.fwl");
        fs::write(&save, "keep database").unwrap();
        fs::write(&metadata, "keep metadata").unwrap();
        let previous = controller.config.backups_dir.join("previous.tar.gz");
        fs::write(&previous, "keep previous archive").unwrap();
        match failure {
            "missing destination" => {
                controller.config.backups_dir = controller.config.backups_dir.join("missing")
            }
            "invalid destination" => controller.config.backups_dir = previous.clone(),
            "missing source" => {
                fs::remove_file(controller.config.project_dir.join("docker-compose.yml")).unwrap()
            }
            _ => unreachable!(),
        }
        assert!(
            controller.run("delete-world", Some("Other")).is_err(),
            "{failure}"
        );
        assert_eq!(fs::read(&save).unwrap(), b"keep database", "{failure}");
        assert_eq!(fs::read(&metadata).unwrap(), b"keep metadata", "{failure}");
        assert_eq!(
            fs::read(&previous).unwrap(),
            b"keep previous archive",
            "{failure}"
        );
        assert_eq!(controller.world().unwrap(), "Home");
        assert!(controller.engine.info.borrow().as_ref().unwrap().running);
        assert_eq!(controller.engine.stops.get(), 1);
        assert_eq!(controller.engine.starts.get(), 1);
    }
}

#[test]
fn deletion_refuses_to_remove_a_world_containing_the_backup_destination() {
    let (_tmp, mut controller) = fixture();
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    let world = controller.config.worlds_dir.join("Other");
    fs::create_dir(&world).unwrap();
    fs::write(world.join("chunk.1"), "world data").unwrap();
    controller.config.backups_dir = world.join("backups");
    fs::create_dir(&controller.config.backups_dir).unwrap();
    let previous = controller.config.backups_dir.join("previous.tar.gz");
    fs::write(&previous, "previous archive").unwrap();
    assert!(
        controller
            .run("delete-world", Some("Other"))
            .unwrap_err()
            .to_string()
            .contains("overlaps")
    );
    assert_eq!(fs::read(world.join("chunk.1")).unwrap(), b"world data");
    assert_eq!(fs::read(&previous).unwrap(), b"previous archive");
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn deletion_refuses_to_recursively_remove_embedded_backups() {
    for filename in ["chunk.1.old", "Other_backup_auto-20261105"] {
        let (_tmp, controller) = fixture();
        controller
            .engine
            .info
            .borrow_mut()
            .as_mut()
            .unwrap()
            .running = false;
        let world = controller.config.worlds_dir.join("Other");
        fs::create_dir(&world).unwrap();
        fs::write(world.join("chunk.1"), "world data").unwrap();
        fs::write(world.join(filename), "embedded backup").unwrap();
        assert!(controller.run("delete-world", Some("Other")).is_err());
        assert_eq!(fs::read(world.join("chunk.1")).unwrap(), b"world data");
        assert_eq!(fs::read(world.join(filename)).unwrap(), b"embedded backup");
    }
}
fn prime(controller: &Controller<Fake>) {
    for now in (0..3600).step_by(60) {
        tick(controller, now);
    }
}
fn idle_file(controller: &Controller<Fake>) -> std::path::PathBuf {
    controller.config.state_dir.join("idle.json")
}

#[test]
fn idle_stops_only_after_full_hour() {
    let (_tmp, controller) = fixture();
    prime(&controller);
    assert_eq!(controller.engine.stops.get(), 0);
    assert!(tick(&controller, 3600).contains("Stopped Valheim"));
    assert_eq!(controller.engine.stops.get(), 1);
    assert!(!idle_file(&controller).exists());
}

#[test]
fn player_failure_disagreement_activity_and_unhealthy_process_reset_countdown() {
    for condition in [
        "player", "query", "disagree", "activity", "STARTING", "FATAL", "BACKOFF", "STOPPED",
        "unknown",
    ] {
        let (_tmp, controller) = fixture();
        prime(&controller);
        match condition {
            "player" => controller.engine.players.set(1),
            "query" => controller.engine.counts.borrow_mut().push_back(None),
            "disagree" => controller
                .engine
                .counts
                .borrow_mut()
                .push_back(Some(Observation {
                    info_players: 0,
                    player_list_entries: 1,
                })),
            "activity" => controller.engine.activity.set(true),
            state => controller.engine.games.borrow_mut().push_back(state.into()),
        }
        tick(&controller, 3600);
        assert_eq!(controller.engine.stops.get(), 0, "{condition}");
        assert!(!idle_file(&controller).exists(), "{condition}");
        controller.engine.players.set(0);
        controller.engine.activity.set(false);
        tick(&controller, 3660);
        let state: serde_json::Value =
            serde_json::from_slice(&fs::read(idle_file(&controller)).unwrap()).unwrap();
        assert_eq!(state["zero_since"], 3660.0);
    }
}

#[test]
fn final_query_and_process_failure_prevent_stop() {
    for fail_game in [false, true] {
        let (_tmp, controller) = fixture();
        prime(&controller);
        if fail_game {
            controller
                .engine
                .games
                .borrow_mut()
                .extend(["RUNNING".into(), "BACKOFF".into()]);
        } else {
            controller.engine.counts.borrow_mut().extend([
                Some(Observation {
                    info_players: 0,
                    player_list_entries: 0,
                }),
                Some(Observation {
                    info_players: 1,
                    player_list_entries: 1,
                }),
            ]);
        }
        tick(&controller, 3600);
        assert_eq!(controller.engine.stops.get(), 0);
        assert!(!idle_file(&controller).exists());
    }
}

#[test]
fn gaps_restart_boot_and_wall_clock_changes_reset_countdown() {
    for reason in ["gap", "restart", "boot", "clock"] {
        let (_tmp, controller) = fixture();
        tick(&controller, 0);
        let now = if reason == "gap" { 4000.0 } else { 60.0 };
        if reason == "restart" {
            controller
                .engine
                .info
                .borrow_mut()
                .as_mut()
                .unwrap()
                .started_at = "new".into();
        }
        controller.idle_at(
            now,
            1_700_000_000.0 + now + if reason == "clock" { 100.0 } else { 0.0 },
            if reason == "boot" { "new" } else { "boot-1" },
        );
        let state: serde_json::Value =
            serde_json::from_slice(&fs::read(idle_file(&controller)).unwrap()).unwrap();
        assert_eq!(state["zero_since"], now, "{reason}");
        assert_eq!(controller.engine.stops.get(), 0);
    }
}

#[test]
fn corrupt_countdown_is_never_reused() {
    for bytes in [
        "not json",
        "[]",
        r#"{"token":["container-1","start-1","boot-1"],"zero_since":-10000,"last_check":0,"last_wall":1700000000}"#,
    ] {
        let (_tmp, controller) = fixture();
        fs::write(idle_file(&controller), bytes).unwrap();
        tick(&controller, 60);
        let state: serde_json::Value =
            serde_json::from_slice(&fs::read(idle_file(&controller)).unwrap()).unwrap();
        assert_eq!(state["zero_since"], 60.0);
        assert_eq!(controller.engine.stops.get(), 0);
    }
}

#[test]
fn disabled_and_stopped_games_are_never_started() {
    let (_tmp, mut controller) = fixture();
    controller.config.idle_enabled = false;
    tick(&controller, 0);
    controller.config.idle_enabled = true;
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    tick(&controller, 4000);
    assert_eq!(controller.engine.starts.get(), 0);
    assert_eq!(controller.engine.stops.get(), 0);
}

#[test]
fn interrupted_query_clears_state_before_work() {
    let (_tmp, controller) = fixture();
    tick(&controller, 0);
    controller.engine.counts.borrow_mut().push_back(None);
    tick(&controller, 60);
    assert!(!idle_file(&controller).exists());
}

#[test]
fn world_selection_freezes_and_world_switch_preserves_stopped_state() {
    let (_tmp, mut controller) = fixture();
    controller.start(false).unwrap();
    controller.config.default_world = "ChangedDefault".into();
    assert_eq!(controller.world().unwrap(), "Home");
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    controller.select_world("OtherWorld").unwrap();
    assert_eq!(controller.world().unwrap(), "OtherWorld");
    assert!(!controller.engine.info.borrow().as_ref().unwrap().running);
    assert_eq!(controller.engine.starts.get(), 1);
}

#[test]
fn consistent_archive_contains_current_world_and_private_permissions() {
    let (_tmp, controller) = fixture();
    let world = controller.config.worlds_dir.join("MyWorld");
    fs::create_dir(&world).unwrap();
    fs::write(world.join("chunk.1"), "saved data").unwrap();
    fs::write(world.join("_main.1.fwl2"), "metadata").unwrap();
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .running = false;
    let path = controller.backup().unwrap();
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let mut archive =
        tar::Archive::new(flate2::read::GzDecoder::new(fs::File::open(path).unwrap()));
    let names: Vec<_> = archive
        .entries()
        .unwrap()
        .map(|entry| entry.unwrap().path().unwrap().into_owned())
        .collect();
    assert!(names.contains(&"saves/MyWorld/chunk.1".into()));
    assert!(names.contains(&"world.json".into()));
    assert!(names.contains(&"docker-compose.yml".into()));
    assert_eq!(controller.engine.starts.get(), 0);
}

#[test]
fn running_backup_resumes_but_failed_archive_preserves_earlier_backups() {
    let (_tmp, mut controller) = fixture();
    let old = controller.backup().unwrap();
    let contents = fs::read(&old).unwrap();
    assert_eq!(controller.engine.stops.get(), 1);
    assert_eq!(controller.engine.starts.get(), 1);
    controller.config.worlds_dir = controller.config.worlds_dir.join("missing");
    assert!(
        controller
            .backup()
            .unwrap_err()
            .to_string()
            .contains("Valheim resumed")
    );
    assert_eq!(fs::read(&old).unwrap(), contents);
    assert_eq!(controller.engine.starts.get(), 2);
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn backup_collision_and_publication_errors_preserve_existing_archives() {
    let (_tmp, controller) = fixture();
    let path = archive::create_named(&controller.config, "Home", "fixed.tar.gz").unwrap();
    let old = fs::read(&path).unwrap();
    assert!(archive::create_named(&controller.config, "Home", "fixed.tar.gz").is_err());
    assert_eq!(fs::read(&path).unwrap(), old);
    fs::create_dir(controller.config.backups_dir.join("directory.tar.gz")).unwrap();
    assert!(archive::create_named(&controller.config, "Home", "directory.tar.gz").is_err());
    assert!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".partial"))
    );
}

#[test]
fn backup_rejects_sources_inside_destination_and_excludes_nested_backups() {
    let (_tmp, mut controller) = fixture();
    controller.config.config_dir = controller.config.backups_dir.clone();
    assert!(
        controller
            .archive()
            .unwrap_err()
            .to_string()
            .contains("inside the backup destination")
    );
    let (_tmp, mut controller) = fixture();
    controller.config.backups_dir = controller.config.config_dir.join("backups");
    fs::create_dir(&controller.config.backups_dir).unwrap();
    fs::write(
        controller.config.backups_dir.join("previous.tar.gz"),
        "secret previous backup",
    )
    .unwrap();
    let path = controller.archive().unwrap();
    let mut archive =
        tar::Archive::new(flate2::read::GzDecoder::new(fs::File::open(path).unwrap()));
    assert!(archive.entries().unwrap().all(|entry| {
        !entry
            .unwrap()
            .path()
            .unwrap()
            .to_string_lossy()
            .contains("backups")
    }));
}

#[test]
fn unclean_stops_abort_before_archiving_or_resuming() {
    for (mode, message) in [(1, "still running"), (2, "SIGKILL")] {
        let (_tmp, controller) = fixture();
        controller.engine.stop_mode.set(mode);
        assert!(
            controller
                .backup()
                .unwrap_err()
                .to_string()
                .contains(message)
        );
        assert_eq!(
            fs::read_dir(&controller.config.backups_dir)
                .unwrap()
                .count(),
            0
        );
        assert_eq!(controller.engine.starts.get(), 0);
    }
}

#[test]
fn successful_backup_is_reported_when_resume_fails() {
    let (_tmp, controller) = fixture();
    controller.engine.compose_fail.set(true);
    let error = controller.backup().unwrap_err().to_string();
    assert!(error.contains("Backup completed:"));
    assert!(error.contains("resuming Valheim also failed"));
    assert_eq!(
        fs::read_dir(&controller.config.backups_dir)
            .unwrap()
            .count(),
        1
    );
}

#[test]
fn both_archive_and_resume_failure_are_reported_and_world_is_unchanged() {
    let (_tmp, mut controller) = fixture();
    controller.config.worlds_dir = controller.config.worlds_dir.join("missing");
    controller.engine.compose_fail.set(true);
    let error = controller
        .select_world("OtherWorld")
        .unwrap_err()
        .to_string();
    assert!(error.contains("World switch failed"));
    assert!(error.contains("resuming Valheim also failed"));
    assert_eq!(controller.world().unwrap(), "Home");
}

#[test]
fn failed_pull_does_not_stop_game_and_restart_failure_is_explicit() {
    let (_tmp, controller) = fixture();
    controller.engine.compose_fail.set(true);
    assert!(
        controller
            .run("update", None)
            .unwrap_err()
            .to_string()
            .contains("existing game was left unchanged")
    );
    assert_eq!(controller.engine.stops.get(), 0);
    assert!(
        controller
            .restart()
            .unwrap_err()
            .to_string()
            .contains("Restart failed after stopping")
    );
    assert_eq!(controller.engine.stops.get(), 1);
}

#[test]
fn fatal_and_exited_start_failures_are_not_successes() {
    for state in ["FATAL", "BACKOFF", "exited", "restarting"] {
        let (_tmp, controller) = fixture();
        match state {
            "exited" => controller.engine.start_mode.set(1),
            "restarting" => controller.engine.start_mode.set(2),
            state => controller.engine.games.borrow_mut().push_back(state.into()),
        }
        assert!(controller.start(false).is_err(), "{state}");
    }
}

#[test]
fn inspect_ownership_refuses_other_projects_and_services() {
    let (_tmp, controller) = fixture();
    for (project, service) in [
        ("homelab", "proxy"),
        ("homelab", "pihole"),
        ("homelab", "website"),
        ("homelab-valheim", "other"),
    ] {
        let info = json!([{"Config":{"Labels":{"com.docker.compose.project":project,"com.docker.compose.service":service}},"State":{"Running":true}}]);
        assert!(
            parse_inspect(&controller.config, &info.to_string())
                .unwrap_err()
                .to_string()
                .contains("another project/service")
        );
    }
}

#[test]
fn unsupported_modes_and_invalid_world_names_fail_closed() {
    for env in [
        HashMap::new(),
        HashMap::from([("SERVER_PUBLIC".into(), "false".into())]),
        HashMap::from([
            ("SERVER_PUBLIC".into(), "true".into()),
            ("CROSSPLAY".into(), "true".into()),
        ]),
        HashMap::from([
            ("SERVER_PUBLIC".into(), "true".into()),
            ("SERVER_ARGS".into(), "-crossplay".into()),
        ]),
    ] {
        assert!(
            supported_observation(&Container {
                env,
                ..Default::default()
            })
            .is_err()
        );
    }
    for world in ["", "../world", "-world", "name;whoami", "a\n"] {
        assert!(!valid_world(world));
    }
    for world in ["Home", "new-world_2", "_home", "1"] {
        assert!(valid_world(world));
    }
}

#[test]
fn status_exposes_only_player_counts_not_game_password() {
    let (_tmp, controller) = fixture();
    controller
        .engine
        .info
        .borrow_mut()
        .as_mut()
        .unwrap()
        .env
        .insert("SERVER_PASS".into(), "secret-value".into());
    let status = controller.status().unwrap();
    assert_eq!(status["players"], 0);
    assert!(!status.to_string().contains("secret-value"));
}

#[test]
fn selected_world_json_is_private_and_invalid_selection_is_rejected() {
    let (_tmp, controller) = fixture();
    let path = controller.config.state_dir.join("world.json");
    save_json(&path, &json!({"world":"Saved"})).unwrap();
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(controller.world().unwrap(), "Saved");
    save_json(&path, &json!({"world":"../bad"})).unwrap();
    assert!(controller.world().is_err());
}

#[test]
fn contending_idle_check_skips_and_discards_stale_countdown() {
    let (_tmp, controller) = fixture();
    tick(&controller, 0);
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(controller.config.state_dir.join("control.lock"))
        .unwrap();
    fs2::FileExt::lock_exclusive(&lock).unwrap();
    assert!(
        controller
            .run("idle", None)
            .unwrap()
            .contains("another operation")
    );
    assert!(!idle_file(&controller).exists());
    assert_eq!(controller.engine.stops.get(), 0);
}
