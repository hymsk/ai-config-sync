# Changelog

Version history is kept newest first. The repository is currently a private, unreleased baseline; no package has been published and no tag exists unless stated otherwise.

## Unreleased

### Fixed

- `once --prefer merge` no longer crashes with a traceback when its push fails after a successful adoption merge; it returns `error-transient` (or `error-auth` for credential failures) and lets the next regular round retry the push from the already-committed merge.
- Remote authentication detection no longer treats a bare local `permission denied` (e.g. a broken `.git` directory owner) as a credential failure; only remote rejections such as `permission denied (publickey)` or HTTP 401/403 classify as `error-auth`.
- Backup pruning now also removes the emptied `source/...` intermediate directories inside a run directory, so pruned runs disappear instead of leaving empty trees.

### Changed

- **Dual modification of the same file now always conflicts.** A merge driver
  (`aics-conflict`, defined as always-fail) is declared for `source/**` and
  `.sync-config/**`, so a file changed on both the host and the remote is
  handed to a human even when git could auto-merge the changes line-wise —
  line-level fusion could silently produce a mixed configuration neither
  side wanted. One-sided changes and changes to different files still
  converge automatically. The conflict scene changes accordingly: the
  worktree keeps the full ours-side content without `<<<<<<<` markers, with
  both sides available through index stages `:2:`/`:3:`. The agent injects
  the driver definition into every merge and re-asserts the local fallback
  (`.git/info/attributes` + local config) each round, so existing
  installations gain the protection without manual upgrades and manual
  `git merge` is covered as well.
- `state/sync.log` rotates (1 MB, 5 backups) instead of growing unbounded.
- Exit code 3 with a clear message when another instance holds the lock (`run`, `once --prefer`, `doctor`).
- `sync_list._match` / `_in_zones` are now public `match_path` / `in_zones`.

### Added

- Optional `max_backups_per_file` in `.sync-config/config.toml` (default 20, `0` disables pruning): pre-write backups per file are pruned by run id after each write-back.
- Open-source documentation set aligned with the project standard: this changelog, English `README.md` + Chinese `README.zh-cn.md`, behavior specification `SPEC.md`, project `AGENTS.md`, `RELEASING.md`, AGPL-3.0-or-later `LICENSE`, and a conservative `deploy/uninstall.sh` (data directories removed only with `--purge-data` and explicit confirmation).

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
