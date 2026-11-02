"""单向归档引擎：四个原子步骤 + 固定组合。

原子步骤（每个只做一件事，边界明确）：
- commit：本机受管文件 → 本地 config（镜像写入工作树 + 本地提交；不联网）
- push：  本地 config → 远端（先把 source/ 残留镜像改动按当前内容补提交，
  再只推送本地提交；不读清单、不重算主机文件）
- sync：  本地 config → 本机（写回受管文件；不联网、永不删除主机文件）
- pull：  远端 → 本地 config（fetch + 仅 --ff-only 对齐；不碰主机文件）

CLI 只接受按执行顺序书写的固定组合：commit push / push sync / pull sync。

共用不变量：不 rebase、不 force push、不 reset、不 clean（clear 命令是
显式例外）；不做三方合并；
远端存在本地没有的提交一律报错保留现场交人工；每条 git 命令强制
core.autocrlf=false；commit/sync 的提交与写回只含显式路径。
commit/push 开始时把上次 commit 中断残留在 source/ 的镜像改动按工作树
当前内容补提交（中断自恢复，无需人工善后）；其余未知改动 error-dirty 交人工。
"""

import os
import socket
import time
from pathlib import Path

from . import applier, collector, gitops, sync_list
from .config import Bootstrap, ConfigError, Layout, parse_repo_config
from .pathmap import PathMap

try:
    import fcntl
except ImportError:  # Windows 原生 Python：用 msvcrt.locking 等价替代
    fcntl = None
    import msvcrt


class SyncError(Exception):
    """携带状态码与退出码的操作失败。"""

    def __init__(self, status, message="", exit_code=1):
        super().__init__(message or status)
        self.status = status
        self.message = message or status
        self.exit_code = exit_code


class Sync:
    _STEPS = {
        "commit": "_step_commit",
        "push": "_step_push",
        "sync": "_step_sync",
        "pull": "_step_pull",
    }

    def __init__(self, boot: Bootstrap, layout: Layout, log=None):
        self.boot = boot
        self.layout = layout
        self.wt = Path(boot.worktree)
        self.branch = boot.branch
        self.identity = ("sync-bot", "sync-bot@invalid")
        self.max_backups = 20
        self.path_map = PathMap()  # 默认 {"~": "__user_home__"}；_load 时按配置严格重建
        self.log = log or (lambda msg: None)
        layout.state_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------

    def _lock(self):
        path = self.layout.lock_path

        def _try_lock(fd):
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError(*exc.args) from exc

        def _unlock(fd):
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

        class _L:
            def __enter__(self):
                self.fd = open(path, "a+")
                try:
                    _try_lock(self.fd)
                except Exception:
                    self.fd.close()
                    raise

            def __exit__(self, *args):
                try:
                    _unlock(self.fd)
                finally:
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

    def _check_branch(self):
        """当前分支必须是 bootstrap 分支（detached 或其他分支 → 拒绝）。"""
        if gitops.current_branch(self.wt) != self.branch:
            raise SyncError(
                "error-conflict",
                "当前分支不是 {}（detached 或位于其他分支），已保留现场；"
                "请在 {} 中手动处理".format(self.branch, self.wt))

    def _split_dirty(self):
        """把工作树脏条目分为（source/ 镜像路径, 未知改动条目）。

        镜像残留 = 上次 commit 中断在 source/ 下留下的改动
        （含已修改/已暂存/已删除/未跟踪的新镜像）；
        .sync-config、source/ 之外的改动、重命名条目属未知改动，交人工。
        """
        mirror, unknown = [], []
        for entry in gitops.status_porcelain(self.wt):
            path = entry[3:]
            if " -> " in path or not path.startswith("source/"):
                unknown.append(entry)
            else:
                mirror.append(path)
        return mirror, unknown

    def _finalize_pending(self, step):
        """了结上次 commit 中断残留的镜像改动（commit/push 共用）。

        以工作树当前内容为准补一个本地提交，不重读主机文件：
        文件存在 → git add；已删除 → git rm；有暂存内容则提交。
        未知改动 → error-dirty 人工处理。
        返回给用户看的了结摘要（无残留时为 None）。
        """
        mirror, unknown = self._split_dirty()
        if unknown:
            raise SyncError(
                "error-dirty",
                "工作树存在未知改动（source/ 之外或 .sync-config），无法{}，"
                "请先手动处理: {}".format(step, "; ".join(unknown[:5])))
        if not mirror:
            return None
        self._refresh_identity()
        add_paths, rm_paths = [], []
        for path in mirror:
            target = add_paths if (self.wt / path).exists() else rm_paths
            target.append(path)
        gitops.add(self.wt, add_paths)
        gitops.rm(self.wt, rm_paths)
        if gitops.staged_exists(self.wt):
            gitops.commit(
                self.wt,
                "sync: finalize {} pending mirror changes (host: {})".format(
                    len(mirror), socket.gethostname().split(".")[0]),
                self.identity)
        add_set = set(add_paths)
        return self._with_detail(
            "已按当前内容补提交残留镜像改动 {} 个".format(len(mirror)),
            [("+ " if p in add_set else "- ") + self._host_of(p)
             for p in mirror])

    def _refresh_identity(self):
        """尽力读取配置中的提交身份与路径映射；失败保持默认（主流程 _load 会严格报错）。"""
        try:
            cfg = parse_repo_config(
                (self.wt / ".sync-config" / "config.toml").read_text(
                    encoding="utf-8"))
            self.identity = (cfg.git_name, cfg.git_email)
            self.path_map = cfg.path_map
        except (OSError, ConfigError):
            pass

    @staticmethod
    def _join(prefix, msg):
        return "{}\n{}".format(prefix, msg) if prefix else msg

    def _host_of(self, repo_path):
        """仓库相对路径 source/... 对应的主机绝对路径（按 path_map 逆映射）。"""
        return self.path_map.to_host(repo_path)

    @staticmethod
    def _lines(items, cap=30):
        """缩进逐行明细；超过 cap 条截断；空列表返回空串。"""
        if not items:
            return ""
        out = ["  " + str(i) for i in items[:cap]]
        if len(items) > cap:
            out.append("  …其余 {} 个从略".format(len(items) - cap))
        return "\n".join(out)

    @classmethod
    def _with_detail(cls, base, items):
        detail = cls._lines(items)
        return base + "：\n" + detail if detail else base

    def _load(self, include_repo_only=True):
        """读取生效配置与受管清单，返回 (cfg, rules, managed)。

        managed.files: host 绝对路径 -> 仓库相对路径 source/...
        include_repo_only=True 时并入"仓库有、本机无"的命中路径：
        - sync 用它重建本机缺失的文件；
        - commit 用它计算真镜像的移除集合。
        软链接声明路径不属于文件受管集合（由 sync 建立链接，不归档内容）。
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
        self.path_map = cfg.path_map
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
            managed = sync_list.expand(rules, zones, log=self.log,
                                       path_map=self.path_map)
        except sync_list.SyncListError as exc:
            raise SyncError("error-config", str(exc), 2)
        decls = sync_list.symlink_decls(rules)
        if include_repo_only:
            # 并入仓库侧受管路径（本机缺失时用于 sync 重建 / commit 移除）
            for repo_path in gitops.ls_tree(self.wt, "HEAD", "source"):
                host_path = self.path_map.to_host(repo_path)
                if host_path in managed.files:
                    continue
                if host_path in decls:
                    continue  # 已声明为软链接：不按文件写回/移除
                if os.path.exists(host_path):
                    continue
                if sync_list.match_path(rules, host_path) and not sync_list.in_zones(
                        host_path, zones):
                    managed.files[host_path] = repo_path
        return cfg, rules, managed

    def _check_prefixes(self, rules):
        """commit 前置检查：纳入规则的字面前缀必须存在于本机。

        - 显式文件（无通配符）缺失，或目录前缀（如 /etc/xxx/**）整体不存在
          → error-missing，提示先变更 sync.list 或恢复路径再重试；
        - 前缀存在、目录内具体文件被删 → 不报错（真镜像会从仓库移除）。
        """
        missing = []
        for rule in rules:
            if rule.negated or rule.target is not None:
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
                "commit".format(detail), 1)

    # ---------- commit：本机受管文件 → 本地 config ----------

    def _step_commit(self):
        self._check_branch()
        finalize_msg = self._finalize_pending("commit")
        cfg, rules, managed = self._load(include_repo_only=True)
        self._check_prefixes(rules)
        if gitops.head(self.wt) is None:
            raise SyncError("error-state", "工作树无提交")
        decls = sync_list.symlink_decls(rules)

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

        # 镜像移除：命中清单、但本机已不存在的受管路径从仓库移除；
        # 已声明为软链接的路径不再是受管文件，仓库中的旧镜像一并移除。
        removed = [rp for hp, rp in managed.files.items()
                   if not Path(hp).is_file()]
        for repo_path in gitops.ls_tree(self.wt, "HEAD", "source"):
            host_path = self.path_map.to_host(repo_path)
            if host_path in decls and host_path not in changed:
                removed.append(repo_path)
        if not changed and not removed:
            if finalize_msg:
                return ("ok", finalize_msg)
            msg = "无变动可提交"
            if unstable:
                msg += "，跳过 {} 个未稳定文件".format(len(unstable))
            return ("ok", msg)

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
        msg = self._join(
            finalize_msg,
            self._with_detail(
                "归档 {} 个文件，移除 {} 个（已本地提交）".format(
                    len(changed), len(removed)),
                ["+ " + h for h in sorted(changed)] +
                ["- " + self._host_of(rp) for rp in removed]))
        if unstable:
            msg += "，跳过 {} 个未稳定文件".format(len(unstable))
        return ("ok", msg)

    # ---------- push：本地 config → 远端 ----------

    def _step_push(self):
        self._check_branch()
        # 先了结上次 commit 中断残留的镜像改动（按工作树当前内容补提交），
        # 再只推送本地提交；不读清单、不重算主机文件。
        finalize_msg = self._finalize_pending("push")
        self._fetch()
        origin = gitops.ref_sha(self.wt, self._remote_ref())
        if origin is None:
            raise SyncError(
                "error-remote",
                "远端分支 {} 不存在（可能被删除）".format(self.branch))
        local = gitops.head(self.wt)
        if local is None:
            raise SyncError("error-state", "工作树无提交")
        if local == origin:
            return ("ok", self._join(finalize_msg, "无本地提交可推送"))
        if gitops.is_ancestor(self.wt, local, origin):
            raise SyncError(
                "error-conflict",
                "远端 {} 存在本地没有的提交（本地落后），已保留现场；"
                "请先执行 pull（或 pull sync）后重试".format(self.branch))
        if not gitops.is_ancestor(self.wt, origin, local):
            raise SyncError(
                "error-conflict",
                "本地与远端 {} 分叉，已保留现场；请在 {} 中手动处理后重试"
                .format(self.branch, self.wt))
        try:
            gitops.push(self.wt, self.branch)
        except gitops.GitError as exc:
            if gitops.is_auth_error(exc.stderr):
                raise SyncError("error-auth", "推送认证失败: {}".format(exc))
            if gitops.is_nonff_error(exc.stderr):
                raise SyncError(
                    "error-conflict",
                    "推送被拒（推送期间远端又有新提交）：请重新执行；"
                    "若持续失败请在 {} 手动检查".format(self.wt))
            raise SyncError("error-transient", "推送失败: {}".format(exc))
        count = gitops.count_commits(self.wt, "{}..{}".format(origin, local))
        return ("ok", self._join(
            finalize_msg,
            self._with_detail(
                "已推送 {} 个提交".format(count),
                gitops.log_oneline(self.wt, "{}..{}".format(origin, local)))))

    # ---------- sync：本地 config → 本机 ----------

    @staticmethod
    def _symlink(path: Path, target: str, rollback=None):
        """建立软链接；失败（如 Windows 无特权/文件系统不支持）返回异常。

        rollback 非 None 时（修正指向场景）失败先尽力恢复原指向。
        """
        try:
            os.symlink(target, str(path))
            return None
        except OSError as exc:
            if rollback is not None:
                try:
                    os.symlink(rollback, str(path))
                except OSError:
                    pass
            return exc

    def _apply_symlinks(self, decls):
        """按声明在本机建立/修正软链接，返回 (创建数, 明细, 跳过)。

        - 已是指向声明目标的软链接 -> 跳过（一致）；
        - 链接路径被普通文件/目录占用 -> 跳过并告警（不覆盖文件、不删除目录）；
        - 指向其他目标的软链接 -> 备份后重建为声明目标；
        - 目标不存在 -> 允许（悬空链接合法，指向的未来可能存在）；
        - 建链失败（Windows 无特权等）-> 跳过并告警，重建场景尽力恢复原指向。
        """
        created, detail, skipped = 0, [], []
        for link, target in sorted(decls.items()):
            path = Path(link)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                current = os.readlink(str(path))
                if current == target:
                    continue
                self._backup_symlink(path, target)
                path.unlink()
                exc = self._symlink(path, target, rollback=current)
                if exc is not None:
                    skipped.append("重建软链接失败: {} -> {}（{}）".format(
                        link, target, exc))
                    continue
                created += 1
                detail.append("{} -> {}（已修正，原指向 {}）".format(
                    link, target, current))
                continue
            if path.exists():
                skipped.append("软链接路径被占用，跳过: {}（存在非链接文件）"
                               .format(link))
                continue
            exc = self._symlink(path, target)
            if exc is not None:
                skipped.append("建立软链接失败: {} -> {}（{}）".format(
                    link, target, exc))
                continue
            created += 1
            detail.append("{} -> {}".format(link, target))
        return created, detail, skipped

    def _backup_symlink(self, link: Path, target: str):
        """重建前把原链接的指向备份到 state/backups/<run_id>/symlinks.log。"""
        try:
            current = os.readlink(str(link))
        except OSError:
            return
        if current == target:
            return
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        log = self.layout.backups_dir / run_id / "symlinks.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write("{} -> {}（重建为 -> {}）\n".format(link, current, target))

    def _step_sync(self):
        cfg, rules, managed = self._load(include_repo_only=True)
        decls = sync_list.symlink_decls(rules)

        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        applied, skipped, same, written = 0, [], 0, []
        for host_path in sorted(managed.files):
            if host_path in decls:
                continue  # 声明为软链接的路径：不做文件写回
            repo_path = managed.files[host_path]
            blob = gitops.blob_at(self.wt, "HEAD", repo_path)
            if blob is None:
                continue  # 仓库无此文件：sync 不删除本机文件
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
            written.append(host_path)
            applied += 1

        parts = []
        if decls:
            n, detail, sym_skip = self._apply_symlinks(decls)
            base = "建立/修正软链接 {} 个（一致 {}）".format(
                n, len(decls) - n - len(sym_skip))
            sym_msg = self._with_detail(base, detail)
            if sym_skip:
                sym_msg += "\n" + self._with_detail(
                    "软链接跳过 {} 个".format(len(sym_skip)), sym_skip)
            parts.append(sym_msg)
            skipped.extend(sym_skip)
        parts.append(self._with_detail(
            "写回 {} 个文件（已一致 {}）".format(applied, same), written))
        msg = "\n".join(parts)
        if skipped:
            msg += "\n" + self._with_detail(
                "跳过 {} 个".format(len(skipped)), skipped)
        return ("ok", msg)

    # ---------- pull：远端 → 本地 config ----------

    def _step_pull(self):
        self._check_branch()
        mirror, unknown = self._split_dirty()
        if unknown:
            raise SyncError(
                "error-dirty",
                "工作树存在未知改动，无法pull（拉取前工作树须干净），"
                "请先手动处理: {}".format("; ".join(unknown[:5])))
        if mirror:
            raise SyncError(
                "error-dirty",
                "工作树存在上次中断残留的镜像改动，无法pull（快进前须先了结）；"
                "请先执行 push（或 commit）自动补提交: {}".format(
                    "; ".join(mirror[:5])))
        self._fetch()
        origin = gitops.ref_sha(self.wt, self._remote_ref())
        if origin is None:
            raise SyncError(
                "error-remote",
                "远端分支 {} 不存在（可能被删除）".format(self.branch))
        local = gitops.head(self.wt)
        if local is None:
            raise SyncError("error-state", "工作树无提交")
        if local == origin:
            return ("ok", "已是最新")
        if gitops.is_ancestor(self.wt, origin, local):
            return ("ok", "本地领先远端，无需拉取")
        if gitops.is_ancestor(self.wt, local, origin):
            gitops.fast_forward(self.wt, self._remote_ref())
            count = gitops.count_commits(self.wt,
                                         "{}..{}".format(local, origin))
            return ("ok", self._with_detail(
                "已快进 {} 个提交".format(count),
                gitops.log_oneline(self.wt, "{}..{}".format(local, origin))))
        raise SyncError(
            "error-conflict",
            "本地与远端 {} 分叉，已保留现场；请在 {} 中手动处理后重试"
            .format(self.branch, self.wt))

    # ---------- 组合 ----------

    def run(self, steps):
        """按顺序执行原子步骤；全程持有独占锁，返回 (status, message)。"""
        with self._lock():
            messages = []
            for step in steps:
                status, msg = getattr(self, self._STEPS[step])()
                messages.append(msg)
            return "ok", "\n".join(messages)

    def commit(self):
        return self.run(["commit"])

    def push(self):
        return self.run(["push"])

    def sync(self):
        return self.run(["sync"])

    def pull(self):
        return self.run(["pull"])

    # ---------- clear：丢弃本地仓库未提交改动 ----------

    def clear(self):
        """丢弃工作树全部未提交改动（reset --hard + clean -fd，显式例外）。"""
        with self._lock():
            return self._step_clear()

    def _step_clear(self):
        self._check_branch()
        if gitops.head(self.wt) is None:
            raise SyncError("error-state", "工作树无提交")
        dirty = gitops.status_porcelain(self.wt)
        if not dirty:
            return ("ok", "工作树已干净，无改动可丢弃")
        gitops.discard_all(self.wt)
        left = gitops.status_porcelain(self.wt)
        if left:
            raise SyncError(
                "error-dirty",
                "部分改动无法丢弃（如嵌套 git 仓库），请手动处理: {}".format(
                    "; ".join(left[:5])))
        return ("ok", self._with_detail(
            "已丢弃 {} 处未提交改动，工作树已与 HEAD 一致".format(len(dirty)),
            dirty))
