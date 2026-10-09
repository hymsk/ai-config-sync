"""全部 Git 操作的唯一封装。

不变量：不 rebase、不 force push、不 reset、不 clean；
每条命令强制 core.autocrlf=false，保证字节级保真（Hash 对比前提）。
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


def run_git(args, cwd=None, identity=None, timeout=GIT_TIMEOUT, check=True,
            stdin_bytes=None):
    cmd = _base_cmd(identity) + [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=stdin_bytes,
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


def unmerged_paths(wt):
    _, out, _ = run_git(["ls-files", "-u"], cwd=wt)
    paths = []
    for line in out.decode("utf-8", "replace").splitlines():
        if "\t" in line:
            p = line.split("\t", 1)[1]
            if p not in paths:
                paths.append(p)
    return paths


def merge_in_progress(wt):
    return ref_sha(wt, "MERGE_HEAD") is not None


def add(wt, paths):
    if not paths:
        return
    run_git(["add", "--"] + [str(p) for p in paths], cwd=wt)


def staged_exists(wt) -> bool:
    rc, _, _ = run_git(["diff", "--cached", "--quiet"], cwd=wt, check=False)
    return rc == 1


def commit(wt, message, identity):
    run_git(["commit", "-m", message], cwd=wt, identity=identity)


def fetch(wt):
    run_git(["fetch", "--prune", "origin"], cwd=wt)


def merge(wt, ref, allow_unrelated=False):
    args = ["merge", ref, "--no-edit"]
    if allow_unrelated:
        args.append("--allow-unrelated-histories")
    rc, _, _ = run_git(args, cwd=wt, check=False)
    return rc


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


def diff_names(wt, a, b):
    _, out, _ = run_git(["diff", "--name-only", a, b], cwd=wt)
    return [l for l in out.decode("utf-8", "replace").splitlines() if l.strip()]


def ls_tree(wt, revision, prefix):
    """列出某提交下 prefix 目录中的文件（递归）。"""
    _, out, _ = run_git(
        ["ls-tree", "-r", "--name-only", revision, "--", prefix], cwd=wt)
    return [l for l in out.decode("utf-8", "replace").splitlines() if l.strip()]


def hash_object(wt, data: bytes) -> str:
    _, out, _ = run_git(["hash-object", "-w", "--stdin"], cwd=wt,
                        stdin_bytes=data)
    return out.decode().strip()


def _run_env(args, wt, env, identity=None):
    cmd = ["git", "-C", str(wt)] + _base_cmd(identity)[1:] + args
    proc = subprocess.run(cmd, capture_output=True, env=env, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise GitError(
            "git 失败: {} | {}".format(" ".join(args[:3]),
                                       proc.stderr.decode("utf-8", "replace")),
            proc.stderr.decode("utf-8", "replace"))
    return proc.returncode, proc.stdout, proc.stderr


def write_temp_tree(wt, entries, tmp_index: str) -> str:
    """用临时 index 构建 tree，返回 tree sha；不触碰工作树与真实 index。

    entries: [(repo_path, blob_sha), ...]
    """
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = tmp_index
    if os.path.exists(tmp_index):
        os.unlink(tmp_index)
    try:
        for path, sha in entries:
            _run_env(["update-index", "--add", "--cacheinfo",
                      "100644," + sha + "," + path], wt, env)
        _, out, _ = _run_env(["write-tree"], wt, env)
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)
    return out.decode().strip()


def commit_tree(wt, tree, message, identity) -> str:
    _, out, _ = run_git(["commit-tree", tree, "-m", message], cwd=wt,
                        identity=identity)
    return out.decode().strip()


def is_auth_error(stderr: str) -> bool:
    # 注意不用裸 "permission denied"：本地文件系统权限错误也会出现该字样，
    # 只有括注密钥类型的远端拒绝才视为认证失败。
    text = (stderr or "").lower()
    return any(k in text for k in (
        "authentication", "permission denied (publickey", "403",
        "could not read username", "publickey", "access denied",
        "http 401", "fatal: could not read",
    ))
