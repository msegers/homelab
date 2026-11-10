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
    Metrics,
    Status,
    Start,
    Stop,
    Restart,
    Worlds,
    World,
    Create,
    Delete,
}
impl Action {
    fn name(self) -> &'static str {
        match self {
            Self::Metrics => "metrics",
            Self::Status => "status",
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Restart => "restart",
            Self::Worlds => "worlds",
            Self::World => "world",
            Self::Create => "create-world",
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seed: Option<String>,
}

impl Command {
    fn validate(&self) -> Result<()> {
        let needs_world = matches!(self.action, Action::World | Action::Delete | Action::Create);
        ensure!(
            needs_world == self.world.is_some(),
            "Unexpected or missing world name"
        );
        if let Some(world) = &self.world {
            ensure!(crate::controller::valid_world(world), "Invalid world name");
        }
        if let Some(seed) = &self.seed {
            ensure!(
                matches!(self.action, Action::Create),
                "Seeds are only allowed for new worlds"
            );
            ensure!(
                crate::world_seed::valid_seed(seed),
                "Seeds use 1–10 letters or numbers (case-sensitive)"
            );
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

fn controller_failure(error: &anyhow::Error) -> Reply {
    // Only typed, fixed user-facing reasons cross the privilege boundary.
    // Underlying I/O and Docker errors may contain secrets and remain in logs.
    if let Some(reason) = error.downcast_ref::<crate::world_seed::CreationError>() {
        Reply::failure(&reason.to_string())
    } else {
        Reply::failure(
            "Controller failed; check server logs and selected/running state before retrying. No success was confirmed.",
        )
    }
}

#[cfg(test)]
mod failure_tests {
    use super::*;

    #[test]
    fn only_fixed_creation_reasons_are_exposed_not_error_context_or_secrets() {
        let private = "token=private-value /private/config.json";
        let conflict =
            anyhow::Error::new(crate::world_seed::CreationError::NameInUse).context(private);
        let reply = controller_failure(&conflict);
        assert!(!reply.ok);
        let message = reply.error.unwrap();
        assert!(message.contains("still has world saves"));
        assert!(!message.contains("private"));
        let reply = controller_failure(&anyhow::anyhow!(private));
        assert!(!reply.ok);
        let message = reply.error.unwrap();
        assert!(message.contains("No success was confirmed"));
        assert!(!message.contains("private"));
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
    proxies: Vec<Ipv4Net>,
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
            proxies: Vec::new(),
        })
    }

    pub fn with_proxies(mut self, proxies: Vec<Ipv4Net>) -> Result<Self> {
        for proxy in &proxies {
            ensure!(
                proxy.prefix_len() == 32 && proxy.addr().is_private(),
                "Trusted proxies must be exact private IPv4 /32 peers"
            );
        }
        self.proxies = proxies;
        Ok(self)
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
                ip.is_loopback()
                    || self
                        .networks
                        .iter()
                        .chain(&self.proxies)
                        .any(|network| network.contains(&ip))
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
        .route("/", get(|| async { Redirect::temporary("/homelab") }))
        .route(
            "/homelab",
            get(|| async {
                (
                    [("content-type", "text/html; charset=utf-8")],
                    include_str!("../web/homelab.html"),
                )
            }),
        )
        .route(
            "/homelab.js",
            get(|| async {
                (
                    [("content-type", "text/javascript; charset=utf-8")],
                    include_str!("../web/homelab.js"),
                )
            }),
        )
        .route(
            "/homelab.css",
            get(|| async {
                (
                    [("content-type", "text/css; charset=utf-8")],
                    include_str!("../web/homelab.css"),
                )
            }),
        )
        .route("/api/metrics", get(metrics))
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

async fn private_headers(
    State(state): State<WebState>,
    mut request: Request,
    next: Next,
) -> Response {
    let trusted_proxy = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .is_some_and(|peer| match peer.0.ip() {
            IpAddr::V4(ip) => state.access.proxies.iter().any(|proxy| proxy.contains(&ip)),
            IpAddr::V6(_) => false,
        });
    if !trusted_proxy {
        request.headers_mut().remove("x-forwarded-proto");
    }
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

async fn metrics(State(state): State<WebState>) -> Response {
    match timeout(
        Duration::from_secs(20),
        broker_request(&state.socket, Action::Metrics),
    )
    .await
    {
        Ok(reply) => reply.response(),
        Err(_) => Reply::failure("Statistics timed out; try refreshing later.").response(),
    }
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
    let scheme = match headers
        .get("x-forwarded-proto")
        .and_then(|h| h.to_str().ok())
    {
        Some("https") => "https",
        _ => "http",
    };
    if origin != format!("{scheme}://{host}")
        || token != state.token
        || matches!(
            command.action,
            Action::Status | Action::Worlds | Action::Metrics
        )
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
            seed: None,
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

type MetricsCache = Arc<std::sync::Mutex<Option<(std::time::Instant, Value)>>>;

fn cached_metrics(cache: &MetricsCache, collect: impl FnOnce() -> Value) -> Value {
    let mut cache = cache.lock().expect("Metrics cache poisoned");
    if let Some((at, value)) = &*cache
        && at.elapsed() < Duration::from_secs(10)
    {
        return value.clone();
    }
    let value = collect();
    *cache = Some((std::time::Instant::now(), value.clone()));
    value
}

#[cfg(test)]
mod metrics_cache_tests {
    use super::*;

    #[test]
    fn cache_reuses_samples_until_expired() {
        let cache: MetricsCache = Arc::new(std::sync::Mutex::new(None));
        let first = serde_json::json!({"sampled_at": "first"});
        assert_eq!(cached_metrics(&cache, || first.clone()), first);
        assert_eq!(
            cached_metrics(&cache, || panic!("Must reuse sample")),
            first
        );
        cache.lock().unwrap().as_mut().unwrap().0 =
            std::time::Instant::now() - Duration::from_secs(11);
        let second = serde_json::json!({"sampled_at": "second"});
        assert_eq!(cached_metrics(&cache, || second.clone()), second);
    }
}

async fn handle_broker(
    mut stream: UnixStream,
    config: PathBuf,
    operation: Arc<Mutex<()>>,
    metrics_cache: MetricsCache,
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
        if let Ok(guard) = operation.try_lock_owned() {
            tokio::task::spawn_blocking(move || {
                let _guard = guard;
                if matches!(command.action, Action::Metrics) {
                    return Reply::success(cached_metrics(&metrics_cache, || {
                        serde_json::to_value(crate::metrics::collect())
                            .expect("Metrics are serializable")
                    }));
                }
                let result = Config::load(&config).and_then(|config| {
                    let engine = DockerEngine(config.clone());
                    Controller { config, engine }.run_with_seed(
                        command.action.name(),
                        command.world.as_deref(),
                        command.seed.as_deref(),
                    )
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
                        controller_failure(&error)
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
    let metrics_cache: MetricsCache = Arc::new(std::sync::Mutex::new(None));
    loop {
        let (stream, _) = listener.accept().await?;
        let config = config.clone();
        let operation = operation.clone();
        let metrics_cache = metrics_cache.clone();
        tokio::spawn(async move {
            if let Err(error) = handle_broker(stream, config, operation, metrics_cache).await {
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
