"""全部 Git 操作的唯一封装。

不变量：不 rebase、不 force push、不 reset、不 clean；不做三方合并，
本地分支只允许 --ff-only 快进对齐远端；冲突保留现场；
每条命令强制 core.autocrlf=false，保证字节级保真（哈希对比前提）。
"""

import os
import re
import subprocess

GIT_TIMEOUT = 120


class GitError(Exception):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def _base_cmd(identity=None):
    cmd = ["git", "-c", "core.autocrlf=false"]
    if identity:
        cmd += ["-c", "user.name=" + identity[0],
                "-c", "user.email=" + identity[1]]
    return cmd


def run_git(args, cwd=None, identity=None, timeout=GIT_TIMEOUT, check=True):
    cmd = _base_cmd(identity) + [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        raise GitError("git 命令超时: " + " ".join(cmd[:5]))
    if check and proc.returncode != 0:
        raise GitError(
            "git 失败({}): {}".format(proc.returncode, " ".join(cmd[:6])),
            proc.stderr.decode("utf-8", "replace"),
        )
    return proc.returncode, proc.stdout, proc.stderr


def ls_remote(url):
    """返回 (heads: {branch: sha}, default_branch or None)。"""
    _, out, _ = run_git(["ls-remote", "--symref", url], timeout=60)
    heads = {}
    default = None
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("ref:"):
            m = re.match(r"ref:\s+(?:refs/heads/(\S+))\s+HEAD", line)
            if m:
                default = m.group(1)
            continue
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1][len("refs/heads/"):]] = parts[0]
    return heads, default


def clone(url, dest, branch=None):
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [url, str(dest)]
    run_git(args)


def head(wt):
    rc, out, _ = run_git(["rev-parse", "HEAD"], cwd=wt, check=False)
    return out.decode().strip() if rc == 0 else None


def ref_sha(wt, ref):
    rc, out, _ = run_git(["rev-parse", "--verify", ref], cwd=wt, check=False)
    return out.decode().strip() if rc == 0 else None


def current_branch(wt):
    rc, out, _ = run_git(["symbolic-ref", "--short", "HEAD"], cwd=wt,
                         check=False)
    return out.decode().strip() if rc == 0 else None


def set_unborn_head(wt, branch):
    run_git(["symbolic-ref", "HEAD", "refs/heads/" + branch], cwd=wt)


def orphan_branch(wt, branch):
    run_git(["switch", "--orphan", branch], cwd=wt)


def status_porcelain(wt):
    _, out, _ = run_git(["status", "--porcelain"], cwd=wt)
    return [l for l in out.decode("utf-8", "replace").splitlines() if l.strip()]


def add(wt, paths):
    if not paths:
        return
    run_git(["add", "--"] + [str(p) for p in paths], cwd=wt)


def staged_exists(wt) -> bool:
    rc, _, _ = run_git(["diff", "--cached", "--quiet"], cwd=wt, check=False)
    return rc == 1


def add_all(wt):
    """暂存工作树全部改动（含未跟踪文件）：commit 命令的显式例外。

    push/pull 只提交显式收集/移除的路径；commit 以本地 git 为准，
    故明确暂存一切（删除与新增一并纳入）。
    """
    run_git(["add", "-A"], cwd=wt)


def staged_paths(wt):
    """已暂存的仓库相对路径（--name-only，NUL 分隔以保真奇怪文件名）。"""
    _, out, _ = run_git(["diff", "--cached", "--name-only", "-z"], cwd=wt)
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def count_commits(wt, rev_range):
    """rev_range（如 a..b）内的提交数。"""
    _, out, _ = run_git(["rev-list", "--count", rev_range], cwd=wt)
    return int(out.decode().strip() or "0")


def commit(wt, message, identity):
    run_git(["commit", "-m", message], cwd=wt, identity=identity)


def fetch(wt):
    run_git(["fetch", "--prune", "origin"], cwd=wt)


def is_ancestor(wt, a, b) -> bool:
    """a 是否为 b 的祖先（b 的历史是否包含 a）。"""
    rc, _, _ = run_git(["merge-base", "--is-ancestor", a, b], cwd=wt,
                       check=False)
    return rc == 0


def fast_forward(wt, ref):
    """把当前分支快进到 ref（--ff-only：无三方合并，无冲突可能）。"""
    run_git(["merge", "--ff-only", ref, "--no-edit"], cwd=wt)


def rm(wt, paths):
    """从索引与工作树移除显式路径（--ignore-unmatch 容忍已不存在的路径）。"""
    if not paths:
        return
    run_git(["rm", "-q", "--ignore-unmatch", "--"] +
            [str(p) for p in paths], cwd=wt)


def push(wt, branch, set_upstream=False):
    args = ["push"]
    if set_upstream:
        args.append("-u")
    args += ["origin", branch + ":" + branch]
    run_git(args, cwd=wt)


def blob_at(wt, revision, path):
    rc, out, _ = run_git(["show", revision + ":" + str(path)], cwd=wt,
                         check=False)
    if rc != 0:
        return None
    return out


def ls_tree(wt, revision, prefix):
    """列出某提交下 prefix 目录中的文件（递归）。"""
    _, out, _ = run_git(
        ["ls-tree", "-r", "--name-only", revision, "--", prefix], cwd=wt)
    return [l for l in out.decode("utf-8", "replace").splitlines() if l.strip()]


def is_auth_error(stderr: str) -> bool:
    # 注意不用裸 "permission denied"：本地文件系统权限错误也会出现该字样，
    # 只有括注密钥类型的远端拒绝才视为认证失败。
    text = (stderr or "").lower()
    return any(k in text for k in (
        "authentication", "permission denied (publickey", "403",
        "could not read username", "publickey", "access denied",
        "http 401", "fatal: could not read",
    ))


def is_nonff_error(stderr: str) -> bool:
    """推送被拒：远端在本地之后又有新提交（non-fast-forward）。"""
    text = (stderr or "").lower()
    return any(k in text for k in (
        "non-fast-forward", "non fast forward", "fetch first", "(rejected)",
        "stale info",
    ))
