"""全部 Git 操作的唯一封装。

不变量：不 rebase、不 force push、不 reset、不 clean；冲突保留现场；
每条命令强制 core.autocrlf=false，保证字节级保真（哈希对比前提）。
"""

import os
import re
import subprocess
from pathlib import Path

GIT_TIMEOUT = 120

# 受管文件的双侧修改一律强制冲突（即使行级可自动融合）：
# attributes 声明 merge=aics-conflict，driver 定义为 false（永远失败）。
# 单侧修改/不同文件不受 driver 影响，仍由 git 自动收敛。
MERGE_DRIVER = "merge.aics-conflict.driver"
MERGE_DRIVER_ATTRS = (
    "source/** merge=aics-conflict\n"
    ".sync-config/** merge=aics-conflict\n"
)


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


def merge(wt, ref):
    # 命令行注入 driver 定义：同一文件双侧修改强制冲突，保留现场
    rc, _, _ = run_git(
        ["-c", MERGE_DRIVER + "=false", "merge", ref, "--no-edit"],
        cwd=wt, check=False)
    return rc


def ensure_conflict_driver(wt):
    """幂等写入强制冲突 driver 的本地兜底定义。

    - .git/info/attributes：优先级高于仓库 .gitattributes，使存量配置仓库
      （种子尚未更新）也立即生效；
    - 本地 config 定义 driver：没有定义时 git 会静默回退到行级自动合并，
      该定义保证手工 git merge 同样受保护。
    本工具的 merge 始终通过 -c 注入定义，不依赖这里的状态。
    """
    info = Path(wt) / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    attrs = info / "attributes"
    text = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
    missing = [l for l in MERGE_DRIVER_ATTRS.splitlines(True)
               if l.strip() and l not in text]
    if missing:
        with open(attrs, "a", encoding="utf-8") as f:
            f.write("".join(missing))
    run_git(["config", MERGE_DRIVER, "false"], cwd=wt)


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
