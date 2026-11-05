//! Loopback HTTP UI talks to a private Unix socket, never to Docker or sudo.
use crate::{
    controller::{Config, Controller},
    engine::DockerEngine,
};
use anyhow::{Context, Result, ensure};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
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
}
impl Action {
    fn name(self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Restart => "restart",
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Command {
    pub action: Action,
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
}
impl WebState {
    pub fn new(socket: PathBuf) -> Self {
        let token = rand::random::<[u8; 32]>()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Self { socket, token }
    }
}

pub fn router(state: WebState) -> Router {
    Router::new()
        .route(
            "/",
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
        .route("/api/action", post(action))
        .layer(DefaultBodyLimit::max(MAX_REQUEST as usize))
        .layer(middleware::from_fn(private_headers))
        .with_state(state)
}

pub fn allowed_host(host: &str) -> bool {
    let (name, port) = host
        .split_once(':')
        .map_or((host, None), |(name, port)| (name, Some(port)));
    matches!(name, "localhost" | "127.0.0.1")
        && port.is_none_or(|port| port.parse::<u16>().is_ok_and(|number| number > 0))
}

async fn private_headers(request: Request, next: Next) -> Response {
    if !request
        .headers()
        .get("host")
        .and_then(|h| h.to_str().ok())
        .is_some_and(allowed_host)
    {
        return (
            StatusCode::FORBIDDEN,
            Json(Reply::failure("Loopback access only")),
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
        || matches!(command.action, Action::Status)
    {
        return (
            StatusCode::FORBIDDEN,
            Json(Reply::failure("Invalid origin or token; reload the page")),
        )
            .into_response();
    }
    broker_request(&state.socket, command.action)
        .await
        .response()
}

pub async fn broker_request(path: &Path, action: Action) -> Reply {
    let result = timeout(Duration::from_secs(1200), async {
        let mut stream = UnixStream::connect(path).await?;
        stream
            .write_all(&serde_json::to_vec(&Command { action })?)
            .await?;
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
        if let Ok(_guard) = operation.try_lock() {
            tokio::task::spawn_blocking(move || {
                let result = Config::load(&config).and_then(|config| {
                    let engine = DockerEngine(config.clone());
                    Controller { config, engine }.run(command.action.name(), None)
                });
                match result {
                    Ok(output) => {
                        let value = if matches!(command.action, Action::Status) {
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

pub async fn web(port: u16, socket: PathBuf) -> Result<()> {
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
    eprintln!("Valheim dashboard listening on http://127.0.0.1:{port} (SSH tunnel only)");
    axum::serve(listener, router(WebState::new(socket))).await?;
    Ok(())
}
