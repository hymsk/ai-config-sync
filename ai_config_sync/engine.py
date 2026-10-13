"""单向归档引擎：push（本机 → 仓库）与 pull（仓库 → 本机）。

两个方向都是显式、手动触发的一次性操作，无双向收敛、无常驻进程。
共用不变量：不 rebase、不 force push、不 reset、不 clean；不做三方合并，
本地分支只快进对齐远端；分叉一律报错保留现场交人工；每条 git 命令强制
core.autocrlf=false；提交只含显式路径。
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

    def _fetch(self):
        try:
            gitops.fetch(self.wt)
        except gitops.GitError as exc:
            if gitops.is_auth_error(exc.stderr):
                raise SyncError("error-auth", "凭据/权限失败: {}".format(exc))
            raise SyncError("error-transient", "网络失败: {}".format(exc))

    def _align(self, tolerate_local_ahead=False):
        """fetch 并把本地分支**只快进**对齐远端。

        分叉（本地与远端各有对方没有的提交）→ error-conflict 交人工；
        本地领先（存在未推送提交）→ 默认报错；tolerate_local_ahead=True
        时（push 场景，上轮推送失败留下的归档提交）容忍继续，提交之上
        继续追加本轮内容后一并推送。
        """
        self._fetch()
        origin = gitops.ref_sha(self.wt, self._remote_ref())
        if origin is None:
            raise SyncError(
                "error-remote",
                "远端分支 {} 不存在（可能被删除）".format(self.branch))
        local = gitops.head(self.wt)
        if local == origin:
            return
        if local is not None and gitops.is_ancestor(self.wt, local, origin):
            gitops.fast_forward(self.wt, self._remote_ref())
            return
        if local is not None and gitops.is_ancestor(self.wt, origin, local):
            if tolerate_local_ahead:
                self.log("本地分支领先远端（上轮未推送提交），继续追加")
                return
            raise SyncError(
                "error-conflict",
                "本地分支领先远端（存在未推送提交），已保留现场；请在 {} "
                "中手动处理（推送或丢弃）后重试".format(self.wt))
        raise SyncError(
            "error-conflict",
            "本地分支与远端 {} 分叉，已保留现场；请在 {} 中手动处理后重试"
            .format(self.branch, self.wt))

    def _load(self, include_repo_only=True):
        """读取生效配置与受管清单，返回 (cfg, rules, managed)。

        managed.files: host 绝对路径 -> 仓库相对路径 source/...
        include_repo_only=True 时并入"仓库有、本机无"的命中路径：
        - pull 用它重建本机缺失的文件；
        - push 用它计算真镜像的移除集合。
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
        if include_repo_only:
            # 并入仓库侧受管路径（本机缺失时用于 pull 重建 / push 移除）
            for repo_path in gitops.ls_tree(self.wt, "HEAD", "source"):
                host_path = "/" + repo_path[len("source/"):]
                if host_path in managed.files:
                    continue
                if os.path.exists(host_path):
                    continue
                if sync_list.match_path(rules, host_path) and not sync_list.in_zones(
                        host_path, zones):
                    managed.files[host_path] = repo_path
        return cfg, rules, managed

    def _check_prefixes(self, rules):
        """push 前置检查：纳入规则的字面前缀必须存在于本机。

        - 显式文件（无通配符）缺失，或目录前缀（如 /etc/xxx/**）整体不存在
          → error-missing，提示先变更 sync.list 或恢复路径再重试；
        - 前缀存在、目录内具体文件被删 → 不报错（真镜像会从仓库移除）。
        """
        missing = []
        for rule in rules:
            if rule.negated:
                continue
            prefix = sync_list.literal_prefix(rule.pattern)
            if not os.path.exists(prefix):
                missing.append(rule.pattern)
        if missing:
            detail = "; ".join(missing[:10]) + (
                " 等 {} 条".format(len(missing)) if len(missing) > 10 else "")
            raise SyncError(
                "error-missing",
                "以下清单规则指向的本机路径不存在: {}；请同步修改 "
                "sync.list（移除或改写规则）或恢复对应路径后重新执行 "
                "push".format(detail), 1)

    # ---------- push：本机 → 仓库 ----------

    def push(self):
        with self._lock():
            self._align(tolerate_local_ahead=True)
            dirty = gitops.status_porcelain(self.wt)
            if dirty:
                raise SyncError(
                    "error-dirty",
                    "工作树存在未提交改动，请先手动处理: " +
                    "; ".join(dirty[:5]))
            cfg, rules, managed = self._load()
            self._check_prefixes(rules)
            a_sha = gitops.head(self.wt)
            if a_sha is None:
                raise SyncError("error-state", "工作树无提交")

            # 真镜像收集：命中清单的本机文件集 = 仓库 source/ 的目标状态。
            changed = {}
            unstable = []
            for host_path in sorted(managed.files):
                host = Path(host_path)
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

            # 镜像移除（收窄）：命中清单规则、但本机已不存在的受管路径
            # 从仓库移除；清单外/他机自有内容绝不越界触碰。
            removed = [rp for hp, rp in managed.files.items()
                       if not Path(hp).is_file()]
            # 已收集 changed 的写入与 removed 的移除可共存于同一提交。
            if changed or removed:
                for host_path, (repo_path, data) in changed.items():
                    dest = self.wt / repo_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                gitops.add(self.wt, [changed[h][0] for h in changed])
                gitops.rm(self.wt, removed)
                if gitops.staged_exists(self.wt):
                    message = "sync: archive {} files, remove {} (host: {})".format(
                        len(changed), len(removed),
                        socket.gethostname().split(".")[0])
                    gitops.commit(self.wt, message, self.identity)

            try:
                gitops.push(self.wt, self.branch)
            except gitops.GitError as exc:
                if gitops.is_auth_error(exc.stderr):
                    raise SyncError("error-auth", "推送认证失败: {}".format(exc))
                if gitops.is_nonff_error(exc.stderr):
                    raise SyncError(
                        "error-conflict",
                        "推送被拒（远端有新提交）：请重新执行 push；"
                        "若持续失败请在 {} 手动检查".format(self.wt))
                raise SyncError("error-transient", "推送失败: {}".format(exc))
            msg = "归档 {} 个文件，移除 {} 个".format(len(changed), len(removed))
            if unstable:
                msg += "，跳过 {} 个未稳定文件".format(len(unstable))
            return ("ok", msg)

    # ---------- pull：仓库 → 本机 ----------

    def pull(self):
        with self._lock():
            # 本地领先时 HEAD 已包含远端全部内容，写回安全（上轮推送失败残留）
            self._align(tolerate_local_ahead=True)
            cfg, rules, managed = self._load()

            run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            applied, skipped, same = 0, [], 0
            for host_path in sorted(managed.files):
                repo_path = managed.files[host_path]
                blob = gitops.blob_at(self.wt, "HEAD", repo_path)
                if blob is None:
                    continue  # 仓库无此文件：pull 不删除本机文件
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
