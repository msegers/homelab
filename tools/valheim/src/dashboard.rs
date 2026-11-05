//! Private LAN/loopback HTTP UI talks to a Unix socket, never to Docker or sudo.
use crate::{
    controller::{Config, Controller},
    engine::DockerEngine,
};
use anyhow::{Context, Result, ensure};
use axum::{
    Json, Router,
    extract::{ConnectInfo, DefaultBodyLimit, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Redirect, Response},
    routing::{get, post},
};
use ipnet::Ipv4Net;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{UnixListener, UnixStream},
    sync::Mutex,
    time::timeout,
};

pub const SOCKET: &str = "/run/valheim-dashboard/control.sock";
const MAX_REQUEST: u64 = 4096;
const MAX_RESPONSE: u64 = 65536;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Status,
    Start,
    Stop,
    Restart,
    Worlds,
    World,
    Delete,
}
impl Action {
    fn name(self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Restart => "restart",
            Self::Worlds => "worlds",
            Self::World => "world",
            Self::Delete => "delete-world",
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Command {
    pub action: Action,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub world: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confirmation: Option<String>,
}

impl Command {
    fn validate(&self) -> Result<()> {
        let needs_world = matches!(self.action, Action::World | Action::Delete);
        ensure!(
            needs_world == self.world.is_some(),
            "Unexpected or missing world name"
        );
        if let Some(world) = &self.world {
            ensure!(crate::controller::valid_world(world), "Invalid world name");
        }
        if matches!(self.action, Action::Delete) {
            ensure!(
                self.confirmation == self.world,
                "Type the world name to confirm deletion"
            );
        } else {
            ensure!(self.confirmation.is_none(), "Unexpected confirmation");
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Reply {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}
impl Reply {
    fn success(result: Value) -> Self {
        Self {
            ok: true,
            result: Some(result),
            error: None,
        }
    }
    fn failure(message: &str) -> Self {
        Self {
            ok: false,
            result: None,
            error: Some(message.into()),
        }
    }
    fn response(self) -> Response {
        (
            if self.ok {
                StatusCode::OK
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            },
            Json(self),
        )
            .into_response()
    }
}

#[derive(Clone)]
pub struct WebState {
    pub socket: PathBuf,
    pub token: String,
    pub access: Access,
}

#[derive(Clone)]
pub struct Access {
    pub bind: Ipv4Addr,
    networks: Vec<Ipv4Net>,
    hosts: Vec<String>,
}

impl Access {
    pub fn new(bind: Ipv4Addr, networks: Vec<Ipv4Net>) -> Result<Self> {
        ensure!(
            bind.is_loopback() || bind.is_private(),
            "Dashboard must bind to a loopback or RFC1918 LAN address, not a wildcard/public address"
        );
        ensure!(
            !networks.is_empty(),
            "At least one allowed private network is required"
        );
        for network in &networks {
            ensure!(
                (network.network().is_private() && network.broadcast().is_private())
                    || (network.network().is_loopback() && network.broadcast().is_loopback()),
                "Allowed networks must be entirely RFC1918 private or loopback ranges"
            );
        }
        ensure!(
            networks.iter().any(|network| network.contains(&bind)),
            "Allowed networks must include the dashboard bind address"
        );
        Ok(Self {
            bind,
            networks,
            hosts: Vec::new(),
        })
    }

    pub fn with_hosts(mut self, hosts: Vec<String>) -> Result<Self> {
        for host in &hosts {
            ensure!(
                host.len() <= 253
                    && host.contains('.')
                    && host.split('.').all(|label| {
                        !label.is_empty()
                            && label.len() <= 63
                            && label.as_bytes()[0].is_ascii_alphanumeric()
                            && label.as_bytes()[label.len() - 1].is_ascii_alphanumeric()
                            && label
                                .bytes()
                                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    }),
                "Allowed hosts must be explicit DNS names without a scheme, port or wildcard"
            );
        }
        self.hosts = hosts
            .into_iter()
            .map(|host| host.to_ascii_lowercase())
            .collect();
        Ok(self)
    }

    pub fn allows_peer(&self, peer: IpAddr) -> bool {
        match peer {
            IpAddr::V4(ip) => {
                ip.is_loopback() || self.networks.iter().any(|network| network.contains(&ip))
            }
            IpAddr::V6(_) => false,
        }
    }

    pub fn allows_host(&self, host: &str) -> bool {
        let (name, port) = host
            .split_once(':')
            .map_or((host, None), |(name, port)| (name, Some(port)));
        (matches!(name, "localhost" | "127.0.0.1")
            || name == self.bind.to_string()
            || self
                .hosts
                .iter()
                .any(|host| name.eq_ignore_ascii_case(host)))
            && port.is_none_or(|port| port.parse::<u16>().is_ok_and(|number| number > 0))
    }
}

impl Default for Access {
    fn default() -> Self {
        Self::new(
            Ipv4Addr::LOCALHOST,
            vec!["127.0.0.0/8".parse().expect("constant network")],
        )
        .expect("constant access policy")
    }
}
impl WebState {
    pub fn new(socket: PathBuf) -> Self {
        let token = rand::random::<[u8; 32]>()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Self {
            socket,
            token,
            access: Access::default(),
        }
    }
}

pub fn router(state: WebState) -> Router {
    Router::new()
        .route("/", get(|| async { Redirect::temporary("/valheim") }))
        .route(
            "/valheim",
            get(|| async {
                (
                    [("content-type", "text/html; charset=utf-8")],
                    include_str!("../web/index.html"),
                )
            }),
        )
        .route(
            "/app.css",
            get(|| async {
                (
                    [("content-type", "text/css; charset=utf-8")],
                    include_str!("../web/app.css"),
                )
            }),
        )
        .route(
            "/app.js",
            get(|| async {
                (
                    [("content-type", "text/javascript; charset=utf-8")],
                    include_str!("../web/app.js"),
                )
            }),
        )
        .route("/api/session", get(session))
        .route("/api/status", get(status))
        .route("/api/worlds", get(worlds))
        .route("/api/action", post(action))
        .layer(DefaultBodyLimit::max(MAX_REQUEST as usize))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            private_headers,
        ))
        .with_state(state)
}

pub fn allowed_host(host: &str) -> bool {
    Access::default().allows_host(host)
}

async fn private_headers(State(state): State<WebState>, request: Request, next: Next) -> Response {
    // Trust the TCP peer, never spoofable Forwarded/X-Forwarded-For headers.
    if !request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .is_some_and(|peer| state.access.allows_peer(peer.0.ip()))
    {
        return (
            StatusCode::FORBIDDEN,
            Json(Reply::failure("Client is outside the allowed LAN networks")),
        )
            .into_response();
    }
    if !request
        .headers()
        .get("host")
        .and_then(|h| h.to_str().ok())
        .is_some_and(|host| state.access.allows_host(host))
    {
        return (
            StatusCode::FORBIDDEN,
            Json(Reply::failure("Unexpected dashboard host")),
        )
            .into_response();
    }
    let mut response = next.run(request).await;
    for (key, value) in [
        ("cache-control", "no-store"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        (
            "content-security-policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        ),
    ] {
        response
            .headers_mut()
            .insert(key, HeaderValue::from_static(value));
    }
    response
}

async fn session(State(state): State<WebState>) -> Json<Value> {
    Json(serde_json::json!({"token": state.token}))
}
async fn status(State(state): State<WebState>) -> Response {
    broker_request(&state.socket, Action::Status)
        .await
        .response()
}

async fn worlds(State(state): State<WebState>) -> Response {
    broker_request(&state.socket, Action::Worlds)
        .await
        .response()
}

async fn action(
    State(state): State<WebState>,
    headers: HeaderMap,
    Json(command): Json<Command>,
) -> Response {
    let host = headers
        .get("host")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let origin = headers
        .get("origin")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let token = headers
        .get("x-valheim-token")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    if origin != format!("http://{host}")
        || token != state.token
        || matches!(command.action, Action::Status | Action::Worlds)
    {
        return (
            StatusCode::FORBIDDEN,
            Json(Reply::failure("Invalid origin or token; reload the page")),
        )
            .into_response();
    }
    if let Err(error) = command.validate() {
        return (
            StatusCode::BAD_REQUEST,
            Json(Reply::failure(&error.to_string())),
        )
            .into_response();
    }
    broker_command(&state.socket, command).await.response()
}

pub async fn broker_request(path: &Path, action: Action) -> Reply {
    broker_command(
        path,
        Command {
            action,
            world: None,
            confirmation: None,
        },
    )
    .await
}

pub async fn broker_command(path: &Path, command: Command) -> Reply {
    let result = timeout(Duration::from_secs(1200), async {
        let mut stream = UnixStream::connect(path).await?;
        stream.write_all(&serde_json::to_vec(&command)?).await?;
        stream.shutdown().await?;
        let mut bytes = Vec::new();
        stream
            .take(MAX_RESPONSE + 1)
            .read_to_end(&mut bytes)
            .await?;
        ensure!(
            bytes.len() as u64 <= MAX_RESPONSE,
            "Oversized helper response"
        );
        Ok::<Reply, anyhow::Error>(serde_json::from_slice(&bytes)?)
    })
    .await;
    match result {
        Ok(Ok(reply)) => reply,
        _ => {
            Reply::failure("Control helper unavailable or timed out; check status before retrying.")
        }
    }
}

async fn handle_broker(
    mut stream: UnixStream,
    config: PathBuf,
    operation: Arc<Mutex<()>>,
) -> Result<()> {
    let mut bytes = Vec::new();
    timeout(
        Duration::from_secs(10),
        (&mut stream).take(MAX_REQUEST + 1).read_to_end(&mut bytes),
    )
    .await??;
    let reply = if bytes.len() as u64 > MAX_REQUEST {
        Reply::failure("Request too large")
    } else if let Ok(command) = serde_json::from_slice::<Command>(&bytes) {
        if let Err(error) = command.validate() {
            stream
                .write_all(&serde_json::to_vec(&Reply::failure(&error.to_string()))?)
                .await?;
            return Ok(());
        }
        if let Ok(_guard) = operation.try_lock() {
            tokio::task::spawn_blocking(move || {
                let result = Config::load(&config).and_then(|config| {
                    let engine = DockerEngine(config.clone());
                    Controller { config, engine }
                        .run(command.action.name(), command.world.as_deref())
                });
                match result {
                    Ok(output) => {
                        let value = if matches!(command.action, Action::Worlds) {
                            match serde_json::from_str::<Vec<String>>(&output) {
                                Ok(worlds) => serde_json::json!(worlds),
                                Err(_) => return Reply::failure("Invalid controller worlds"),
                            }
                        } else if matches!(command.action, Action::Status) {
                            match serde_json::from_str::<Value>(&output) {
                                Ok(mut status) => {
                                    // Only intentionally exposed fields cross the privilege boundary.
                                    if let Some(fields) = status.as_object_mut() {
                                        fields.retain(|key, _| {
                                            matches!(
                                                key.as_str(),
                                                "running"
                                                    | "game_state"
                                                    | "players"
                                                    | "selected_world"
                                                    | "running_world"
                                                    | "idle_enabled"
                                                    | "idle_timeout_seconds"
                                                    | "pre_change_backups"
                                            )
                                        });
                                    }
                                    status
                                }
                                Err(_) => return Reply::failure("Invalid controller status"),
                            }
                        } else {
                            Value::String(output)
                        };
                        Reply::success(value)
                    }
                    Err(error) => {
                        eprintln!("Dashboard {} failed: {error:#}", command.action.name());
                        Reply::failure(
                            "Controller failed; check server logs and status before retrying.",
                        )
                    }
                }
            })
            .await
            .context("Controller worker failed")?
        } else {
            Reply::failure("Another operation is in progress; try again shortly.")
        }
    } else {
        Reply::failure("Unsupported action or invalid request")
    };
    timeout(
        Duration::from_secs(10),
        stream.write_all(&serde_json::to_vec(&reply)?),
    )
    .await??;
    Ok(())
}

pub async fn broker(path: &Path, config: PathBuf) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::remove_file(path) {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
        Err(error) => return Err(error.into()),
    }
    let listener = UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660))?;
    serve_broker(listener, config).await
}

pub async fn serve_broker(listener: UnixListener, config: PathBuf) -> Result<()> {
    let operation = Arc::new(Mutex::new(()));
    loop {
        let (stream, _) = listener.accept().await?;
        let config = config.clone();
        let operation = operation.clone();
        tokio::spawn(async move {
            if let Err(error) = handle_broker(stream, config, operation).await {
                eprintln!("Broker request failed: {error}");
            }
        });
    }
}

pub async fn web(port: u16, socket: PathBuf, access: Access) -> Result<()> {
    let listener = tokio::net::TcpListener::bind((access.bind, port)).await?;
    eprintln!(
        "Valheim dashboard listening on http://{}:{port} (allowed private networks only)",
        access.bind
    );
    let mut state = WebState::new(socket);
    state.access = access;
    axum::serve(
        listener,
        router(state).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await?;
    Ok(())
}
