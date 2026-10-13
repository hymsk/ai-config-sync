# Changelog

Version history is kept newest first. The repository is currently a private, unreleased baseline; no package has been published and no tag exists unless stated otherwise.

## Unreleased

### Changed

- **Reworked into a manual one-direction tool.** The daemon (`run`), the single-round transaction, the crash-recovery journal and the conflict pause flag (`~/ERROR_MERGE_FLAG`) are removed. The CLI now exposes exactly two explicit, one-shot operations:
  - `push` (host -> repo): archive managed host files into the config repo and push.
  - `pull` (repo -> host): write the repo's managed files back to the host, with a pre-write backup, format validation and atomic replace.
  There is no automatic two-way convergence; every direction is triggered by hand.
- `.sync-config/config.toml` no longer reads `interval` / `remote_check_interval` (they belonged to the daemon); only `max_backups_per_file` and the `[git]` identity remain.
- The systemd unit is removed; `deploy/uninstall.sh` now only uninstalls the package and cleans up a data directory on `--purge-data` (with a best-effort removal of a legacy unit if one is found).
- README/SPEC/AGENTS/CHANGELOG rewritten for the two-mode model; version bumped to 0.2.0.

### Kept

- The strict conflict policy and the `aics-conflict` merge driver (dual modification of the same file always conflicts, scene preserved for a human).
- Byte fidelity (`core.autocrlf=false` plus a `* -text` seed `.gitattributes`), stable two-read collection, hard exclusion of the tool's own directories, traversal-forbidden roots and `.git`.
- `install new` (empty repo or `--force` orphan-branch seeding) and `install attach` (existing branch + validated `.sync-config`), rejecting URLs with embedded credentials.
- `sync.list` include lists (absolute paths, `!` exclusion, last rule wins, `**` segments).
- Signal-based status codes and backup pruning; non-fast-forward push retries.

### Removed

- `ai_config_sync/agent.py`, `ai_config_sync/adopt.py`, `ai_config_sync/state.py`, the systemd unit, and the P0-P7 transaction / journal / flag machinery.

## 0.1.0 — initial baseline

Initial source baseline of the configuration synchronization agent.

### Added

- Rule-driven two-way sync of managed regular files between Linux hosts through a dedicated Git config repository (`source/` mirroring host absolute paths), with no LLM and no external Python dependencies.
- Single-round transaction P0-P7 with an append-only fsynced journal: capture, merge, push, validated backup + atomic write-back, checkpoint; crash recovery resumes from the last transaction record.
- Conflict policy: pause only the local machine via `~/ERROR_MERGE_FLAG`, preserve the Git conflict scene, re-set the flag if it is deleted without resolution, and write back the resolved result only when the host still matches the hash recorded at conflict time.
- First-round adoption with difference preview and `--prefer local|repo|merge`; single-sided files converge in every mode.
- `sync.list` include lists (absolute paths, `!` exclusion, last rule wins, `**` segments) with hard exclusion of the agent's own directories, traversal-forbidden roots and `.git`.
- `install new` (empty repo or `--force` orphan-branch seeding) and `install attach` (existing branch + validated `.sync-config`), rejecting URLs with embedded credentials and pinning resolved branches into `/etc/ai-config-sync/bootstrap.toml`.
- Byte fidelity (forced `core.autocrlf=false` plus a `* -text` seed `.gitattributes`), stable two-read collection, mid-round edit protection, delete-recreation, a systemd unit, and a 26-check isolated test suite using a local bare repository as the remote.

### Scope

- Private local baseline only; no package publication and no tag.
