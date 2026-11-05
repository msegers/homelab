use clap::{Parser, Subcommand};
use homelab_valheim::dashboard;
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Private Valheim dashboard; HTTP always binds to 127.0.0.1")]
struct Args {
    #[command(subcommand)]
    mode: Mode,
}

#[derive(Subcommand)]
enum Mode {
    Web {
        #[arg(long, default_value_t = 8787)]
        port: u16,
        #[arg(long, default_value = dashboard::SOCKET)]
        socket: PathBuf,
    },
    Broker {
        #[arg(long)]
        config: PathBuf,
        #[arg(long, default_value = dashboard::SOCKET)]
        socket: PathBuf,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    unsafe {
        libc::umask(0o077);
    }
    match Args::parse().mode {
        Mode::Web { port, socket } => dashboard::web(port, socket).await,
        Mode::Broker { config, socket } => dashboard::broker(&socket, config).await,
    }
}
