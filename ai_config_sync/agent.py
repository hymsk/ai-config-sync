"""单轮同步事务 P0-P7、崩溃恢复与主循环。

机制要点（以文件为准，无 per-file 状态）：
- A = 轮开始时 HEAD（上一轮一致状态）；L = A + 本机收集提交；M = merge 后 HEAD；
- 所有判定都是"主机文件 Hash <-> Git blob Hash"即时对比；
- 唯一本机状态是追加式 journal，崩溃后按最后一条事务记录恢复。
"""

import fcntl
import os
import socket
import time
from pathlib import Path

from . import applier, collector, gitops, sync_list
from .config import Bootstrap, ConfigError, Layout, parse_repo_config
from .state import Journal


class SyncAbort(Exception):
    """携带状态码的中止。"""

    def __init__(self, status, message=""):
        super().__init__(message or status)
        self.status = status
        self.message = message or status


class _StopAfter(Exception):
    """测试钩子：AICS_STOP_AFTER=<phase> 时在该阶段后中止。"""

    def __init__(self, phase):
        Exception.__init__(self, phase)
        self.phase = phase


class Agent:
    def __init__(self, boot: Bootstrap, layout: Layout, log=None):
        self.boot = boot
        self.layout = layout
        self.wt = Path(boot.worktree)
        self.branch = boot.branch
        self.identity = ("sync-bot", "sync-bot@invalid")
        self.journal = Journal(layout.journal_path)
        self.log = log or (lambda msg: None)
        self._last_fetch = 0.0
        self._interval = 5.0
        layout.state_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------

    def _lock(self):
        path = self.layout.lock_path

        class _L:
            def __init__(self, p):
                self.path = p
                self.fd = None

            def __enter__(self):
                self.fd = open(self.path, "a+")
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def __exit__(self, *args):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()

        return _L(path)

    def _flag(self, reason=""):
        self.layout.flag.parent.mkdir(parents=True, exist_ok=True)
        if not self.layout.flag.exists():
            self.layout.flag.write_text("", encoding="utf-8")
        self.log("冲突暂停: " + reason)

    def _remote_ref(self):
        return "origin/" + self.branch

    def _host_of(self, managed, repo_path):
        for host_path, rp in managed.files.items():
            if rp == repo_path:
                return Path(host_path)
        return None

    def _load(self, tree_ref="HEAD"):
        """读取生效配置与受管清单。失败抛 SyncAbort(paused-invalid-config)。

        tree_ref 指定仓库侧路径的来源提交（预览时可用远端跟踪 ref）。
        """
        cfg_path = self.wt / ".sync-config" / "config.toml"
        list_path = self.wt / ".sync-config" / "sync.list"
        try:
            cfg = parse_repo_config(cfg_path.read_text(encoding="utf-8"))
        except OSError:
            raise SyncAbort("paused-invalid-config",
                            "缺少 .sync-config/config.toml")
        except ConfigError as exc:
            raise SyncAbort("paused-invalid-config", str(exc))
        self.identity = (cfg.git_name, cfg.git_email)
        self._interval = cfg.interval
        try:
            list_text = list_path.read_text(encoding="utf-8")
        except OSError:
            raise SyncAbort("paused-invalid-config", "缺少 .sync-config/sync.list")
        try:
            rules = sync_list.parse(list_text)
        except sync_list.SyncListError as exc:
            raise SyncAbort("paused-invalid-config", str(exc))
        zones = [str(z) for z in
                 self.layout.hard_zones(self.wt, self.boot.install_dir)]
        try:
            managed = sync_list.expand(rules, zones, log=self.log)
        except sync_list.SyncListError as exc:
            raise SyncAbort("paused-invalid-config", str(exc))
        # 并入仓库侧受管路径（本机缺失时也要可重建/预览 repo-only）
        for repo_path in gitops.ls_tree(self.wt, tree_ref, "source"):
            host_path = "/" + repo_path[len("source/"):]
            if host_path in managed.files:
                continue
            if os.path.exists(host_path):
                continue  # 本机存在但被展开排除（如软链接）→ 只合并不写回
            if sync_list._match(rules, host_path) and not sync_list._in_zones(
                    host_path, zones):
                managed.files[host_path] = repo_path
        return cfg, rules, managed

    def _maybe_stop(self, phase):
        if os.environ.get("AICS_STOP_AFTER") == phase:
            raise _StopAfter(phase)

    # ---------- 入口 ----------

    def run_once(self):
        """执行（或恢复）一轮事务，返回 (status, message)。"""
        with self._lock():
            flag_exists = self.layout.flag.exists()
            resumable = self.journal.resumable()
            if resumable == "corrupt":
                return ("paused-needs-doctor", "journal 损坏")
            if flag_exists:
                return ("paused-conflict", "ERROR_MERGE_FLAG 存在")
            if resumable is not None:
                try:
                    return self._resume(resumable)
                except SyncAbort as exc:
                    return (exc.status, exc.message)
                except _StopAfter as stop:
                    return ("stopped-" + stop.phase, "")
            raw_last = self.journal.last()
            if isinstance(raw_last, dict) and raw_last.get("phase") == "conflict":
                return self._post_conflict(raw_last)
            try:
                return self._round()
            except SyncAbort as exc:
                return (exc.status, exc.message)
            except _StopAfter as stop:
                return ("stopped-" + stop.phase, "")

    def run(self):
        """常驻主循环。"""
        backoff = 0
        while True:
            status, message = self.run_once()
            self.log("round -> {} {}".format(status, message))
            if status == "error-transient":
                backoff = min(backoff + 1, 6)
            elif status != "idle":
                backoff = 0
            time.sleep(self._interval * (2 ** backoff))

    # ---------- 全新一轮 ----------

    def _round(self):
        # P0 预检
        if gitops.merge_in_progress(self.wt) or gitops.unmerged_paths(self.wt):
            self._flag("检测到未完成合并")
            self.journal.append("conflict", files={}, note="unresolved-merge")
            return ("paused-conflict", "存在未完成合并")
        dirty = gitops.status_porcelain(self.wt)
        if dirty:
            return ("paused-needs-doctor",
                    "工作树存在未知改动: " + "; ".join(dirty[:5]))

        # P1 配置与清单
        cfg, rules, managed = self._load()
        a_sha = gitops.head(self.wt)
        if a_sha is None:
            return ("paused-needs-doctor", "工作树无提交")
        origin = gitops.ref_sha(self.wt, self._remote_ref())

        # P2 变更计算（本机 vs A）
        changed = {}
        waiting = []
        missing = [p for p in managed.files if not Path(p).exists()]
        for host_path in sorted(managed.files):
            host = Path(host_path)
            if not host.exists():
                continue  # 缺失文件由 P6 依仓库内容重建
            got = collector.read_stable(host)
            if got is None:
                waiting.append(host_path)
                continue
            data, digest = got
            blob = gitops.blob_at(self.wt, "HEAD", managed.files[host_path])
            if blob is None or collector.sha256_bytes(blob) != digest:
                changed[host_path] = (managed.files[host_path], data)
        if (not changed and not missing and origin == a_sha and waiting == []
                and (time.time() - self._last_fetch) < cfg.remote_check_interval):
            return ("idle", "")

        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.journal.append("round-begin", run_id=run_id, anchor=a_sha)

        # P3 收集提交（不联网）
        l_sha = a_sha
        if changed:
            for host_path, (repo_path, data) in changed.items():
                dest = self.wt / repo_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            gitops.add(self.wt, [changed[h][0] for h in changed])
            if gitops.staged_exists(self.wt):
                message = "sync: capture {} files (host: {})".format(
                    len(changed), socket.gethostname().split(".")[0])
                gitops.commit(self.wt, message, self.identity)
                l_sha = gitops.head(self.wt)
        self.journal.append("captured", run_id=run_id, anchor=a_sha, commit=l_sha)
        self._maybe_stop("captured")

        return self._integrate(a_sha, l_sha, run_id, cfg, managed)

    # ---------- P4/P5 ----------

    def _integrate(self, a_sha, l_sha, run_id, cfg, managed):
        try:
            gitops.fetch(self.wt)
            self._last_fetch = time.time()
        except gitops.GitError as exc:
            if gitops.is_auth_error(exc.stderr):
                return ("error-auth", "凭据/权限失败: {}".format(exc))
            return ("error-transient", "网络失败: {}".format(exc))

        origin = gitops.ref_sha(self.wt, self._remote_ref())
        if origin is None:
            return ("paused-needs-doctor",
                    "远端分支 {} 不存在（可能被删除）；确认后手动 push 或重新安装"
                    .format(self.branch))

        m_sha = l_sha
        if origin != l_sha:
            rc = gitops.merge(self.wt, self._remote_ref())
            if rc != 0:
                return self._conflict(a_sha, origin, run_id, managed)
            m_sha = gitops.head(self.wt)
        self.journal.append("merged", run_id=run_id, anchor=a_sha, l=l_sha,
                            commit=m_sha)
        self._maybe_stop("merged")

        for _ in range(3):
            try:
                gitops.push(self.wt, self.branch)
                break
            except gitops.GitError as exc:
                if gitops.is_auth_error(exc.stderr):
                    return ("error-auth", "推送认证失败: {}".format(exc))
                try:
                    gitops.fetch(self.wt)
                except gitops.GitError as exc2:
                    return ("error-transient", "网络失败: {}".format(exc2))
                origin2 = gitops.ref_sha(self.wt, self._remote_ref())
                if origin2 is None:
                    return ("paused-needs-doctor", "远端分支消失")
                if origin2 != gitops.head(self.wt):
                    rc = gitops.merge(self.wt, self._remote_ref())
                    if rc != 0:
                        return self._conflict(a_sha, origin2, run_id, managed)
        else:
            return ("error-transient", "推送重试耗尽")

        self.journal.append("pushed", run_id=run_id, anchor=a_sha, l=l_sha,
                            commit=gitops.head(self.wt))
        self._maybe_stop("pushed")
        return self._apply(a_sha, l_sha, run_id, cfg, managed)

    def _conflict(self, a_sha, origin_sha, run_id, managed):
        """保留 Git 冲突现场，置标记，记录冲突时主机哈希用于恢复。"""
        unmerged = gitops.unmerged_paths(self.wt)
        remote_changed = (gitops.diff_names(self.wt, a_sha, origin_sha)
                          if a_sha else [])
        stale = {}
        for repo_path in set(remote_changed + unmerged):
            host = self._host_of(managed, repo_path)
            if host is not None and host.exists():
                try:
                    stale[repo_path] = collector.sha256_bytes(
                        host.read_bytes())
                except OSError:
                    pass
        self.journal.append("conflict", run_id=run_id, files=stale)
        self._flag("merge 冲突: " + ", ".join(unmerged[:5]))
        return ("paused-conflict", "合并冲突，已保留现场并置标记")

    # ---------- P6/P7 ----------

    def _apply(self, a_sha, l_sha, run_id, cfg, managed):
        m_sha = gitops.head(self.wt)
        reverse = {rp: hp for hp, rp in managed.files.items()}
        diff = gitops.diff_names(self.wt, a_sha, m_sha) if a_sha else []
        todo = set(diff)
        # 本机缺失但仓库有的受管路径也纳入（重建策略）
        for host_path, repo_path in managed.files.items():
            if not Path(host_path).exists():
                todo.add(repo_path)
        applied, waiting, skipped = [], [], []
        for repo_path in sorted(todo):
            host = reverse.get(repo_path)
            if host is None:
                continue  # 不在当前清单：只合并不写回
            host = Path(host)
            m_blob = gitops.blob_at(self.wt, m_sha, repo_path)
            if m_blob is None:
                if host.exists():
                    self.log("远端删除不传播，本机保留: " + repo_path)
                continue
            a_blob = gitops.blob_at(self.wt, a_sha, repo_path) if a_sha else None
            l_blob = gitops.blob_at(self.wt, l_sha, repo_path)
            if host.exists():
                try:
                    h_hash = collector.sha256_bytes(host.read_bytes())
                except OSError:
                    waiting.append(str(host))
                    continue
                m_hash = collector.sha256_bytes(m_blob)
                if h_hash == m_hash:
                    continue
                if a_blob is not None and h_hash == collector.sha256_bytes(a_blob):
                    pass  # 本机仍在基线 → 安全写回
                elif l_blob is not None and h_hash == collector.sha256_bytes(l_blob):
                    continue  # 本机提交内容已被合并保留
                else:
                    waiting.append(str(host))  # 轮中又被修改 → 不覆盖
                    continue
            try:
                applier.validate(host, m_blob)
            except applier.ApplyError as exc:
                skipped.append(str(exc))
                continue
            applier.backup(host, self.layout.backups_dir, run_id, repo_path)
            applier.atomic_write(host, m_blob)
            applied.append(repo_path)
        self.journal.append("applied", run_id=run_id)
        self.journal.append("checkpointed", run_id=run_id)
        self.journal.rotate()
        status = "ok" if not (waiting or skipped) else "waiting-local-stable"
        return (status, "applied={} waiting={} skipped={}".format(
            len(applied), len(waiting), len(skipped)))

    # ---------- 恢复 ----------

    def _resume(self, last):
        phase = last.get("phase")
        if phase == "applied":
            self.journal.append("checkpointed", run_id=last.get("run_id"))
            self.journal.rotate()
            return ("ok", "恢复：完成检查点")
        a_sha = last.get("anchor")
        l_sha = last.get("l") or last.get("commit")
        cfg, rules, managed = self._load()
        return self._integrate(a_sha, l_sha, last.get("run_id"), cfg, managed)

    def _post_conflict(self, entry):
        """标记已删除：复核冲突已解决，然后按记录的主机哈希写回解决结果。"""
        if gitops.merge_in_progress(self.wt) or gitops.unmerged_paths(self.wt):
            self._flag("标记被删除但合并未完成")
            self.journal.append("conflict", files=entry.get("files", {}),
                                note="re-flag")
            return ("paused-conflict", "合并未完成，已重新置标记")
        if gitops.status_porcelain(self.wt):
            return ("paused-needs-doctor",
                    "工作树存在未提交内容；完成解决并提交后重试")
        cfg, rules, managed = self._load()
        run_id = str(entry.get("run_id", "post-conflict"))
        applied = 0
        for repo_path, old_hash in (entry.get("files") or {}).items():
            host = self._host_of(managed, repo_path)
            if host is None or not host.exists():
                continue
            head_blob = gitops.blob_at(self.wt, "HEAD", repo_path)
            if head_blob is None:
                continue
            try:
                cur = collector.sha256_bytes(host.read_bytes())
            except OSError:
                continue
            head_hash = collector.sha256_bytes(head_blob)
            if cur == head_hash:
                continue  # 主机已是解决结果
            if cur != old_hash:
                continue  # 暂停期间本机又被编辑 → 留给下一轮收集
            try:
                applier.validate(host, head_blob)
            except applier.ApplyError as exc:
                self.log(str(exc))
                continue
            applier.backup(host, self.layout.backups_dir, run_id, repo_path)
            applier.atomic_write(host, head_blob)
            applied += 1
        self.journal.append("checkpointed", run_id=run_id, note="post-conflict")
        self.journal.rotate()
        return ("ok", "冲突后恢复，写回 {} 个文件（下一轮推送解决提交）".format(applied))
