"""命令行入口：install / commit / push / sync / pull / update。

四个原子操作（各只做一件事）与三个固定组合：
- commit：本机配置 → 本地仓库（归档 + 本地提交；不联网）
- push：  本地仓库 → 远端仓库（先补提交中断残留的镜像改动，再推送本地提交）
- sync：  本地仓库 → 本机配置（写回；不联网、永不删本机文件）
- pull：  远端仓库 → 本地仓库（fetch + 仅快进；不碰本机文件）
- commit push：本机配置 → 远端仓库（= 归档并推送）
- push sync：  本地仓库 → 远端仓库 + 本机配置（以本地 git 为准）
- pull sync：  远端仓库 → 本机配置（= 拉取并写回）
- clear：     丢弃本地仓库的全部未提交改动（显式破坏性命令，不参与组合）

操作按执行顺序书写；只接受上述固定组合，拒绝 `push commit` 等异常语义。
同步操作执行前自动做一次 update 检查（5s 超时，失败只警告不阻塞）；
有更新则更新后重新执行命令（环境变量防循环）。无常驻进程。
"""

import argparse
import logging
import os
import re
import sys
import unicodedata
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

# 顶层 help 的操作表：(名称, 描述, 流向)。流向是第三列，箭头保留。
ACTION_DESC = (
    ("commit", "归档本机配置到本地仓库", "(本机配置 -> 本地仓库)"),
    ("push", "推送本地提交到远端仓库", "(本地仓库 -> 远端仓库)"),
    ("sync", "把本地仓库文件写回本机配置", "(本地仓库 -> 本机配置)"),
    ("pull", "拉取远端提交到本地仓库", "(远端仓库 -> 本地仓库)"),
    ("commit push", "归档本机配置并推送到远端仓库", "(本机配置 -> 远端仓库)"),
    ("push sync", "推送本地提交并写回本机配置",
     "(本地仓库 -> 远端仓库 + 本机配置)"),
    ("pull sync", "拉取远端提交并写回本机配置", "(远端仓库 -> 本机配置)"),
)

COMMAND_DESC = (
    ("install", "安装并绑定配置仓库"),
    ("update", "更新工具"),
    ("clear", "丢弃本地仓库的未提交改动"),
)

# 单步操作的 (描述, 流向)，由 ACTION_DESC 派生。
STEP_META = {n: (d, f) for n, d, f in ACTION_DESC if " " not in n}

# 每个原子步骤之后允许的下一步。
NEXT_STEPS = {
    "commit": ("push",),
    "push": ("sync",),
    "sync": (),
    "pull": ("sync",),
}

_USAGE = "ai-config-sync <action> [<step> ...] | <command> [args ...]"


def _display_width(text):
    """终端显示宽度：全角/宽字符计 2，其余计 1。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def _pad(text, width):
    return text + " " * max(0, width - _display_width(text))


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
        desc, flow = STEP_META[name]
        nxt = NEXT_STEPS[name]
        rest_help = ("后续步骤：" + "/".join(nxt)) if nxt else "后续步骤：无"
        p = sub.add_parser(name, help=desc,
                           description="{}  {}".format(desc, flow))
        p.add_argument("rest", nargs="*", metavar="STEP", help=rest_help)


def _combos_text():
    return "、".join(" ".join(c) for c in ALLOWED)


def _help_text():
    """顶层 help：action（四个原子步骤 + 三个组合）与 command 两组。

    按显示宽度（中文 = 2 列）对齐，保证终端里三列对得齐。
    """
    name_w = max(_display_width(n)
                 for n, *_ in ACTION_DESC + COMMAND_DESC)
    desc_w = max(_display_width(d) for _, d, *_ in ACTION_DESC)
    lines = ["usage: {}".format(_USAGE), "", "action:"]
    for name, desc, flow in ACTION_DESC:
        lines.append("  {}  {}  {}".format(
            _pad(name, name_w), _pad(desc, desc_w), flow).rstrip())
    lines += ["", "command:"]
    for name, desc in COMMAND_DESC:
        lines.append("  {}  {}".format(_pad(name, name_w), desc).rstrip())
    lines += ["", "查看某操作的用法：ai-config-sync <action> -h", ""]
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    """错误信息里不暴露内部 dest 名（如 cmd），改为可读提示。"""

    def error(self, message):
        m = re.match(
            r"argument \w+: invalid choice: (.+?) \(choose from (.+)\)$",
            message)
        if m:
            choices = m.group(2).replace("'", "").replace(", ", "、")
            message = "未知操作或命令：{}；可选：{}".format(m.group(1), choices)
        self.print_usage(sys.stderr)
        print("ai-config-sync: error: {}".format(message), file=sys.stderr)
        raise SystemExit(2)


def main(argv=None):
    parser = _Parser(
        prog="ai-config-sync", usage=_USAGE, add_help=False)
    parser.add_argument("-h", "--help", action="store_true",
                        help="显示帮助并退出")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="cmd", prog="ai-config-sync")

    p_install = sub.add_parser("install", help="安装并绑定配置仓库")
    p_mode = p_install.add_subparsers(dest="mode")
    mode_meta = {
        "new": ("在空仓库中初始化配置仓库",
                "目标仓库必须为空；从种子模板初始化"),
        "attach": ("接入已存在的配置仓库",
                   "目标仓库须已有分支且 .sync-config/config.toml 可用"),
    }
    for name in ("new", "attach"):
        help_text, desc = mode_meta[name]
        p = p_mode.add_parser(name, help=help_text, description=desc)
        p.add_argument("repo_url", help="配置仓库 Git URL")
        p.add_argument("-b", "--branch", default=None,
                       help="共享分支，省略则跟随 git 默认")
        p.add_argument("-p", "--place-path", dest="place", default=None,
                       help="工作树位置，默认为 /var/lib/ai-config-sync/repo")
        if name == "new":
            p.add_argument("--force", action="store_true",
                           help="在非空仓库中以孤儿分支播种，只新增 ref")

    _add_operations(sub)
    sub.add_parser("update", help="更新工具", description="更新工具")
    sub.add_parser(
        "clear", help="丢弃本地仓库的未提交改动",
        description="丢弃工作树全部未提交改动（已跟踪文件恢复到 HEAD，"
                    "未跟踪文件移除；不联网、不碰主机文件）")

    args = parser.parse_args(argv)
    layout = Layout()

    if args.help or not args.cmd:
        print(_help_text(), end="")
        return 0

    if args.cmd == "install":
        if not args.mode:
            # 无 mode：打印 install 的用法（而非 argparse 的 "required: mode"）
            p_install.print_usage(sys.stderr)
            print("ai-config-sync: 请指定 install 模式：new 或 attach；"
                  "查看用法：ai-config-sync install -h", file=sys.stderr)
            return 2
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
        print("下一步：编辑 .sync-config/sync.list，然后执行 "
              "ai-config-sync commit push 或 pull sync")
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

    if args.cmd == "clear":
        try:
            sync = _sync(layout)
        except ConfigError as exc:
            print("配置错误: {}".format(exc), file=sys.stderr)
            return 2
        try:
            status, message = sync.clear()
        except BlockingIOError:
            print("另一实例正在运行（锁被占用）: {}".format(
                layout.lock_path), file=sys.stderr)
            return 3
        except SyncError as exc:
            print("{} {}".format(exc.status, exc.message), file=sys.stderr)
            return exc.exit_code
        print("{} {}".format(status, message))
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
