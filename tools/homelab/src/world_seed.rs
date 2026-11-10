//! Initial legacy .fwl metadata; Valheim generates the terrain/database on start.
//! Format reference: MakeFwl v2 (world version 36, generator 2), also readable
//! by Valheim 1.0's legacy-save importer. See docs/world-seeds.md for references.
use anyhow::{Result, ensure};
use std::{
    fs,
    io::Write,
    os::unix::{fs::MetadataExt, io::AsRawFd},
    path::Path,
};

/// Deliberately public-safe reasons; never forward arbitrary controller/Docker
/// errors, paths, or configuration contents to the web process.
#[derive(Debug)]
pub enum CreationError {
    InvalidName,
    InvalidSeed,
    BackupName,
    NameInUse,
    Selected,
    Running,
}

impl std::fmt::Display for CreationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::InvalidName => "Use a world name with letters, numbers, underscores or hyphens (up to 128 characters).",
            Self::InvalidSeed => "Seeds use 1–10 letters or numbers (case-sensitive).",
            Self::BackupName => "Choose a name without _backup_; native backup names are reserved.",
            Self::NameInUse => "That name still has world saves or incomplete save files. Select the existing world or choose another name; no existing files were overwritten.",
            Self::Selected => "That world is already selected. Choose a different new name.",
            Self::Running => "That world is running. Choose a different new name.",
        })
    }
}

impl std::error::Error for CreationError {}

pub fn valid_seed(seed: &str) -> bool {
    !seed.is_empty() && seed.len() <= 10 && seed.bytes().all(|byte| byte.is_ascii_alphanumeric())
}

/// Valheim's case-sensitive stable string hash (two interleaved DJB2 hashes).
fn seed_hash(seed: &str) -> i32 {
    let hash = |offset| {
        seed.bytes()
            .skip(offset)
            .step_by(2)
            .fold(5381_u32, |hash, byte| {
                hash.wrapping_mul(33) ^ u32::from(byte)
            })
    };
    hash(0).wrapping_add(hash(1).wrapping_mul(1_566_083_941)) as i32
}

fn string(bytes: &mut Vec<u8>, value: &str) {
    // .NET BinaryWriter strings use a 7-bit encoded UTF-8 byte length.
    let mut length = value.len();
    while length >= 128 {
        bytes.push((length as u8 & 0x7f) | 0x80);
        length >>= 7;
    }
    bytes.push(length as u8);
    bytes.extend_from_slice(value.as_bytes());
}

fn metadata(name: &str, seed: &str, uid: i64) -> Vec<u8> {
    let mut bytes = vec![0; 4];
    bytes.extend_from_slice(&36_i32.to_le_bytes());
    string(&mut bytes, name);
    string(&mut bytes, seed);
    bytes.extend_from_slice(&seed_hash(seed).to_le_bytes());
    bytes.extend_from_slice(&uid.to_le_bytes());
    bytes.extend_from_slice(&2_i32.to_le_bytes());
    bytes.push(0); // No existing database required: generate a new world.
    bytes.extend_from_slice(&0_i32.to_le_bytes()); // Default world modifiers.
    let length = (bytes.len() - 4) as i32;
    bytes[..4].copy_from_slice(&length.to_le_bytes());
    bytes
}

/// Reject primary/incomplete saves and dangling symlinks, but ignore separately
/// named native backups: those neither occupy nor get modified at the new path.
pub fn require_new(directory: &Path, name: &str) -> Result<()> {
    ensure!(
        crate::controller::valid_world(name),
        CreationError::InvalidName
    );
    ensure!(!name.contains("_backup_"), CreationError::BackupName);
    let name = name.to_ascii_lowercase();
    for entry in fs::read_dir(directory)? {
        let filename = entry?.file_name().to_string_lossy().to_ascii_lowercase();
        ensure!(
            filename != name && !filename.starts_with(&format!("{name}.")),
            CreationError::NameInUse
        );
    }
    Ok(())
}

/// Publish a complete, durable file without replacing anything. The owner matches
/// the saves directory, so the unprivileged game process can read private metadata.
pub fn create(directory: &Path, name: &str, seed: &str) -> Result<()> {
    ensure!(valid_seed(seed), CreationError::InvalidSeed);
    require_new(directory, name)?;
    let owner = fs::metadata(directory)?;
    let mut file = tempfile::NamedTempFile::new_in(directory)?;
    let current = file.as_file().metadata()?;
    if (current.uid(), current.gid()) != (owner.uid(), owner.gid()) {
        // SAFETY: the descriptor belongs to this live temporary file.
        let result = unsafe { libc::fchown(file.as_raw_fd(), owner.uid(), owner.gid()) };
        if result != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    file.write_all(&metadata(name, seed, rand::random::<i64>() & i64::MAX))?;
    file.flush()?;
    file.as_file().sync_all()?;
    // Recheck after writing; persist_noclobber independently protects the target.
    require_new(directory, name)?;
    file.persist_noclobber(directory.join(format!("{name}.fwl")))
        .map_err(|error| error.error)?;
    fs::File::open(directory)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metadata_matches_binarywriter_layout_and_stable_hash() {
        // Fixed external-format fixture: size, v36, name, seed, hash, UID,
        // generator version, needsDB=false, zero modifiers. No round-trip encoder.
        let expected = [
            37, 0, 0, 0, 36, 0, 0, 0, 4, 72, 111, 109, 101, 6, 80, 111, 116, 97, 116, 111, 41, 170,
            156, 131, 42, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0,
        ];
        assert_eq!(metadata("Home", "Potato", 42), expected);
        assert_ne!(seed_hash("Potato"), seed_hash("potato"));
        assert_eq!(seed_hash("a"), 372029373);
        assert_eq!(seed_hash("abc"), 1099313834);
        let long_name = "a".repeat(128);
        assert_eq!(&metadata(&long_name, "a", 42)[8..10], &[0x80, 0x01]);
    }

    #[test]
    fn seed_validation_does_not_trim_or_change_case() {
        for seed in ["A", "0123456789", "aB12"] {
            assert!(valid_seed(seed));
        }
        for seed in [
            "",
            "12345678901",
            " a",
            "a ",
            "a-b",
            "a_b",
            "é",
            "a\n",
            "$(id)",
        ] {
            assert!(!valid_seed(seed), "{seed:?}");
        }
    }

    #[test]
    fn publication_never_overwrites_metadata_or_follows_a_target_symlink() {
        let directory = tempfile::tempdir().unwrap();
        create(directory.path(), "NewWorld", "Potato").unwrap();
        let path = directory.path().join("NewWorld.fwl");
        let original = fs::read(&path).unwrap();
        assert!(create(directory.path(), "NewWorld", "Other").is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        fs::remove_file(&path).unwrap();
        let outside = directory.path().join("unrelated");
        fs::write(&outside, "keep me").unwrap();
        std::os::unix::fs::symlink(&outside, &path).unwrap();
        assert!(create(directory.path(), "NewWorld", "Other").is_err());
        assert_eq!(fs::read(&outside).unwrap(), b"keep me");
        assert!(
            fs::symlink_metadata(&path)
                .unwrap()
                .file_type()
                .is_symlink()
        );
    }
}
