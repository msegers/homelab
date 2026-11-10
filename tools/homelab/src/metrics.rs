//! On-demand, read-only host and Compose metrics. Never exposes inspect/env/logs.
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::Value;
use std::{
    fs,
    io::{Read, Seek},
    process::{Command, Stdio},
    time::Duration,
};
use wait_timeout::ChildExt;

#[derive(Debug, Serialize)]
pub struct Host {
    pub specs: crate::specs::Specs,
    pub cpu_percent: f64,
    pub memory_used_bytes: u64,
    pub memory_total_bytes: u64,
    pub uptime_seconds: f64,
    pub load_average: Vec<f64>,
}

#[derive(Debug, Serialize)]
pub struct Service {
    pub name: String,
    pub state: String,
    pub status: String,
    pub cpu: Option<String>,
    pub memory: Option<String>,
    pub network: Option<String>,
    pub block_io: Option<String>,
    pub pids: Option<String>,
}

#[derive(Serialize)]
pub struct Snapshot {
    pub sampled_at: String,
    pub host: Option<Host>,
    pub disks: Vec<crate::disk::Disk>,
    pub services: Vec<Service>,
    pub warnings: Vec<&'static str>,
}

fn cpu(text: &str) -> Result<(u64, u64)> {
    let fields = text.lines().next().context("Missing CPU counters")?;
    let values = fields
        .strip_prefix("cpu ")
        .context("Invalid CPU counters")?
        .split_whitespace()
        .take(8)
        .map(str::parse::<u64>)
        .collect::<std::result::Result<Vec<_>, _>>()?;
    ensure!(values.len() >= 4, "Incomplete CPU counters");
    Ok((
        values.iter().sum(),
        values[3] + values.get(4).copied().unwrap_or(0),
    ))
}

fn host(
    before: (u64, u64),
    after: (u64, u64),
    memory: &str,
    uptime: &str,
    load: &str,
) -> Result<Host> {
    let kib = |key: &str| -> Result<u64> {
        let value = memory
            .lines()
            .find_map(|line| line.strip_prefix(key))
            .context("Missing memory counter")?
            .split_whitespace()
            .next()
            .context("Empty memory counter")?
            .parse::<u64>()?;
        value.checked_mul(1024).context("Invalid memory counter")
    };
    let total = kib("MemTotal:")?;
    let available = kib("MemAvailable:")?;
    let elapsed = after.0.saturating_sub(before.0);
    ensure!(
        elapsed > 0 && total > 0 && available <= total,
        "Invalid host counters"
    );
    let idle = after.1.saturating_sub(before.1).min(elapsed);
    Ok(Host {
        specs: Default::default(),
        cpu_percent: 100.0 * (elapsed - idle) as f64 / elapsed as f64,
        memory_used_bytes: total - available,
        memory_total_bytes: total,
        uptime_seconds: uptime
            .split_whitespace()
            .next()
            .context("Missing uptime")?
            .parse()?,
        load_average: load
            .split_whitespace()
            .take(3)
            .map(str::parse)
            .collect::<std::result::Result<_, _>>()?,
    })
}

fn collect_host() -> Result<Host> {
    let before = cpu(&fs::read_to_string("/proc/stat")?)?;
    std::thread::sleep(Duration::from_millis(250));
    let mut host = host(
        before,
        cpu(&fs::read_to_string("/proc/stat")?)?,
        &fs::read_to_string("/proc/meminfo")?,
        &fs::read_to_string("/proc/uptime")?,
        &fs::read_to_string("/proc/loadavg")?,
    )?;
    host.specs = crate::specs::collect();
    Ok(host)
}

fn docker(args: &[&str]) -> Result<String> {
    let mut output = tempfile::tempfile()?;
    let mut child = Command::new("/usr/bin/docker")
        .args(["--context", "default"])
        .args(args)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("HOME", "/root")
        .env("LANG", "C.UTF-8")
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(Stdio::null())
        .spawn()?;
    let Some(status) = child.wait_timeout(Duration::from_secs(8))? else {
        let _ = child.kill();
        let _ = child.wait();
        anyhow::bail!("Metrics command timed out");
    };
    ensure!(status.success(), "Metrics command failed");
    ensure!(
        output.metadata()?.len() <= 512 * 1024,
        "Metrics response too large"
    );
    output.rewind()?;
    let mut text = String::new();
    output.read_to_string(&mut text)?;
    Ok(text)
}

fn field(value: &Value, name: &str) -> Result<String> {
    let text = value[name].as_str().context("Missing metrics field")?;
    ensure!(text.len() <= 256, "Metrics field too large");
    Ok(text.to_owned())
}

fn services(text: &str) -> Result<Vec<Service>> {
    let mut result = Vec::new();
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        ensure!(result.len() < 64, "Too many Compose containers");
        let value: Value = serde_json::from_str(line)?;
        result.push(Service {
            name: field(&value, "Names")?,
            state: field(&value, "State")?,
            status: field(&value, "Status")?,
            cpu: None,
            memory: None,
            network: None,
            block_io: None,
            pids: None,
        });
    }
    result.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(result)
}

fn apply_stats(services: &mut [Service], text: &str) -> Result<()> {
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let value: Value = serde_json::from_str(line)?;
        let name = field(&value, "Name")?;
        if let Some(service) = services
            .iter_mut()
            .find(|s| s.name == name && s.state == "running")
        {
            service.cpu = Some(field(&value, "CPUPerc")?);
            service.memory = Some(field(&value, "MemUsage")?);
            service.network = Some(field(&value, "NetIO")?);
            service.block_io = Some(field(&value, "BlockIO")?);
            service.pids = Some(field(&value, "PIDs")?);
        }
    }
    ensure!(
        services
            .iter()
            .filter(|s| s.state == "running")
            .all(|s| s.cpu.is_some()),
        "Running container statistics missing"
    );
    Ok(())
}

pub fn collect() -> Snapshot {
    collect_with(collect_host, crate::disk::collect, docker)
}

fn collect_with(
    host: impl FnOnce() -> Result<Host> + Send,
    disks: impl FnOnce() -> Vec<crate::disk::Disk> + Send,
    docker: impl FnMut(&[&str]) -> Result<String>,
) -> Snapshot {
    // Called from the broker's blocking worker, never a Tokio runtime thread.
    // Only two short-lived threads per cache miss; Docker ps/stats stay sequential
    // because stats must use the explicitly discovered running container names.
    std::thread::scope(|scope| {
        let host_worker = scope.spawn(host);
        let disk_worker = scope.spawn(disks);
        let mut snapshot = collect_docker(docker);
        snapshot.host = host_worker
            .join()
            .unwrap_or_else(|_| Err(anyhow::anyhow!("Host metrics worker failed")))
            .map_err(|_| snapshot.warnings.push("Host statistics unavailable."))
            .ok();
        match disk_worker.join() {
            Ok(disks) => {
                if disks.iter().any(|disk| disk.status != "Mounted") {
                    snapshot
                        .warnings
                        .push("Some storage filesystems are unmounted or unavailable.");
                }
                snapshot.disks = disks;
            }
            Err(_) => snapshot.warnings.push("Storage statistics unavailable."),
        }
        snapshot.sampled_at = chrono::Utc::now().to_rfc3339();
        snapshot
    })
}

fn collect_docker(mut docker: impl FnMut(&[&str]) -> Result<String>) -> Snapshot {
    let mut warnings = Vec::new();
    let mut services = docker(&[
        "ps",
        "--all",
        "--filter",
        "label=com.docker.compose.project",
        "--format",
        "{{json .}}",
    ])
    .and_then(|text| services(&text))
    .unwrap_or_else(|_| {
        warnings.push("Docker service list unavailable.");
        Vec::new()
    });
    let names: Vec<_> = services
        .iter()
        .filter(|s| s.state == "running")
        .map(|s| s.name.clone())
        .collect();
    if !names.is_empty() {
        let mut args = vec!["stats", "--no-stream", "--format", "{{json .}}", "--"];
        args.extend(names.iter().map(String::as_str));
        if docker(&args)
            .and_then(|text| apply_stats(&mut services, &text))
            .is_err()
        {
            for service in &mut services {
                service.cpu = None;
                service.memory = None;
                service.network = None;
                service.block_io = None;
                service.pids = None;
            }
            warnings.push("Container statistics unavailable; service states may still be shown.");
        }
    }
    Snapshot {
        sampled_at: chrono::Utc::now().to_rfc3339(),
        host: None,
        disks: Vec::new(),
        services,
        warnings,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_host() -> Result<Host> {
        host(
            (100, 70),
            (200, 110),
            "MemTotal: 1000 kB\nMemAvailable: 400 kB",
            "123.5 42",
            "0.1 0.2 0.3",
        )
    }

    fn fixture_disks() -> Vec<crate::disk::Disk> {
        vec![crate::disk::Disk {
            name: "System / SSD",
            mount: "/",
            device: Some("/dev/fixture".into()),
            total_bytes: Some(100),
            used_bytes: Some(40),
            available_bytes: Some(60),
            used_percent: Some(40.0),
            status: "Mounted",
        }]
    }

    #[test]
    fn host_disk_and_docker_collection_overlap_without_timing_benchmarks() {
        use std::sync::mpsc;
        let (started, ready) = mpsc::channel();
        let host_started = started.clone();
        let (release_host, wait_host) = mpsc::channel();
        let (release_disk, wait_disk) = mpsc::channel();
        let snapshot = collect_with(
            move || {
                host_started.send("host").unwrap();
                wait_host.recv_timeout(Duration::from_secs(5))?;
                fixture_host()
            },
            move || {
                started.send("disk").unwrap();
                wait_disk.recv_timeout(Duration::from_secs(5)).unwrap();
                fixture_disks()
            },
            |args| {
                assert_eq!(args[0], "ps");
                let mut workers = [
                    ready.recv_timeout(Duration::from_secs(5)).unwrap(),
                    ready.recv_timeout(Duration::from_secs(5)).unwrap(),
                ];
                workers.sort();
                assert_eq!(workers, ["disk", "host"]);
                release_host.send(()).unwrap();
                release_disk.send(()).unwrap();
                Ok(String::new())
            },
        );
        assert!(snapshot.host.is_some());
        assert_eq!(snapshot.disks.len(), 1);
        assert!(snapshot.warnings.is_empty());
    }

    #[test]
    fn concurrent_collection_preserves_partial_results_and_fixed_failure_messages() {
        let snapshot = collect_with(fixture_host, fixture_disks, |_| {
            anyhow::bail!("private docker error")
        });
        assert!(snapshot.host.is_some());
        assert_eq!(snapshot.disks.len(), 1);
        assert_eq!(snapshot.warnings, ["Docker service list unavailable."]);
        assert!(
            !serde_json::to_string(&snapshot)
                .unwrap()
                .contains("private")
        );

        let snapshot = collect_with(
            || panic!("private host panic"),
            || panic!("private disk panic"),
            |_| Ok(String::new()),
        );
        assert!(snapshot.host.is_none());
        assert!(snapshot.disks.is_empty());
        assert_eq!(
            snapshot.warnings,
            [
                "Host statistics unavailable.",
                "Storage statistics unavailable."
            ]
        );
        assert!(
            !serde_json::to_string(&snapshot)
                .unwrap()
                .contains("private")
        );
    }

    #[test]
    fn collection_limits_stats_to_discovered_running_compose_containers() {
        let mut calls = Vec::new();
        let snapshot = collect_with(
            || Err(anyhow::anyhow!("private host error")),
            Vec::new,
            |args| {
                calls.push(args.iter().map(|s| s.to_string()).collect::<Vec<_>>());
                match args[0] {
                "ps" => Ok(concat!(
                    "{\"Names\":\"pihole\",\"State\":\"running\",\"Status\":\"Up (healthy)\"}\n",
                    "{\"Names\":\"valheim\",\"State\":\"exited\",\"Status\":\"Exited\"}"
                )
                .into()),
                "stats" => anyhow::bail!("private docker error"),
                _ => panic!("Unexpected Docker operation"),
            }
            },
        );
        assert_eq!(
            calls[0],
            [
                "ps",
                "--all",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                "{{json .}}"
            ]
        );
        assert_eq!(
            calls[1],
            [
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                "--",
                "pihole"
            ]
        );
        assert!(snapshot.host.is_none());
        assert_eq!(snapshot.services.len(), 2);
        assert!(snapshot.services.iter().all(|s| s.cpu.is_none()));
        assert_eq!(snapshot.warnings.len(), 2);
        assert!(
            !serde_json::to_string(&snapshot)
                .unwrap()
                .contains("private")
        );
    }

    #[test]
    fn empty_or_unavailable_docker_never_starts_unscoped_stats() {
        for unavailable in [false, true] {
            let mut count = 0;
            let snapshot = collect_with(
                || Err(anyhow::anyhow!("fixture")),
                Vec::new,
                |args| {
                    count += 1;
                    assert_eq!(args[0], "ps");
                    if unavailable {
                        anyhow::bail!("fixture");
                    }
                    Ok(String::new())
                },
            );
            assert_eq!(count, 1);
            assert!(snapshot.services.is_empty());
            assert_eq!(snapshot.warnings.len(), if unavailable { 2 } else { 1 });
        }
    }

    #[test]
    fn host_uses_available_memory_and_excludes_guest_cpu_counters() {
        assert_eq!(
            cpu("cpu 10 0 20 60 10 0 0 0 50 50\ncpu0 1").unwrap(),
            (100, 70)
        );
        let host = host(
            (100, 70),
            (200, 110),
            "MemTotal: 1000 kB\nMemAvailable: 400 kB",
            "123.5 42",
            "0.1 0.2 0.3 1/2",
        )
        .unwrap();
        assert_eq!(host.cpu_percent, 60.0);
        assert_eq!(host.memory_used_bytes, 600 * 1024);
        assert_eq!(host.uptime_seconds, 123.5);
        assert!(cpu("cpu 1").is_err());
        assert!(super::host((1, 1), (1, 1), "", "", "").is_err());
    }

    #[test]
    fn only_explicit_metrics_cross_boundary_and_stopped_stats_remain_unknown() {
        let mut rows = services(
            r#"{"Names":"pihole","State":"running","Status":"Up (healthy)","Env":"secret"}
{"Names":"valheim","State":"exited","Status":"Exited"}"#,
        )
        .unwrap();
        apply_stats(&mut rows, r#"{"Name":"pihole","CPUPerc":"2.0%","MemUsage":"10MiB / 1GiB","NetIO":"1kB / 2kB","BlockIO":"0B / 0B","PIDs":"3","Secret":"hidden"}"#).unwrap();
        assert_eq!(rows[0].cpu.as_deref(), Some("2.0%"));
        assert!(rows[1].cpu.is_none());
        let json = serde_json::to_string(&rows).unwrap();
        assert!(!json.contains("secret") && !json.contains("hidden"));
        assert!(services("invalid").is_err());
        assert!(
            services(&"{\"Names\":\"x\",\"State\":\"running\",\"Status\":\"up\"}\n".repeat(65))
                .is_err()
        );
    }
}
