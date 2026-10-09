"""install new / install attach。

- 仅接受 Git URL；URL 内嵌凭据直接拒绝；
- bootstrap 或工作树已存在时一律拒绝（重新绑定=手动清理后重装）；
- new：仓库必须为空；--force 时可在非空仓库的孤儿分支播种（纯新增 ref）；
- attach：远端分支必须存在且 .sync-config/config.toml 可解析。
"""

import re
from pathlib import Path

from . import gitops, seed
from .config import Bootstrap, ConfigError, Layout, parse_repo_config, \
    save_bootstrap

SEED_IDENTITY = ("sync-bot", "sync-bot@invalid")


class InstallError(Exception):
    pass


def _refuse_url_credentials(url: str):
    if "://" in url:
        authority = url.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority:
            raise InstallError(
                "repo_url 含内嵌凭据；请使用 SSH key 或 credential helper")


def _check_fresh(layout: Layout, worktree: Path):
    if layout.bootstrap.exists():
        raise InstallError(
            "bootstrap 已存在: {}；重新绑定请先手动清理".format(layout.bootstrap))
    if worktree.exists() and any(worktree.iterdir()):
        raise InstallError("工作树目录非空: {}".format(worktree))


def _detect_install_dir() -> str:
    p = Path(__file__).resolve().parent
    for cand in (p.parent, p.parent.parent):
        if (cand / ".git").exists() and (cand / "ai_config_sync").exists():
            return str(cand)
    return ""


def _seed(worktree: Path):
    sync_cfg = worktree / ".sync-config"
    sync_cfg.mkdir(parents=True, exist_ok=True)
    (sync_cfg / "config.toml").write_text(seed.CONFIG_TOML, encoding="utf-8")
    (sync_cfg / "sync.list").write_text(seed.SYNC_LIST, encoding="utf-8")
    (worktree / ".gitattributes").write_text(seed.GITATTRIBUTES,
                                             encoding="utf-8")
    (worktree / "README.md").write_text(seed.README, encoding="utf-8")
    (worktree / "source").mkdir(exist_ok=True)


def install_new(layout: Layout, repo_url: str, branch=None, worktree=None,
                force=False, log=print):
    _refuse_url_credentials(repo_url)
    worktree = Path(worktree) if worktree else layout.default_worktree
    _check_fresh(layout, worktree)

    heads, default_branch = gitops.ls_remote(repo_url)
    if heads and not force:
        raise InstallError(
            "仓库非空（已有分支: {}）；接入请使用 install attach，"
            "或在非空仓库播种新分支使用 --force".format(", ".join(sorted(heads))))
    if heads and force:
        if not branch:
            raise InstallError(
                "非空仓库 --force 需要显式 -b 指定一个不存在的分支")
        if branch in heads:
            raise InstallError(
                "分支 {} 已存在；请使用 install attach".format(branch))

    worktree.parent.mkdir(parents=True, exist_ok=True)
    gitops.clone(repo_url, worktree)
    if heads:
        gitops.orphan_branch(worktree, branch)
    else:
        current = gitops.current_branch(worktree)
        if not branch:
            branch = current or default_branch or "main"
        if current and current != branch:
            gitops.set_unborn_head(worktree, branch)

    _seed(worktree)
    gitops.add(worktree, [".sync-config/config.toml", ".sync-config/sync.list",
                          ".gitattributes", "README.md"])
    gitops.commit(
        worktree,
        "chore: initialize config repo from ai-config-sync examples",
        SEED_IDENTITY)
    gitops.push(worktree, branch, set_upstream=True)
    save_bootstrap(layout, Bootstrap(repo_url, branch, worktree,
                                     install_dir=_detect_install_dir()))
    log("install new 完成: branch={} worktree={}".format(branch, worktree))
    return branch


def install_attach(layout: Layout, repo_url: str, branch=None, worktree=None,
                   log=print):
    _refuse_url_credentials(repo_url)
    worktree = Path(worktree) if worktree else layout.default_worktree
    _check_fresh(layout, worktree)

    heads, default_branch = gitops.ls_remote(repo_url)
    if not heads:
        raise InstallError("仓库为空；请使用 install new")
    if branch:
        if branch not in heads:
            raise InstallError(
                "远端不存在分支 {}；可用分支: {}".format(
                    branch, ", ".join(sorted(heads))))
    else:
        if not default_branch or default_branch not in heads:
            raise InstallError("无法确定远端默认分支；请显式指定 -b")
        branch = default_branch

    gitops.clone(repo_url, worktree, branch=branch)
    cfg = worktree / ".sync-config" / "config.toml"
    if not cfg.is_file():
        raise InstallError("目标仓库缺少 .sync-config/config.toml；不是配置仓库")
    try:
        parse_repo_config(cfg.read_text(encoding="utf-8"))
    except ConfigError as exc:
        raise InstallError("配置校验失败: {}".format(exc))
    save_bootstrap(layout, Bootstrap(repo_url, branch, worktree,
                                     install_dir=_detect_install_dir()))
    log("install attach 完成: branch={} worktree={}".format(branch, worktree))
    return branch
