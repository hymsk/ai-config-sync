"""命令行入口：install / push / pull / update。

两个手动单向操作 + 一个自更新命令：
- push：本机受管文件 → 配置仓库（自动 commit + push）
- pull：配置仓库受管文件 → 本机（写前备份、格式校验、原子替换）
- update：快进安装仓库到远端最新 tag
push/pull 执行前自动做一次 update 检查（5s 超时，失败只警告不阻塞）；
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


def _auto_update(logger):
    """push/pull 前的自动更新检查（5s 超时）；失败只警告不阻塞。

    有更新则更新安装仓库并替换进程重跑命令（新代码立即生效）。
    """
    install_dir = _install_dir_or_empty(Layout())
    if not install_dir:
        return  # 未安装或非 git 检出，无从更新
    try:
        updated, tag = updater.check_and_update(
            install_dir, log=logger.info, timeout=5)
    except updater.UpdateError as exc:
        print("警告: {}".format(exc), file=sys.stderr)
        return
    if updated:
        print("已更新到 {}，重新执行命令".format(tag))
        cmd = [sys.executable, "-m", "ai_config_sync"] + sys.argv[1:]
        updater.exec_self(cmd)


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

    sub.add_parser("push", help="本机受管文件归档到配置仓库并推送")
    sub.add_parser("pull", help="配置仓库受管文件写回本机（写前备份）")
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
              "ai-config-sync push（本机→仓库）或 pull（仓库→本机）")
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

    try:
        sync = _sync(layout)
    except ConfigError as exc:
        print("配置错误: {}".format(exc), file=sys.stderr)
        return 2

    if os.environ.get(NO_UPDATE_ENV) != "1":
        _auto_update(sync.log)
        # exec_self 成功时不返回；失败（网络等）只已警告，继续执行本命令

    try:
        if args.cmd == "push":
            status, message = sync.push()
        else:
            status, message = sync.pull()
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
