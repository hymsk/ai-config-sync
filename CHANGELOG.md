# Changelog

Version history is kept newest first. The repository is a private baseline; no package has been published — releases are Git tags only.

## 0.6.0

### Added

- **Interrupted-commit recovery in `commit`/`push`.** Both steps now begin by committing any pending `source/` mirror changes left behind by an interrupted `commit` — modified, staged, deleted or untracked-new mirrors — **as-is, using the current worktree content**: `git add` for existing paths, `git rm` for deleted ones, then a single `sync: finalize N pending mirror changes (host: …)` commit. Pending local changes are thus committed and pushed with no manual git work. Host files are never re-read for this, and `push` still never reads the include list nor recomputes archives.
- **`clear` command.** `ai-config-sync clear` discards ALL uncommitted changes in the local config worktree: `git reset --hard HEAD` (tracked files, staged included) + `git clean -fd` (untracked files/dirs; ignored files and nested git repositories stay). Offline, never touches host files, holds the exclusive lock, is a no-op on a clean worktree, and reports `error-dirty` if anything cannot be discarded. It is the single deliberate exception to the "never reset / never clean" invariant. For `source/` mirror leftovers it offers the opposite exit of the finalize path: `push`/`commit` commit them (worktree wins), `clear` drops them (HEAD wins).

### Changed

- **`commit`/`push` no longer abort with `error-dirty` on pending `source/` mirror changes** — they finalize them first (see above). Unknown changes — anything in `.sync-config`, outside `source/`, or rename entries — still fail with `error-dirty` for manual handling.
- **`pull` still requires a clean worktree but explains it**: pending mirror leftovers are rejected with a hint to run `push` (or `commit`) first, which finalizes them automatically; other unknown changes keep the plain manual-handling message.

## 0.5.0

### Added

- **Friendly CLI help.** `ai-config-sync`, `-h` and `--help` print the same overview: an `action` table listing the four atomic steps plus the three combinations, each with a plain-language description and its data flow in a third column, followed by a `command` table (`install`/`update`). Columns are aligned by terminal display width (CJK counts as two). Steps, `install new`/`attach` and `update` gained descriptions in their own `-h`.

### Changed

- **No argument now prints help and exits 0** instead of argparse's `the following arguments are required: cmd`; an unknown action reports `未知操作或命令: ...` (the internal `dest` name is no longer exposed). `install` without a mode reports which modes exist instead of `required: mode`.
- Help wording unified: **本机配置 / 本地仓库 / 远端仓库**, with arrows kept in the flow column; `update` is described as `更新工具`.

## 0.4.0

### Added

- **Orthogonal four-step model with fixed combinations.** The CLI now exposes four atomic steps, each doing exactly one thing:
  - `commit` (host files -> local config): mirror managed files into the worktree and make a local commit; offline.
  - `push` (local config -> remote): push local commits only; never recompute host files.
  - `sync` (local config -> host): write HEAD's managed files back to the host, with a pre-write backup, format validation and atomic replace; offline and never deletes host files.
  - `pull` (remote -> local config): fetch + fast-forward-only align; never touches host files.

  Only three combinations are accepted, written in execution order: `commit push` (host files -> remote), `push sync` (local-git truth) and `pull sync` (remote -> host). Abnormal sequences such as `push commit` are rejected with exit code 2. Every call holds `state/lock` for its whole run. These combinations replace the previous all-in-one `push`/`pull` semantics.

### Changed

- **`push` no longer recomputes host files, and the same-file-both-sides behavior changed.** In the split model `push` only pushes local commits and refuses when the remote is ahead or diverged (`error-conflict`) — it never fast-forwards. The old all-in-one `push` is gone, so a file modified on both sides is no longer silently overwritten by local content: you must `pull`, reconcile locally, then `commit push` (or `push sync` when the local content is already a commit).
- **Mirror removal and the prefix check moved from `push` to `commit`.** Managed paths that no longer exist on this host are `git rm`'ed during `commit` (the local mirror step), and every include rule's literal prefix must exist on this host at `commit` time (`error-missing`). `sync` performs neither.
- **`pull` no longer writes back and `sync` no longer fetches.** `pull` is fetch + `--ff-only` align only; `sync` is write-back only (pre-write backup, format validation, atomic replace, never deletes host files). `pull sync` is the combined write-back.
- Version bumped to 0.4.0.

## 0.3.0

### Added

- `update` command and pre-run self-update check: the version is tag-driven only (highest `vX.Y.Z` on the tool's remote via `ls-remote --tags`); updating fast-forwards the install repository to the tag (`--ff-only`, no merge/force/reset) and re-executes the running command. `push`/`pull` check automatically with a 5s timeout (failures warn but never block); manual `update` uses 60s. A diverged install repo is reported for manual handling; a non-git installation says it cannot self-update.

### Changed

- **No merging of any kind; mirror semantics.** Both `push` and `pull` now only ever fast-forward the local branch (`--ff-only`) to the remote. Divergence (local and remote each holding commits the other lacks) stops with `error-conflict`, preserving the scene for manual handling. The `aics-conflict` merge driver, merge-based integration, and the non-fast-forward auto-retry loop are all removed.
- **`push` is an exact mirror of this host's managed files.** Managed paths that no longer exist on this host are removed from the repo (`git rm`) — deletions now propagate in the push direction. The last pusher wins; to keep another machine's archived files, `pull` before `push`. The same file modified on both sides no longer conflicts: local content overwrites the remote (local priority).
- **Prefix existence check on `push`.** Every include rule's literal prefix (an explicitly listed file, or the directory of a `/etc/xxx/**`-style rule) must exist on this host; otherwise `error-missing` lists the offending rules and asks for a sync.list change or a path restore. Files deleted *inside* an existing directory are fine (silent mirror removal). `pull` performs no such check — a missing host file is exactly what it reconstructs.
- `pull` writes back with pre-write backup, format validation and atomic replace as before, and never deletes host files.
- **Reworked into a manual one-direction tool.** The daemon (`run`), the single-round transaction, the crash-recovery journal and the conflict pause flag (`~/ERROR_MERGE_FLAG`) are removed. The CLI now exposes exactly two explicit, one-shot operations:
  - `push` (host -> repo): archive managed host files into the config repo and push.
  - `pull` (repo -> host): write the repo's managed files back to the host, with a pre-write backup, format validation and atomic replace.
  There is no automatic two-way convergence; every direction is triggered by hand.
- `.sync-config/config.toml` no longer reads `interval` / `remote_check_interval` (they belonged to the daemon); only `max_backups_per_file` and the `[git]` identity remain.
- The systemd unit is removed; `deploy/uninstall.sh` now only uninstalls the package and cleans up a data directory on `--purge-data` (with a best-effort removal of a legacy unit if one is found).
- README/SPEC/AGENTS/CHANGELOG rewritten for the mirror model; version bumped to 0.3.0.

### Kept

- Byte fidelity (`core.autocrlf=false` plus a `* -text` seed `.gitattributes`), stable two-read collection, hard exclusion of the tool's own directories, traversal-forbidden roots and `.git`.
- `install new` (empty repo or `--force` orphan-branch seeding) and `install attach` (existing branch + validated `.sync-config`), rejecting URLs with embedded credentials.
- `sync.list` include lists (absolute paths, `!` exclusion, last rule wins, `**` segments).
- Signal-based status codes and backup pruning.

### Removed

- The `aics-conflict` merge driver and its installer hook (`ensure_conflict_driver`), the merge-based `_integrate` path, and the automatic non-fast-forward retry loop.
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
