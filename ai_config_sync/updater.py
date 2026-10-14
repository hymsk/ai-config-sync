"""自更新：比较远端 tag 与本地版本，快进更新安装仓库。

机制：
- 仅查 tag（vX.Y.Z），不比较分支——发布以 tag 为准；
- 更新 = 安装仓库（editable 检出的 git 仓库）fetch --tags + merge --ff-only；
- 无 merge、无 force、无 reset；tag 存在但本地无法快进（历史被改写或
  分叉）→ 报错交人工，绝不自动破坏现场；
- 非 git 检出安装（wheel/PyPI 等）无法自更新，返回 None 交由调用方决定。
"""

import os
import re
import subprocess

from . import __version__, gitops

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """更新失败（携带用户可读信息）。"""


def _parse_version(text):
    m = TAG_RE.match(text.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def current_version():
    ver = _parse_version("v" + __version__)
    if ver is None:
        raise UpdateError("本地版本号无法解析: {}".format(__version__))
    return ver


def latest_tag(install_dir, timeout=5):
    """查询远端最高的 vX.Y.Z tag；仓库不可用/无 tag/超时 → None。"""
    url = _remote_url(install_dir)
    if not url:
        return None
    try:
        _, out, _ = gitops.run_git(
            ["ls-remote", "--tags", "--refs", url], timeout=timeout)
    except gitops.GitError:
        return None
    tags = []
    for line in out.decode("utf-8", "replace").splitlines():
        # <sha>\trefs/tags/vX.Y.Z
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ver = _parse_version(parts[1].replace("refs/tags/", ""))
        if ver:
            tags.append((ver, parts[1].replace("refs/tags/", "")))
    return tags[-1][1] if tags else None


def _remote_url(install_dir):
    """安装目录的 origin URL；不可用返回 None。"""
    if not install_dir or not os.path.isdir(os.path.join(install_dir, ".git")):
        return None
    try:
        _, out, _ = gitops.run_git(["config", "--get", "remote.origin.url"],
                                   cwd=install_dir)
    except gitops.GitError:
        return None
    url = out.decode().strip()
    return url or None


def check_and_update(install_dir, log=None, timeout=5):
    """有更新则快进安装仓库到最新 tag。

    返回:
      (False, None)     无更新 / 无法检查（非 git 安装、无 tag、网络失败）
      (True, tag)       已更新到 tag
    抛 UpdateError:     本地落后但无法快进（历史分叉/被改写），需人工处理
    """
    log = log or (lambda msg: None)
    if not install_dir or not os.path.isdir(os.path.join(install_dir, ".git")):
        return False, None  # 非 git 检出安装，无法自更新
    tag = latest_tag(install_dir, timeout=timeout)
    if not tag:
        return False, None
    remote_ver = _parse_version(tag)
    local_ver = current_version()
    if remote_ver <= local_ver:
        return False, None

    log("发现新版本 {}（当前 v{}），开始更新".format(tag, __version__))
    try:
        gitops.run_git(["fetch", "--tags", "--prune", "origin"],
                       cwd=install_dir, timeout=timeout)
        local = gitops.head(install_dir)
        tag_sha = gitops.ref_sha(install_dir, tag)
        if tag_sha is None:
            return False, None  # fetch 到的 tag 集里没有它（异常，静默放弃）
        if local is not None and tag_sha != local and not gitops.is_ancestor(
                install_dir, local, tag_sha):
            raise UpdateError(
                "安装目录无法快进到 {}（本地历史分叉或被改写）；请在 {} "
                "中手动处理".format(tag, install_dir))
        if local != tag_sha:
            gitops.fast_forward(install_dir, tag)
            log("已更新到 {}".format(tag))
            return True, tag
        # HEAD 已在 tag 上但版本号更旧（忘改 __init__.py 之类），不重装
        return False, None
    except gitops.GitError as exc:
        # 网络等失败：交由调用方（手动 update 命令）报告
        raise UpdateError("更新失败: {}".format(exc))
