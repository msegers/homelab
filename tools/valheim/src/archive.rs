//! Consistent archives: temporary 0600 file, sync, then publish without overwrite.
use crate::controller::Config;
use anyhow::{Context, Result, ensure};
use flate2::{Compression, write::GzEncoder};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};
use walkdir::WalkDir;

pub fn create(config: &Config, world: &str) -> Result<String> {
    let filename = format!(
        "manual-{}-{:016x}.tar.gz",
        chrono::Utc::now().format("%Y%m%dT%H%M%SZ"),
        rand::random::<u64>()
    );
    create_named(config, world, &filename)
}

pub fn create_named(config: &Config, world: &str, filename: &str) -> Result<String> {
    let destination =
        fs::canonicalize(&config.backups_dir).context("Backup destination missing")?;
    let entries = [
        (config.worlds_dir.clone(), "saves"),
        (config.config_dir.clone(), "config"),
        (
            config.project_dir.join("docker-compose.yml"),
            "docker-compose.yml",
        ),
    ];
    for (path, _) in &entries {
        let resolved = fs::canonicalize(path)
            .context("Backup source is missing; refusing incomplete archive")?;
        ensure!(
            !resolved.starts_with(&destination),
            "Backup source is inside the backup destination; refusing incomplete archive"
        );
    }
    let mut temporary = tempfile::Builder::new()
        .prefix("manual-")
        .suffix(".partial")
        .tempfile_in(&destination)?;
    write_archive(temporary.as_file_mut(), &entries, &destination, world)?;
    temporary.as_file().sync_all()?;
    let path = destination.join(filename);
    temporary
        .persist_noclobber(&path)
        .map_err(|error| error.error)
        .context("Cannot publish backup; existing archives preserved")?;
    // Make the published name durable before callers may delete its source.
    fs::File::open(&destination)?
        .sync_all()
        .context("Cannot sync backup directory; archive not confirmed durable")?;
    Ok(path.to_string_lossy().into_owned())
}

fn write_archive(
    writer: impl Write,
    entries: &[(PathBuf, &str)],
    destination: &Path,
    world: &str,
) -> Result<()> {
    let gzip = GzEncoder::new(writer, Compression::default());
    let mut archive = tar::Builder::new(gzip);
    archive.follow_symlinks(false);
    let selection = serde_json::to_vec(&serde_json::json!({"world": world}))?;
    let mut header = tar::Header::new_gnu();
    header.set_size(selection.len() as u64);
    header.set_mode(0o600);
    header.set_cksum();
    archive.append_data(&mut header, "world.json", selection.as_slice())?;
    for (source, name) in entries {
        if source.is_dir() {
            let paths = WalkDir::new(source)
                .follow_links(false)
                .into_iter()
                .filter_entry(|entry| {
                    !fs::canonicalize(entry.path()).is_ok_and(|path| path.starts_with(destination))
                });
            for entry in paths {
                let entry = entry?;
                let relative = entry.path().strip_prefix(source)?;
                archive.append_path_with_name(entry.path(), Path::new(name).join(relative))?;
            }
        } else {
            archive.append_path_with_name(source, name)?;
        }
    }
    archive.into_inner()?.finish()?.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FullDisk;
    impl Write for FullDisk {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::from_raw_os_error(libc::ENOSPC))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn compressed_archive_propagates_disk_full_instead_of_succeeding() {
        let error = write_archive(FullDisk, &[], Path::new("/backups"), "Home").unwrap_err();
        assert_eq!(
            error
                .downcast_ref::<std::io::Error>()
                .unwrap()
                .raw_os_error(),
            Some(libc::ENOSPC)
        );
    }
}
