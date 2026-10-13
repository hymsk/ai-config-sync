"""命令行入口：install / commit / push / sync / pull / update。

四个原子操作（各只做一件事）与三个固定组合：
- commit：本机受管文件 → 本地 config（归档 + 本地提交；不联网）
- push：  本地 config → 远端（只推送本地提交）
- sync：  本地 config → 本机（写回受管文件；不联网、永不删主机文件）
- pull：  远端 → 本地 config（fetch + 仅快进；不碰主机文件）
- commit push：本机文件 → 远端（= 归档并推送）
- push sync：  本地 config → 远端 + 本机（以本地 git 为准）
- pull sync：  远端 → 本机（= 拉取并写回）

操作按执行顺序书写；只接受上述固定组合，拒绝 `push commit` 等异常语义。
同步操作执行前自动做一次 update 检查（5s 超时，失败只警告不阻塞）；
有更新则更新后重新执行命令（环境变量防循环）。无常驻进程。
"""

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from . import __version__, gitops, installer, updater
from .config import ConfigError, Layout, load_bootstrap
from .engine import Sync, SyncError

NO_UPDATE_ENV = "AI_CONFIG_SYNC_NO_UPDATE"

STEPS = ("commit", "push", "sync", "pull")

# 仅接受这些步序列（元组），其余（如 push commit）一律报错。
ALLOWED = (
    ("commit",),
    ("push",),
    ("sync",),
    ("pull",),
    ("commit", "push"),
    ("push", "sync"),
    ("pull", "sync"),
)

STEP_HELP = {
    "commit": "本机受管文件归档到本地 config（本地提交，不联网）",
    "push": "把本地 config 的提交推送到远端（不重算主机文件）",
    "sync": "把本地 config 的受管文件写回本机（写前备份，不联网）",
    "pull": "把远端快进到本地 config（不碰主机文件）",
}


def _setup_log(layout: Layout):
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ai-config-sync")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(RotatingFileHandler(
            layout.log_path, maxBytes=1024 * 1024, backupCount=5,
            encoding="utf-8"))
        logger.addHandler(logging.StreamHandler(sys.stderr))
    return logger


def _sync(layout: Layout) -> Sync:
    boot = load_bootstrap(layout)
    logger = _setup_log(layout)
    return Sync(boot, layout, log=logger.info)


def _install_dir_or_empty(layout: Layout) -> str:
    try:
        return load_bootstrap(layout).install_dir
    except ConfigError:
        return ""


def _auto_update(log):
    """同步操作前的自动更新检查（5s 超时）；失败只警告不阻塞。

    有更新则更新安装仓库并替换进程重跑命令（新代码立即生效）。
    """
    install_dir = _install_dir_or_empty(Layout())
    if not install_dir:
        return  # 未安装或非 git 检出，无从更新
    try:
        updated, tag = updater.check_and_update(
            install_dir, log=log, timeout=5)
    except updater.UpdateError as exc:
        print("警告: {}".format(exc), file=sys.stderr)
        return
    if updated:
        print("已更新到 {}，重新执行命令".format(tag))
        cmd = [sys.executable, "-m", "ai_config_sync"] + sys.argv[1:]
        updater.exec_self(cmd)


def _add_operations(sub):
    """注册四个原子操作；其余步由各子命令的 rest 承接（按执行顺序）。

    不用 argparse 的 choices（nargs='*' 会把空列表也当非法值），
    改为在 main 中用 ALLOWED 显式校验顺序与组合。
    """
    for name in STEPS:
        p = sub.add_parser(name, help=STEP_HELP[name])
        p.add_argument("rest", nargs="*",
                       help="后续步骤（按执行顺序，如 commit push）")


def _combos_text():
    return "、".join(" ".join(c) for c in ALLOWED)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ai-config-sync", description="配置单向归档工具（手动触发）")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="安装（绑定配置仓库）")
    p_mode = p_install.add_subparsers(dest="mode", required=True)
    for name in ("new", "attach"):
        p = p_mode.add_parser(name)
        p.add_argument("repo_url", help="配置仓库 Git URL")
        p.add_argument("-b", "--branch", default=None,
                       help="共享分支（省略跟随 git 默认）")
        p.add_argument("-p", "--place-path", dest="place", default=None,
                       help="工作树位置（默认 /var/lib/ai-config-sync/repo）")
        if name == "new":
            p.add_argument("--force", action="store_true",
                           help="非空仓库中于孤儿分支播种（纯新增 ref）")

    _add_operations(sub)
    sub.add_parser("update", help="更新工具自身（快进到远端最新 tag）")

    args = parser.parse_args(argv)
    layout = Layout()

    if args.cmd == "install":
        logger = _setup_log(layout)
        try:
            if args.mode == "new":
                installer.install_new(layout, args.repo_url,
                                      branch=args.branch,
                                      worktree=args.place,
                                      force=args.force, log=logger.info)
            else:
                installer.install_attach(layout, args.repo_url,
                                          branch=args.branch,
                                          worktree=args.place,
                                          log=logger.info)
        except (installer.InstallError, gitops.GitError) as exc:
            print("安装失败: {}".format(exc), file=sys.stderr)
            return 1
        print("下一步: 编辑 .sync-config/sync.list 后执行 "
              "ai-config-sync commit push（本机→远端）"
              "或 pull sync（远端→本机）")
        return 0

    if args.cmd == "update":
        logger = _setup_log(layout)
        install_dir = _install_dir_or_empty(layout)
        if not install_dir:
            print("无法定位安装目录（bootstrap 缺失或非 git 检出安装）；"
                  "请手动更新", file=sys.stderr)
            return 1
        try:
            updated, tag = updater.check_and_update(
                install_dir, log=logger.info, timeout=60)
        except updater.UpdateError as exc:
            print("更新失败: {}".format(exc), file=sys.stderr)
            return 1
        if updated:
            print("ok 已更新到 {}（当前会话请重开或重新执行命令）".format(tag))
        else:
            print("ok 已是最新（v{}）".format(__version__))
        return 0

    steps = (args.cmd,) + tuple(args.rest)
    if steps not in ALLOWED:
        print("不支持的操作组合: {}；仅支持: {}".format(
            " ".join(steps), _combos_text()), file=sys.stderr)
        return 2

    try:
        sync = _sync(layout)
    except ConfigError as exc:
        print("配置错误: {}".format(exc), file=sys.stderr)
        return 2

    if os.environ.get(NO_UPDATE_ENV) != "1":
        logger = _setup_log(layout)
        _auto_update(logger.info)
        # exec_self 成功时不返回；失败（网络等）只已警告，继续执行本命令

    try:
        status, message = sync.run(list(steps))
    except BlockingIOError:
        print("另一实例正在运行（锁被占用）: {}".format(
            layout.lock_path), file=sys.stderr)
        return 3
    except SyncError as exc:
        print("{} {}".format(exc.status, exc.message), file=sys.stderr)
        return exc.exit_code
    print("{} {}".format(status, message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
