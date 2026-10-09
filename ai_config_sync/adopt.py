"""首轮采用：差异预览与 --prefer local / repo / merge。

--prefer 只作用于"双侧都有且内容不同"的文件；
单侧文件（仅本机/仅仓库）在任何 prefer 下都向双侧收敛。
"""

import os
import socket
import time
from pathlib import Path

from . import applier, collector, gitops
from .agent import Agent


def _tree_ref(agent: Agent) -> str:
    """预览用的参照：优先远端跟踪 ref（fetch 后可见），否则 HEAD。"""
    ref = agent._remote_ref()
    return ref if gitops.ref_sha(agent.wt, ref) is not None else "HEAD"


def _fetch_quiet(agent: Agent):
    try:
        gitops.fetch(agent.wt)
        agent._last_fetch = time.time()
    except gitops.GitError:
        pass  # 离线时按本地 HEAD 预览


def _settle(agent: Agent):
    """fetch 并合并远端（仅快进/干净合并）；冲突时按冲突流程处理。"""
    try:
        gitops.fetch(agent.wt)
        agent._last_fetch = time.time()
    except gitops.GitError as exc:
        return ("error-transient", "网络失败: {}".format(exc))
    origin = gitops.ref_sha(agent.wt, agent._remote_ref())
    if origin is not None and origin != gitops.head(agent.wt):
        rc = gitops.merge(agent.wt, agent._remote_ref())
        if rc != 0:
            cfg, rules, managed = agent._load()
            return agent._conflict(gitops.head(agent.wt), origin,
                                   "adopt-settle", managed)
    return None


def preview(agent: Agent):
    """返回 [(status, host_path), ...]；status ∈ differs/host-only/
    repo-only/unstable/same。只读：fetch 更新跟踪 ref，不产生本地提交。"""
    _fetch_quiet(agent)
    cfg, rules, managed = agent._load(tree_ref=_tree_ref(agent))
    rows = []
    for host_path in sorted(managed.files):
        host = Path(host_path)
        repo_path = managed.files[host_path]
        blob = gitops.blob_at(agent.wt, _tree_ref(agent), repo_path)
        if not host.exists():
            if blob is not None:
                rows.append(("repo-only", host_path))
            continue
        got = collector.read_stable(host)
        if got is None:
            rows.append(("unstable", host_path))
            continue
        _, digest = got
        if blob is None:
            rows.append(("host-only", host_path))
        elif collector.sha256_bytes(blob) != digest:
            rows.append(("differs", host_path))
        else:
            rows.append(("same", host_path))
    return rows


def print_preview(rows):
    order = {"differs": 0, "host-only": 1, "repo-only": 2, "unstable": 3,
             "same": 4}
    rows = sorted(rows, key=lambda r: (order.get(r[0], 9), r[1]))
    counts = {}
    for status, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    for status, path in rows:
        print("{:<12} {}".format(status, path))
    print("-- " + " ".join("{}={}".format(k, v) for k, v in sorted(counts.items())))


def prefer_local(agent: Agent):
    """本机为准：先整合远端（仅快进/干净合并），再正常轮收集推送。

    先整合是为了保证随后收集的本机提交直接覆盖远端版本，
    而不是与远端形成 add/add 冲突；若与本机历史真实分叉，
    则按普通冲突流程暂停。
    """
    settled = _settle(agent)
    if settled is not None:
        return settled
    return agent.run_once()


def prefer_repo(agent: Agent):
    """仓库为准：先整合远端，再写回本机（备份后原子替换），
    最后跑正常轮收敛单侧文件。"""
    settled = _settle(agent)
    if settled is not None:
        return settled
    cfg, rules, managed = agent._load()
    applied = 0
    for host_path in sorted(managed.files):
        host = Path(host_path)
        repo_path = managed.files[host_path]
        blob = gitops.blob_at(agent.wt, "HEAD", repo_path)
        if blob is None:
            continue
        if host.exists():
            got = collector.read_stable(host)
            if got is None:
                continue
            if collector.sha256_bytes(got[0]) == collector.sha256_bytes(blob):
                continue
        applier.validate(host, blob)
        applier.backup(host, agent.layout.backups_dir, "adopt-repo", repo_path)
        applier.prune_backups(agent.layout.backups_dir, repo_path,
                              cfg.max_backups_per_file)
        applier.atomic_write(host, blob)
        applied += 1
    agent.log("prefer-repo 写回 {} 个文件".format(applied))
    return agent.run_once()


def prefer_merge(agent: Agent):
    """三方裁决：把本机受管文件做成无父提交，与 HEAD 做无历史关联合并。

    内容不同的双侧文件会产生 add/add 冲突（ours=仓库版 theirs=本机版），
    置标记后逐文件人工解决；单侧文件自动合并。
    """
    settled = _settle(agent)
    if settled is not None:
        return settled
    cfg, rules, managed = agent._load()
    entries = []
    reverse = {}
    for host_path in sorted(managed.files):
        host = Path(host_path)
        if not host.exists():
            continue
        repo_path = managed.files[host_path]
        data = host.read_bytes()
        sha = gitops.hash_object(agent.wt, data)
        entries.append((repo_path, sha))
        reverse[repo_path] = host_path
    if not entries:
        return agent.run_once()

    tmp_index = str(agent.layout.state_dir / "tmp-index")
    try:
        tree = gitops.write_temp_tree(agent.wt, entries, tmp_index)
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)
    host_label = socket.gethostname().split(".")[0]
    snapshot = gitops.commit_tree(
        agent.wt, tree,
        "sync: host snapshot for adoption merge (host: {})".format(host_label),
        (cfg.git_name, cfg.git_email))
    rc = gitops.merge(agent.wt, snapshot, allow_unrelated=True)
    if rc != 0:
        stale = {}
        for repo_path, _ in entries:
            hp = reverse.get(repo_path)
            if hp and Path(hp).exists():
                stale[repo_path] = collector.sha256_bytes(
                    Path(hp).read_bytes())
        agent.journal.append("conflict", run_id="adopt-merge", files=stale,
                             note="adoption add/add")
        agent._flag("首轮 merge 采用冲突；解决后删除标记")
        return ("paused-conflict",
                "add/add 冲突待人工解决（ours=仓库版 theirs=本机版）")
    try:
        gitops.push(agent.wt, agent.branch)
    except gitops.GitError as exc:
        if gitops.is_auth_error(exc.stderr):
            return ("error-auth", "推送认证失败: {}".format(exc))
        # 合并提交已在本地 HEAD，未推送部分交由下一轮正常事务重试推送
        return ("error-transient", "推送失败（下一轮重试）: {}".format(exc))
    return agent.run_once()
