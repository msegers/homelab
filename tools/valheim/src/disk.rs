//! Capacity of the two repository-owned storage locations, without walking files.
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use std::{
    fs,
    os::{fd::AsRawFd, unix::fs::MetadataExt},
};

#[derive(Debug, Serialize)]
pub struct Disk {
    pub name: &'static str,
    pub mount: &'static str,
    pub device: Option<String>,
    pub total_bytes: Option<u64>,
    pub used_bytes: Option<u64>,
    pub available_bytes: Option<u64>,
    pub used_percent: Option<f64>,
    pub status: &'static str,
}

struct Mount {
    device_number: String,
    source: String,
}

// mountinfo escapes spaces, tabs, newlines and backslashes as octal sequences.
fn unescape(text: &str) -> String {
    text.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
}

fn find_mount(text: &str, target: &str) -> Result<Option<Mount>> {
    let mut found = None;
    for line in text.lines() {
        let (left, right) = line.split_once(" - ").context("Invalid mountinfo")?;
        let fields: Vec<_> = left.split_whitespace().collect();
        ensure!(fields.len() >= 6, "Incomplete mountinfo");
        if unescape(fields[4]) == target {
            let source = right
                .split_whitespace()
                .nth(1)
                .context("Missing mount source")?;
            // Later entries cover earlier mounts of the same path.
            found = Some(Mount {
                device_number: fields[2].into(),
                source: unescape(source),
            });
        }
    }
    Ok(found)
}

fn capacity(blocks: u64, free: u64, available: u64, size: u64) -> Result<(u64, u64, u64, f64)> {
    ensure!(
        blocks > 0 && size > 0 && available <= free && free <= blocks,
        "Invalid filesystem counters"
    );
    let total = blocks.checked_mul(size).context("Capacity overflow")?;
    let used = (blocks - free)
        .checked_mul(size)
        .context("Capacity overflow")?;
    let available = available.checked_mul(size).context("Capacity overflow")?;
    // Like df: reserved filesystem blocks are not counted as available space.
    let percent = if used + available == 0 {
        0.0
    } else {
        100.0 * used as f64 / (used + available) as f64
    };
    Ok((total, used, available, percent))
}

fn read_capacity(path: &str, mount: &Mount) -> Result<(u64, u64, u64, f64)> {
    // Hold the filesystem open so an unmount cannot switch this sample to the SSD.
    let directory = fs::File::open(path)?;
    let metadata = directory.metadata()?;
    let actual = format!(
        "{}:{}",
        libc::major(metadata.dev()),
        libc::minor(metadata.dev())
    );
    ensure!(
        actual == mount.device_number,
        "Mount changed while sampling"
    );
    let mut counters = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: directory owns a valid fd and counters is valid writable storage.
    let result = unsafe { libc::fstatvfs(directory.as_raw_fd(), counters.as_mut_ptr()) };
    ensure!(result == 0, "Filesystem counters unavailable");
    // SAFETY: statvfs initialized the structure on success.
    let counters = unsafe { counters.assume_init() };
    capacity(
        counters.f_blocks,
        counters.f_bfree,
        counters.f_bavail,
        counters.f_frsize,
    )
}

fn collect_with(
    mounts: Result<String>,
    mut sample: impl FnMut(&str, &Mount) -> Result<(u64, u64, u64, f64)>,
) -> Vec<Disk> {
    [("System / SSD", "/"), ("Storage / RAID", "/srv/storage")]
        .into_iter()
        .map(|(name, target)| {
            let mut disk = Disk {
                name,
                mount: target,
                device: None,
                total_bytes: None,
                used_bytes: None,
                available_bytes: None,
                used_percent: None,
                status: "Statistics unavailable",
            };
            let mount = mounts
                .as_ref()
                .ok()
                .and_then(|text| find_mount(text, target).ok());
            match mount {
                Some(Some(mount)) => {
                    disk.device = Some(mount.source.clone());
                    if let Ok((total, used, available, percent)) = sample(target, &mount) {
                        disk.total_bytes = Some(total);
                        disk.used_bytes = Some(used);
                        disk.available_bytes = Some(available);
                        disk.used_percent = Some(percent);
                        disk.status = "Mounted";
                    }
                }
                Some(None) => disk.status = "Not mounted",
                None => (),
            }
            disk
        })
        .collect()
}

pub fn collect() -> Vec<Disk> {
    collect_with(
        fs::read_to_string("/proc/self/mountinfo").map_err(Into::into),
        read_capacity,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const MOUNTS: &str = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 1 9:127 / /srv/storage rw - ext4 /dev/md127 rw\n3 1 0:1 / /run rw - tmpfs tmpfs rw\n";

    #[test]
    fn capacity_distinguishes_free_and_available_reserved_blocks() {
        let (total, used, available, percent) = capacity(100, 40, 30, 1024).unwrap();
        assert_eq!((total, used, available), (102400, 61440, 30720));
        assert!((percent - 100.0 * 60.0 / 90.0).abs() < 0.001);
        assert!(capacity(0, 0, 0, 1024).is_err());
        assert!(capacity(100, 101, 20, 1024).is_err());
        assert!(capacity(u64::MAX, 0, 0, 2).is_err());
        assert_eq!(capacity(100, 0, 0, 1).unwrap().3, 100.0);
    }

    #[test]
    fn only_exact_target_mounts_are_sampled() {
        let mut paths = Vec::new();
        let disks = collect_with(Ok(MOUNTS.into()), |path, _| {
            paths.push(path.to_owned());
            capacity(100, 40, 30, 1024)
        });
        assert_eq!(paths, ["/", "/srv/storage"]);
        assert_eq!(disks[1].device.as_deref(), Some("/dev/md127"));
        assert_eq!(disks[1].status, "Mounted");
        assert!(find_mount(MOUNTS, "/srv").unwrap().is_none());
        assert_eq!(unescape("/a\\040b\\134040"), "/a b\\040");
    }

    #[test]
    fn missing_raid_never_samples_ssd_as_fallback_and_failures_stay_unknown() {
        let disks = collect_with(Ok(MOUNTS.lines().next().unwrap().into()), |path, _| {
            assert_eq!(path, "/");
            capacity(100, 40, 30, 1024)
        });
        assert_eq!(disks[1].status, "Not mounted");
        assert!(disks[1].total_bytes.is_none());
        let disks = collect_with(Ok(MOUNTS.into()), |_, _| anyhow::bail!("private error"));
        assert!(disks.iter().all(|disk| disk.total_bytes.is_none()));
        assert!(
            !serde_json::to_string(&disks)
                .unwrap()
                .contains("private error")
        );
        let disks = collect_with(Err(anyhow::anyhow!("fixture")), |_, _| {
            panic!("No mount table")
        });
        assert!(
            disks
                .iter()
                .all(|disk| disk.status == "Statistics unavailable")
        );
    }

    #[test]
    fn local_root_counters_can_be_read() {
        let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
        let root = find_mount(&mounts, "/").unwrap().unwrap();
        let (total, used, available, _) = read_capacity("/", &root).unwrap();
        assert!(total > 0 && used <= total && available <= total);
    }
}
