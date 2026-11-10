use clap::{Parser, ValueEnum};
use homelab_valheim::{
    controller::{Config, Controller},
    engine::DockerEngine,
};
use std::path::PathBuf;

#[derive(Clone, Debug, ValueEnum)]
enum Action {
    Start,
    Stop,
    Restart,
    Status,
    Backup,
    Update,
    World,
    Worlds,
    Idle,
}

#[derive(Parser)]
#[command(about = "Control only the dedicated Valheim Compose project")]
struct Args {
    #[arg(long)]
    config: PathBuf,
    #[arg(value_enum)]
    action: Action,
    world: Option<String>,
}

fn main() -> std::process::ExitCode {
    // All control files and backups are private, even when invoked outside systemd.
    unsafe {
        libc::umask(0o077);
    }
    let args = Args::parse();
    let action = format!("{:?}", args.action).to_ascii_lowercase();
    let result = Config::load(&args.config).and_then(|config| {
        let engine = DockerEngine(config.clone());
        Controller { config, engine }.run(&action, args.world.as_deref())
    });
    match result {
        Ok(message) => {
            println!("{message}");
            std::process::ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("Valheim command failed: {error:#}");
            std::process::ExitCode::FAILURE
        }
    }
}
