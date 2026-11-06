# Seeded world creation

The new-world form accepts an optional, case-sensitive seed of 1–10 ASCII letters
or digits. Blank means native random generation. Seeds are not launch flags in
the current dedicated-server image, and `WORLD_NAME` is a save name, not a seed.

The browser sends the typed broker action `create`, with `world` and optional
`seed`. Ordinary `world` selection and all other actions reject a seed. The
controller's `create-world` operation runs under the same exclusive control lock
as switching, backups, start/stop, and deletion. It:

1. Validates the name/seed and rejects selected/running names, existing saves,
   incomplete saves and symlinks (including case-only collisions). Separately
   named native backups such as `Skoltr_backup_auto-20261105-190438` do not block
   recreating `Skoltr` and are never modified. Exact-name remnants such as
   `Skoltr.fwl.old` still block creation; backup-named new worlds are reserved.
2. Stops a running server cleanly and takes the configured pre-change archive.
   A failed stop or required archive prevents seed publication and selection.
3. For a custom seed, atomically publishes `<name>.fwl` **without replacement**.
   The file is synced before selection; it is private (`0600`) and owned by the
   saves-directory owner/group so the unprivileged game account can read it.
4. Persists the normal `world.json` selection and resumes only if previously
   running. A stopped server stays stopped until Start is pressed.

The metadata uses the legacy world-version-36 `.fwl` format: little-endian
length/version, .NET length-prefixed name/seed strings, Valheim's stable seed
hash, random world UID, generator version 2, `needsDB=false`, and no world
modifiers. It contains no terrain or database: the game generates those on first
start. No existing metadata is edited; subsequent starts/switches reuse the save
and its seed. No mods, extra containers, or external runtime tools are added.

The format and hash were checked against the documented implementation in
[MakeFwl v2, revision a7b3afc](https://github.com/Sleavely/MakeFwl/tree/a7b3afcb00e7ebd8e890990e05792a23fdcb843a).
The upstream [Docker seed-generation example](https://gist.github.com/Sleavely/9821e658a9d9d0679dbadb591bcf9d18)
also describes loading initial `.fwl` metadata on Valheim 1.0. The Rust writer
has no dependency on MakeFwl; these are format references, not installed programs.

## Failure behavior

- Existing worlds cannot be reseeded. Use a different new name.
- Safe creation-rejection reasons are returned to the client. Progress, success,
  validation, and failure messages appear beside the world form and are not
  replaced by background refresh errors. Detailed filesystem/Docker errors stay
  in server logs rather than leaking paths or secrets through the API.
- The original backup policy and confirmation prompts remain in place. Deletion
  still requires a successful archive, independently of the switch-backup flag.
- If metadata publication completes but selection/resume fails, retain the new
  metadata for recovery. Do not delete it or automatically generate another seed.
  Check selected/running state before retrying. An aborted publication may leave
  recoverable metadata after a filesystem sync failure; it is never overwritten.
- The seed is in the game metadata, not a new secret or Ansible setting. The
  existing selected-world JSON schema is unchanged and Ansible preserves it.

## Acceptance after an authorized deployment

Automated checks cover byte layout/hash fixtures, validation at HTTP/broker/
controller boundaries, filesystem safeguards, owner/mode, backups, failure paths,
and UI behavior with fake engines. They **do not execute the real game**.

Before using valuable worlds:

1. Start with a stopped server. Create a disposable world with a known seed such
   as `Potato`. Confirm selection changes but the server stays stopped.
2. Start it and verify game logs report that seed, not an auto-generated one.
   Compare terrain with a freshly created in-game world using the same seed and
   the same Valheim version; generation can change across game versions.
3. Restart and switch away/back. Confirm the same world/seed is retained.
4. Repeat with a running server to verify clean stop, backup, and resume.
5. Test blank-seed creation and rejection of a seeded request for an existing
   world. Verify old saves and backups remain unchanged.

If a future Valheim release stops importing this metadata format, update the
writer and fixtures and repeat acceptance before deployment; do not silently
fall back to a random seed when a custom seed was requested.
