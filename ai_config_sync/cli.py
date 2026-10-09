"""命令行入口：install / once / run / doctor。"""

import argparse
import logging
import sys

from . import __version__, adopt, gitops, installer
from .agent import Agent
from .config import ConfigError, Layout, load_bootstrap


def _setup_log(layout: Layout):
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ai-config-sync")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.FileHandler(layout.log_path,
                                               encoding="utf-8"))
        logger.addHandler(logging.StreamHandler(sys.stderr))
    return logger


def _agent(layout: Layout) -> Agent:
    boot = load_bootstrap(layout)
    logger = _setup_log(layout)
    return Agent(boot, layout, log=logger.info)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ai-config-sync", description="多服务器配置同步 Agent")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="安装")
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

    p_once = sub.add_parser("once", help="单轮（默认仅预览差异）")
    p_once.add_argument("--prefer", choices=("local", "repo", "merge"),
                        default=None, help="首轮采用方向")

    sub.add_parser("run", help="常驻运行")
    sub.add_parser("doctor", help="体检")

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
        print("下一步: ai-config-sync once  # 预览差异，然后 --prefer local|repo|merge")
        return 0

    try:
        agent = _agent(layout)
    except ConfigError as exc:
        print("配置错误: {}".format(exc), file=sys.stderr)
        return 2

    if args.cmd == "once":
        if not args.prefer:
            rows = adopt.preview(agent)
            adopt.print_preview(rows)
            print("确认后执行: ai-config-sync once --prefer local|repo|merge")
            return 0
        if args.prefer == "local":
            status, message = adopt.prefer_local(agent)
        elif args.prefer == "repo":
            status, message = adopt.prefer_repo(agent)
        else:
            status, message = adopt.prefer_merge(agent)
        print("{} {}".format(status, message))
        return 0 if status in ("ok", "idle") else 1

    if args.cmd == "run":
        try:
            agent.run()
        except KeyboardInterrupt:
            print("退出")
        return 0

    if args.cmd == "doctor":
        return _doctor(agent, layout)
    return 0


def _doctor(agent: Agent, layout: Layout) -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print("[{}] {} {}".format("OK" if cond else "FAIL", name, detail))
        if not cond:
            ok = False

    boot = agent.boot
    check("bootstrap", layout.bootstrap.is_file(), str(layout.bootstrap))
    check("worktree", (agent.wt / ".git").exists(), str(agent.wt))
    branch = gitops.current_branch(agent.wt)
    check("branch", branch == boot.branch,
          "local={} expect={}".format(branch, boot.branch))
    try:
        heads, _ = gitops.ls_remote(boot.repo_url)
        check("remote", True, "heads=" + (",".join(sorted(heads)) or "-"))
        check("remote-branch", boot.branch in heads, boot.branch)
    except gitops.GitError as exc:
        check("remote", False, str(exc))
    dirty = gitops.status_porcelain(agent.wt)
    check("worktree-clean", not dirty, "; ".join(dirty[:3]))
    check("merge-clean", not (gitops.merge_in_progress(agent.wt)
                              or gitops.unmerged_paths(agent.wt)))
    check("flag", True, "paused" if layout.flag.exists() else "clear")
    try:
        agent._load()
        check("config", True)
    except Exception as exc:
        check("config", False, str(exc))
    last = agent.journal.last()
    check("journal", last != "corrupt", str(last))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
