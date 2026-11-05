use axum::{
    body::{Body, to_bytes},
    extract::ConnectInfo,
    http::{Request, StatusCode},
};
use homelab_valheim::dashboard::{
    Action, Command, WebState, allowed_host, broker_request, router, serve_broker,
};
use serde_json::{Value, json};
use std::path::PathBuf;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream, UnixListener},
};
use tower::ServiceExt;

fn state() -> WebState {
    WebState {
        socket: PathBuf::from("/nonexistent/test.sock"),
        token: "test-token".into(),
        access: Default::default(),
    }
}
fn request(method: &str, path: &str) -> axum::http::request::Builder {
    Request::builder()
        .method(method)
        .uri(path)
        .header("host", "localhost:8787")
        .extension(ConnectInfo(
            "127.0.0.1:12345".parse::<std::net::SocketAddr>().unwrap(),
        ))
}

#[tokio::test]
async fn separate_static_assets_and_security_headers_are_served() {
    for (path, content_type) in [
        ("/valheim", "text/html"),
        ("/app.css", "text/css"),
        ("/app.js", "text/javascript"),
    ] {
        let response = router(state())
            .oneshot(request("GET", path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert!(
            response.headers()["content-type"]
                .to_str()
                .unwrap()
                .starts_with(content_type)
        );
        assert_eq!(response.headers()["cache-control"], "no-store");
        assert!(
            response.headers()["content-security-policy"]
                .to_str()
                .unwrap()
                .contains("frame-ancestors 'none'")
        );
        let bytes = to_bytes(response.into_body(), 65536).await.unwrap();
        if path == "/valheim" {
            let html = std::str::from_utf8(&bytes).unwrap();
            assert!(html.contains("src=\"/app.js\""));
            assert!(!html.contains("<style>"));
            assert!(html.contains("data-action=\"start\""));
        }
    }
}

#[tokio::test]
async fn dns_rebinding_and_public_hosts_are_rejected() {
    for host in [
        "evil.example",
        "192.168.68.111:8787",
        "localhost.evil.example",
        "localhost@evil.example",
        "127.0.0.1:0",
        "localhost:65536",
        "localhost:8787/path",
    ] {
        assert!(!allowed_host(host));
        let response = router(state())
            .oneshot(
                Request::builder()
                    .uri("/")
                    .header("host", host)
                    .extension(ConnectInfo(
                        "127.0.0.1:12345".parse::<std::net::SocketAddr>().unwrap(),
                    ))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN, "{host}");
    }
    for host in ["localhost", "localhost:8787", "127.0.0.1:9999"] {
        assert!(allowed_host(host));
    }
}

#[tokio::test]
async fn missing_host_is_rejected() {
    let response = router(state())
        .oneshot(
            Request::builder()
                .uri("/")
                .extension(ConnectInfo(
                    "127.0.0.1:12345".parse::<std::net::SocketAddr>().unwrap(),
                ))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn https_origin_requires_an_exact_trusted_proxy_peer() {
    use homelab_valheim::dashboard::Access;
    let access = Access::new(
        "192.168.68.111".parse().unwrap(),
        vec!["192.168.68.0/24".parse().unwrap()],
    )
    .unwrap()
    .with_hosts(vec!["lab.mathijs.se".into()])
    .unwrap();
    assert!(
        access
            .clone()
            .with_proxies(vec!["172.18.0.0/16".parse().unwrap()])
            .is_err()
    );
    assert!(
        access
            .clone()
            .with_proxies(vec!["8.8.8.8/32".parse().unwrap()])
            .is_err()
    );
    let access = access
        .with_proxies(vec!["172.18.0.2/32".parse().unwrap()])
        .unwrap();
    for (peer, scheme, origin, expected) in [
        (
            "172.18.0.2",
            "https",
            "https://lab.mathijs.se",
            StatusCode::SERVICE_UNAVAILABLE,
        ),
        (
            "172.18.0.2",
            "https",
            "http://lab.mathijs.se",
            StatusCode::FORBIDDEN,
        ),
        (
            "172.18.0.2",
            "https,http",
            "https://lab.mathijs.se",
            StatusCode::FORBIDDEN,
        ),
        (
            "192.168.68.42",
            "https",
            "https://lab.mathijs.se",
            StatusCode::FORBIDDEN,
        ),
        (
            "192.168.68.42",
            "https",
            "http://lab.mathijs.se",
            StatusCode::SERVICE_UNAVAILABLE,
        ),
        (
            "172.18.0.3",
            "https",
            "https://lab.mathijs.se",
            StatusCode::FORBIDDEN,
        ),
    ] {
        let response = router(WebState {
            access: access.clone(),
            ..state()
        })
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/action")
                .header("host", "lab.mathijs.se")
                .header("origin", origin)
                .header("x-forwarded-proto", scheme)
                .header("x-valheim-token", "test-token")
                .header("content-type", "application/json")
                .extension(ConnectInfo(
                    format!("{peer}:12345")
                        .parse::<std::net::SocketAddr>()
                        .unwrap(),
                ))
                .body(Body::from(r#"{"action":"start"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
        assert_eq!(response.status(), expected, "{peer} {scheme} {origin}");
    }
}

#[tokio::test]
async fn mutations_require_origin_token_and_allowlisted_action() {
    for (origin, token) in [
        ("http://evil.example", "test-token"),
        ("http://localhost:8787", "wrong"),
        ("", ""),
        ("null", "test-token"),
    ] {
        let response = router(state())
            .oneshot(
                request("POST", "/api/action")
                    .header("origin", origin)
                    .header("x-valheim-token", token)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"action":"start"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
    }
    for body in [
        r#"{"action":"backup"}"#,
        r#"{"action":"start","args":"; id"}"#,
        r#"{"action":[]}"#,
        "invalid json",
    ] {
        let response = router(state())
            .oneshot(
                request("POST", "/api/action")
                    .header("origin", "http://localhost:8787")
                    .header("x-valheim-token", "test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(response.status().is_client_error());
    }
    let response = router(state())
        .oneshot(request("GET", "/api/action").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn session_and_helper_failure_are_clear() {
    let response = router(state())
        .oneshot(request("GET", "/api/session").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let body = to_bytes(response.into_body(), 4096).await.unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&body).unwrap()["token"],
        "test-token"
    );
    let response = router(state())
        .oneshot(request("GET", "/api/status").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn authenticated_actions_reach_only_unix_broker() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("test.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let helper = tokio::spawn(async move {
        for action in ["status", "start", "stop", "restart"] {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut bytes = Vec::new();
            stream.read_to_end(&mut bytes).await.unwrap();
            let command: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(command, json!({"action":action}));
            let result = if action == "status" {
                json!({"running":false,"selected_world":"Home","players":"unknown"})
            } else {
                json!(format!("{action} requested"))
            };
            stream
                .write_all(&serde_json::to_vec(&json!({"ok":true,"result":result})).unwrap())
                .await
                .unwrap();
        }
    });
    let state = WebState {
        socket: path,
        ..state()
    };
    for action in ["status", "start", "stop", "restart"] {
        let request = if action == "status" {
            request("GET", "/api/status").body(Body::empty()).unwrap()
        } else {
            request("POST", "/api/action")
                .header("origin", "http://localhost:8787")
                .header("x-valheim-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from(json!({"action":action}).to_string()))
                .unwrap()
        };
        let response = router(state.clone()).oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 4096).await.unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&bytes).unwrap()["ok"], true);
    }
    helper.await.unwrap();
}

#[tokio::test]
async fn broker_itself_rejects_unknown_actions_without_loading_config() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("test.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let helper = tokio::spawn(serve_broker(
        listener,
        directory.path().join("missing.json"),
    ));
    for body in [
        r#"{"action":"update"}"#,
        r#"{"action":"world","world":"Other","command":"sh"}"#,
        r#"{"action":"start","args":["anything"]}"#,
    ] {
        let mut stream = tokio::net::UnixStream::connect(&path).await.unwrap();
        stream.write_all(body.as_bytes()).await.unwrap();
        stream.shutdown().await.unwrap();
        let mut response = Vec::new();
        stream.read_to_end(&mut response).await.unwrap();
        let reply: Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(reply["ok"], false);
        assert!(
            reply["error"]
                .as_str()
                .unwrap()
                .contains("Unsupported action")
        );
    }
    assert!(!broker_request(&path, Action::Status).await.ok); // missing config fails, never contacts Docker
    helper.abort();
}

#[tokio::test]
async fn real_http_socket_serves_dashboard_on_loopback() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    assert!(address.ip().is_loopback());
    let task = tokio::spawn(async move {
        axum::serve(
            listener,
            router(state()).into_make_service_with_connect_info::<std::net::SocketAddr>(),
        )
        .await
        .unwrap();
    });
    let mut client = TcpStream::connect(address).await.unwrap();
    client
        .write_all(
            format!(
                "GET /valheim HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
                address.port()
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    let mut response = String::new();
    client.read_to_string(&mut response).await.unwrap();
    assert!(response.starts_with("HTTP/1.1 200"));
    assert!(response.contains("Start server"));
    task.abort();
}

#[test]
fn broker_protocol_cannot_encode_arbitrary_commands() {
    assert!(serde_json::from_str::<Command>(r#"{"action":"sh"}"#).is_err());
    assert!(serde_json::from_str::<Command>(r#"{"action":"start","command":"sh"}"#).is_err());
}

#[tokio::test]
async fn root_redirects_to_valheim_page() {
    let response = router(state())
        .oneshot(request("GET", "/").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
    assert_eq!(response.headers()["location"], "/valheim");
}

#[tokio::test]
async fn world_requests_preserve_only_typed_fields_across_the_socket() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("worlds.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let commands = vec![
        json!({"action":"worlds"}),
        json!({"action":"world","world":"NewWorld"}),
        json!({"action":"delete","world":"Other","confirmation":"Other"}),
    ];
    let expected = commands.clone();
    let helper = tokio::spawn(async move {
        for command in expected {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut bytes = Vec::new();
            stream.read_to_end(&mut bytes).await.unwrap();
            assert_eq!(serde_json::from_slice::<Value>(&bytes).unwrap(), command);
            stream
                .write_all(br#"{"ok":true,"result":["Home","Other"]}"#)
                .await
                .unwrap();
        }
    });
    let state = WebState {
        socket: path,
        ..state()
    };
    for command in commands {
        let req = if command["action"] == "worlds" {
            request("GET", "/api/worlds").body(Body::empty()).unwrap()
        } else {
            request("POST", "/api/action")
                .header("origin", "http://localhost:8787")
                .header("x-valheim-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from(command.to_string()))
                .unwrap()
        };
        assert_eq!(
            router(state.clone()).oneshot(req).await.unwrap().status(),
            StatusCode::OK
        );
    }
    helper.await.unwrap();
}

#[tokio::test]
async fn world_payloads_are_validated_before_contacting_the_broker() {
    for body in [
        json!({"action": "world"}),
        json!({"action": "world", "world": "../escape"}),
        json!({"action": "start", "world": "Home"}),
        json!({"action": "delete", "world": "Other"}),
        json!({"action": "delete", "world": "Other", "confirmation": "Wrong"}),
        json!({"action": "world", "world": "Other", "confirmation": "Other"}),
    ] {
        let response = router(state())
            .oneshot(
                request("POST", "/api/action")
                    .header("origin", "http://localhost:8787")
                    .header("x-valheim-token", "test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST, "{body}");
    }
}

#[test]
fn explicitly_allowed_dns_hosts_do_not_allow_other_names() {
    use homelab_valheim::dashboard::Access;
    let access = Access::default()
        .with_hosts(vec!["lab.mathijs.se".into()])
        .unwrap();
    assert!(access.allows_host("lab.mathijs.se:8787"));
    assert!(access.allows_host("LAB.mathijs.se"));
    for host in [
        "mathijs.se",
        "lan.mathijs.se",
        "lab.mathijs.se.evil.test",
        "lab.mathijs.se@evil.test",
        "lab.mathijs.se:0",
    ] {
        assert!(!access.allows_host(host));
    }
    for host in [
        "*.mathijs.se",
        "http://lab.mathijs.se",
        "lab.mathijs.se:8787",
        "bad name.test",
        "-flag.test",
    ] {
        assert!(Access::default().with_hosts(vec![host.into()]).is_err());
    }
}
