"""单向归档引擎：push（本机 → 仓库）与 pull（仓库 → 本机）。

两个方向都是显式、手动触发的一次性操作，无双向收敛、无常驻进程。
共用不变量：不 rebase、不 force push、不 reset、不 clean；提交只含显式
路径；每条 git 命令强制 core.autocrlf=false。
"""

import fcntl
import os
import socket
import time
from pathlib import Path

from . import applier, collector, gitops, sync_list
from .config import Bootstrap, ConfigError, Layout, parse_repo_config


class SyncError(Exception):
    """携带状态码与退出码的操作失败。"""

    def __init__(self, status, message="", exit_code=1):
        super().__init__(message or status)
        self.status = status
        self.message = message or status
        self.exit_code = exit_code


class Sync:
    def __init__(self, boot: Bootstrap, layout: Layout, log=None):
        self.boot = boot
        self.layout = layout
        self.wt = Path(boot.worktree)
        self.branch = boot.branch
        self.identity = ("sync-bot", "sync-bot@invalid")
        self.max_backups = 20
        self.log = log or (lambda msg: None)
        layout.state_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------

    def _lock(self):
        path = self.layout.lock_path

        class _L:
            def __enter__(self):
                self.fd = open(path, "a+")
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def __exit__(self, *args):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()

        return _L()

    def _remote_ref(self):
        return "origin/" + self.branch

    def _load(self, tree_ref="HEAD"):
        """读取生效配置与受管清单，返回 (cfg, rules, managed)。

        managed.files: host 绝对路径 -> 仓库相对路径 source/...
        tree_ref 指定仓库侧路径来源；pull/push 均使用 HEAD（先 fetch+merge）。
        """
        cfg_path = self.wt / ".sync-config" / "config.toml"
        list_path = self.wt / ".sync-config" / "sync.list"
        try:
            cfg = parse_repo_config(cfg_path.read_text(encoding="utf-8"))
        except OSError:
            raise SyncError("error-config", "缺少 .sync-config/config.toml", 2)
        except ConfigError as exc:
            raise SyncError("error-config", str(exc), 2)
        self.identity = (cfg.git_name, cfg.git_email)
        self.max_backups = cfg.max_backups_per_file
        try:
            list_text = list_path.read_text(encoding="utf-8")
        except OSError:
            raise SyncError("error-config", "缺少 .sync-config/sync.list", 2)
        try:
            rules = sync_list.parse(list_text)
        except sync_list.SyncListError as exc:
            raise SyncError("error-config", str(exc), 2)
        zones = [str(z) for z in
                 self.layout.hard_zones(self.wt, self.boot.install_dir)]
        try:
            managed = sync_list.expand(rules, zones, log=self.log)
        except sync_list.SyncListError as exc:
            raise SyncError("error-config", str(exc), 2)
        # 并入仓库侧受管路径（本机缺失时用于 pull 重建）
        for repo_path in gitops.ls_tree(self.wt, tree_ref, "source"):
            host_path = "/" + repo_path[len("source/"):]
            if host_path in managed.files:
                continue
            if os.path.exists(host_path):
                continue
            if sync_list.match_path(rules, host_path) and not sync_list.in_zones(
                    host_path, zones):
                managed.files[host_path] = repo_path
        return cfg, rules, managed

    def _integrate(self):
        """fetch + 合并远端；仅快进/干净合并，真实冲突交人工。"""
        try:
            gitops.fetch(self.wt)
        except gitops.GitError as exc:
            if gitops.is_auth_error(exc.stderr):
                raise SyncError("error-auth", "凭据/权限失败: {}".format(exc))
            raise SyncError("error-transient", "网络失败: {}".format(exc))
        origin = gitops.ref_sha(self.wt, self._remote_ref())
        if origin is None:
            raise SyncError(
                "error-remote",
                "远端分支 {} 不存在（可能被删除）".format(self.branch))
        if origin != gitops.head(self.wt):
            rc = gitops.merge(self.wt, self._remote_ref())
            if rc != 0:
                raise SyncError(
                    "error-conflict",
                    "合并冲突，已保留现场；请在工作树手动解决后重试")
        return gitops.head(self.wt)

    # ---------- push：本机 → 仓库 ----------

    def push(self):
        with self._lock():
            dirty = gitops.status_porcelain(self.wt)
            if dirty:
                raise SyncError(
                    "error-dirty",
                    "工作树存在未提交改动，请先手动处理: " +
                    "; ".join(dirty[:5]))
            cfg, rules, managed = self._load()
            a_sha = gitops.head(self.wt)
            if a_sha is None:
                raise SyncError("error-state", "工作树无提交")

            # 先采集本机变化并提交（不联网），再整合远端；
            # 这样双侧改同一文件是真三方合并，由 merge driver 强制冲突。
            changed = {}
            unstable = []
            for host_path in sorted(managed.files):
                host = Path(host_path)
                if host.is_symlink():
                    self.log("跳过软链接: " + host_path)
                    continue
                if not host.is_file():
                    continue
                got = collector.read_stable(host)
                if got is None:
                    unstable.append(host_path)
                    continue
                data, digest = got
                blob = gitops.blob_at(self.wt, "HEAD", managed.files[host_path])
                if blob is None or collector.sha256_bytes(blob) != digest:
                    changed[host_path] = (managed.files[host_path], data)

            if changed:
                for host_path, (repo_path, data) in changed.items():
                    dest = self.wt / repo_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                gitops.add(self.wt, [changed[h][0] for h in changed])
                if gitops.staged_exists(self.wt):
                    message = "sync: archive {} files (host: {})".format(
                        len(changed), socket.gethostname().split(".")[0])
                    gitops.commit(self.wt, message, self.identity)

            self._integrate()
            self._push_with_retry()
            msg = "归档 {} 个文件".format(len(changed))
            if unstable:
                msg += "，跳过 {} 个未稳定文件".format(len(unstable))
            return ("ok", msg)

    def _push_with_retry(self):
        for _ in range(3):
            try:
                gitops.push(self.wt, self.branch)
                return
            except gitops.GitError as exc:
                if gitops.is_auth_error(exc.stderr):
                    raise SyncError("error-auth", "推送认证失败: {}".format(exc))
                try:
                    gitops.fetch(self.wt)
                except gitops.GitError as exc2:
                    raise SyncError("error-transient", "网络失败: {}".format(exc2))
                origin = gitops.ref_sha(self.wt, self._remote_ref())
                if origin is None:
                    raise SyncError("error-remote", "远端分支消失")
                if origin != gitops.head(self.wt):
                    rc = gitops.merge(self.wt, self._remote_ref())
                    if rc != 0:
                        raise SyncError(
                            "error-conflict",
                            "推送前合并冲突，已保留现场；请手动解决后重试")
        raise SyncError("error-transient", "推送重试耗尽")

    # ---------- pull：仓库 → 本机 ----------

    def pull(self):
        with self._lock():
            self._integrate()
            cfg, rules, managed = self._load()

            run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            applied, skipped, same = 0, [], 0
            for host_path in sorted(managed.files):
                repo_path = managed.files[host_path]
                blob = gitops.blob_at(self.wt, "HEAD", repo_path)
                if blob is None:
                    continue  # 仓库无此文件：删除不传播，本机保留
                host = Path(host_path)
                if host.is_symlink():
                    skipped.append("软链接跳过: {}".format(host_path))
                    continue
                if host.exists():
                    try:
                        if collector.sha256_bytes(host.read_bytes()) == \
                                collector.sha256_bytes(blob):
                            same += 1
                            continue
                    except OSError:
                        skipped.append("读取失败: {}".format(host_path))
                        continue
                try:
                    applier.validate(host, blob)
                except applier.ApplyError as exc:
                    skipped.append(str(exc))
                    continue
                applier.backup(host, self.layout.backups_dir, run_id, repo_path)
                applier.prune_backups(self.layout.backups_dir, repo_path,
                                      self.max_backups)
                applier.atomic_write(host, blob)
                applied += 1

            msg = "写回 {} 个文件（已一致 {}）".format(applied, same)
            if skipped:
                msg += "，跳过 {} 个".format(len(skipped))
            return ("ok", msg)
