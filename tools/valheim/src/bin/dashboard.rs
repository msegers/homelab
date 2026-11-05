use clap::{Parser, Subcommand};
use homelab_valheim::dashboard;
use ipnet::Ipv4Net;
use std::{net::Ipv4Addr, path::PathBuf};

#[derive(Parser)]
#[command(
    about = "Private Valheim dashboard; explicit LAN/loopback bind and client network allowlist"
)]
struct Args {
    #[command(subcommand)]
    mode: Mode,
}

#[derive(Subcommand)]
enum Mode {
    Web {
        #[arg(long, default_value = "127.0.0.1")]
        bind: Ipv4Addr,
        #[arg(long = "allow-network", default_value = "127.0.0.0/8")]
        networks: Vec<Ipv4Net>,
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
        Mode::Web {
            port,
            socket,
            bind,
            networks,
        } => dashboard::web(port, socket, dashboard::Access::new(bind, networks)?).await,
        Mode::Broker { config, socket } => dashboard::broker(&socket, config).await,
    }
}
