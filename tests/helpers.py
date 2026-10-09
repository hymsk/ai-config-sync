"""测试辅助：本地 bare 仓库模拟远端、注入式 Layout 模拟多机。"""

import subprocess
from pathlib import Path

from ai_config_sync.config import Bootstrap, Layout

TEST_IDENTITY = ("test-bot", "test-bot@invalid")


def git(args, cwd=None):
    proc = subprocess.run(
        ["git", "-c", "user.name=" + TEST_IDENTITY[0],
         "-c", "user.email=" + TEST_IDENTITY[1]] + args,
        cwd=str(cwd) if cwd else None, capture_output=True)
    if proc.returncode != 0:
        raise AssertionError(
            "git {} 失败: {}".format(args, proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace")


def make_bare(tmpdir, name="origin.git"):
    path = Path(tmpdir) / name
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    return path


def make_layout(tmpdir, name):
    """独立机器布局：etc/var/flag 全部隔离在 tmpdir/name 下。"""
    base = Path(tmpdir) / name
    return Layout(etc=base / "etc", var=base / "var",
                  flag=base / "home" / "ERROR_MERGE_FLAG")


def boot_for(layout: Layout, remote, branch="main") -> Bootstrap:
    return Bootstrap(repo_url=str(remote), branch=branch,
                     worktree=layout.default_worktree)


def host_file(base, rel, content):
    p = Path(base) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def manual_push(remote, workdir, edits, message):
    """模拟"另一台服务器"：clone、改文件、提交、推送。

    edits: [(repo_path, content), ...]
    """
    git(["clone", str(remote), str(workdir)])
    for repo_path, content in edits:
        p = Path(workdir) / repo_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(["add", "."], cwd=workdir)
    git(["commit", "-m", message], cwd=workdir)
    git(["push", "origin", "main"], cwd=workdir)
