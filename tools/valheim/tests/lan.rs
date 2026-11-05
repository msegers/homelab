use axum::{
    body::Body,
    extract::ConnectInfo,
    http::{Request, StatusCode},
};
use homelab_valheim::dashboard::{Access, WebState, router};
use std::net::SocketAddr;
use tower::ServiceExt;

fn lan() -> Access {
    Access::new(
        "192.168.68.111".parse().unwrap(),
        vec!["192.168.68.0/24".parse().unwrap()],
    )
    .unwrap()
}

#[test]
fn rejects_public_wildcard_and_overbroad_configuration() {
    for bind in ["0.0.0.0", "8.8.8.8", "169.254.1.2", "224.0.0.1"] {
        assert!(
            Access::new(
                bind.parse().unwrap(),
                vec!["192.168.68.0/24".parse().unwrap()]
            )
            .is_err()
        );
    }
    for network in [
        "0.0.0.0/0",
        "8.8.8.0/24",
        "192.168.0.0/15",
        "10.0.0.0/7",
        "172.0.0.0/11",
    ] {
        assert!(
            Access::new(
                "192.168.68.111".parse().unwrap(),
                vec![network.parse().unwrap()]
            )
            .is_err()
        );
    }
    assert!(Access::new("192.168.68.111".parse().unwrap(), vec![]).is_err());
    assert!(
        Access::new(
            "192.168.68.111".parse().unwrap(),
            vec!["10.0.0.0/8".parse().unwrap()]
        )
        .is_err()
    );
}

#[tokio::test]
async fn lan_clients_can_open_dashboard_but_internet_other_subnets_and_spoofed_headers_cannot() {
    let mut state = WebState::new("/unused.sock".into());
    state.access = lan();
    for (peer, expected) in [
        ("192.168.68.42:1234", StatusCode::OK),
        ("127.0.0.1:1234", StatusCode::OK),
        ("8.8.8.8:1234", StatusCode::FORBIDDEN),
        ("192.168.69.42:1234", StatusCode::FORBIDDEN),
        ("10.1.1.1:1234", StatusCode::FORBIDDEN),
    ] {
        for path in ["/", "/app.js", "/api/session"] {
            let request = Request::builder()
                .uri(path)
                .header("host", "192.168.68.111:8787")
                .header("x-forwarded-for", "192.168.68.42")
                .header("forwarded", "for=192.168.68.42")
                .extension(ConnectInfo(peer.parse::<SocketAddr>().unwrap()))
                .body(Body::empty())
                .unwrap();
            assert_eq!(
                router(state.clone())
                    .oneshot(request)
                    .await
                    .unwrap()
                    .status(),
                expected,
                "{peer} {path}"
            );
        }
    }
}

#[tokio::test]
async fn lan_host_does_not_relax_origin_token_or_dns_rebinding_checks() {
    let mut state = WebState::new("/unused.sock".into());
    state.access = lan();
    for host in [
        "evil.example",
        "192.168.68.112:8787",
        "192.168.68.111.evil.example",
    ] {
        let request = Request::builder()
            .uri("/")
            .header("host", host)
            .extension(ConnectInfo(
                "192.168.68.42:1234".parse::<SocketAddr>().unwrap(),
            ))
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            router(state.clone())
                .oneshot(request)
                .await
                .unwrap()
                .status(),
            StatusCode::FORBIDDEN
        );
    }
    let request = Request::builder()
        .method("POST")
        .uri("/api/action")
        .header("host", "192.168.68.111:8787")
        .header("origin", "http://evil.example")
        .header("x-valheim-token", &state.token)
        .header("content-type", "application/json")
        .extension(ConnectInfo(
            "192.168.68.42:1234".parse::<SocketAddr>().unwrap(),
        ))
        .body(Body::from(r#"{"action":"start"}"#))
        .unwrap();
    assert_eq!(
        router(state).oneshot(request).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn missing_connection_metadata_fails_closed() {
    let request = Request::builder()
        .uri("/")
        .header("host", "localhost:8787")
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        router(WebState::new("/unused.sock".into()))
            .oneshot(request)
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
}

#[tokio::test]
async fn valid_lan_origin_and_token_reach_existing_unix_broker() {
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::UnixListener,
    };
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let helper = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut bytes = Vec::new();
        stream.read_to_end(&mut bytes).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&bytes).unwrap(),
            serde_json::json!({"action":"start"})
        );
        stream
            .write_all(br#"{"ok":true,"result":"Started"}"#)
            .await
            .unwrap();
    });
    let mut state = WebState::new(path);
    state.access = lan();
    let request = Request::builder()
        .method("POST")
        .uri("/api/action")
        .header("host", "192.168.68.111:8787")
        .header("origin", "http://192.168.68.111:8787")
        .header("x-valheim-token", &state.token)
        .header("content-type", "application/json")
        .extension(ConnectInfo(
            "192.168.68.42:1234".parse::<SocketAddr>().unwrap(),
        ))
        .body(Body::from(r#"{"action":"start"}"#))
        .unwrap();
    assert_eq!(
        router(state).oneshot(request).await.unwrap().status(),
        StatusCode::OK
    );
    helper.await.unwrap();
}

#[tokio::test]
async fn public_peer_cannot_mutate_even_with_valid_origin_and_token() {
    let mut state = WebState::new("/unused.sock".into());
    state.access = lan();
    let request = Request::builder()
        .method("POST")
        .uri("/api/action")
        .header("host", "192.168.68.111:8787")
        .header("origin", "http://192.168.68.111:8787")
        .header("x-valheim-token", &state.token)
        .header("content-type", "application/json")
        .header("x-forwarded-for", "192.168.68.42")
        .extension(ConnectInfo("8.8.8.8:1234".parse::<SocketAddr>().unwrap()))
        .body(Body::from(r#"{"action":"start"}"#))
        .unwrap();
    assert_eq!(
        router(state).oneshot(request).await.unwrap().status(),
        StatusCode::FORBIDDEN
    );
}
