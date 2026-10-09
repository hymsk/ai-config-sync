"""bootstrap 与配置仓库运行配置 (.sync-config) 的加载与保存。"""

import os
from dataclasses import dataclass
from pathlib import Path

from . import tomlite

DEFAULT_ETC = Path("/etc/ai-config-sync")
DEFAULT_VAR = Path("/var/lib/ai-config-sync")


class ConfigError(Exception):
    pass


@dataclass
class Layout:
    """Agent 自身目录布局（固定绝对路径；可注入以便隔离测试）。

    唯一的 $HOME 依赖是冲突标记 flag（原始需求：产生在 ~ 目录下）。
    """

    etc: Path = DEFAULT_ETC
    var: Path = DEFAULT_VAR
    flag: Path = None  # type: ignore[assignment]

    def __post_init__(self):
        self.etc = Path(self.etc)
        self.var = Path(self.var)
        if self.flag is None:
            self.flag = Path.home() / "ERROR_MERGE_FLAG"
        self.flag = Path(self.flag)

    @property
    def bootstrap(self) -> Path:
        return self.etc / "bootstrap.toml"

    @property
    def default_worktree(self) -> Path:
        return self.var / "repo"

    @property
    def state_dir(self) -> Path:
        return self.var / "state"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "sync.log"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    def hard_zones(self, worktree, install_dir=""):
        """硬排除前缀路径列表（含 flag 文件）。"""
        zones = [self.etc, self.var, Path(worktree), self.flag]
        if install_dir:
            p = Path(install_dir)
            if str(p) not in ("", "/"):
                zones.append(p)
        return zones


@dataclass
class Bootstrap:
    repo_url: str
    branch: str
    worktree: Path
    install_dir: str = ""

    def dump(self) -> str:
        return (
            'repo_url = "{0}"\n'
            'branch = "{1}"\n'
            'worktree = "{2}"\n'
            'install_dir = "{3}"\n'
        ).format(self.repo_url, self.branch, self.worktree, self.install_dir)


def load_bootstrap(layout: Layout) -> Bootstrap:
    path = layout.bootstrap
    if not path.is_file():
        raise ConfigError("bootstrap 不存在: {}（先执行 install）".format(path))
    try:
        data = tomlite.loads(path.read_text(encoding="utf-8"))
    except tomlite.TomlError as exc:
        raise ConfigError("bootstrap 解析失败: {}".format(exc))
    for key in ("repo_url", "branch", "worktree"):
        if not data.get(key):
            raise ConfigError("bootstrap 缺少字段: {}".format(key))
    return Bootstrap(
        repo_url=str(data["repo_url"]),
        branch=str(data["branch"]),
        worktree=Path(str(data["worktree"])).expanduser(),
        install_dir=str(data.get("install_dir", "")),
    )


def save_bootstrap(layout: Layout, boot: Bootstrap):
    layout.etc.mkdir(parents=True, exist_ok=True)
    tmp = layout.bootstrap.with_suffix(".tmp")
    tmp.write_text(boot.dump(), encoding="utf-8")
    os.replace(str(tmp), str(layout.bootstrap))


@dataclass
class RepoConfig:
    interval: float = 5.0
    remote_check_interval: float = 5.0
    max_backups_per_file: int = 20
    git_name: str = "sync-bot"
    git_email: str = "sync-bot@invalid"


def parse_repo_config(text: str) -> RepoConfig:
    try:
        data = tomlite.loads(text)
    except tomlite.TomlError as exc:
        raise ConfigError(".sync-config/config.toml 解析失败: {}".format(exc))
    git = data.get("git") or {}
    if not isinstance(git, dict):
        raise ConfigError("config.toml: [git] 节格式错误")
    cfg = RepoConfig()
    try:
        if "interval" in data:
            cfg.interval = float(data["interval"])
        if "remote_check_interval" in data:
            cfg.remote_check_interval = float(data["remote_check_interval"])
        else:
            cfg.remote_check_interval = cfg.interval
        if "max_backups_per_file" in data:
            cfg.max_backups_per_file = int(data["max_backups_per_file"])
    except (TypeError, ValueError):
        raise ConfigError("config.toml: interval 必须为数值")
    cfg.git_name = str(git.get("name", cfg.git_name))
    cfg.git_email = str(git.get("email", cfg.git_email))
    if cfg.interval <= 0 or cfg.remote_check_interval <= 0:
        raise ConfigError("config.toml: interval 必须为正数")
    if cfg.max_backups_per_file < 0:
        raise ConfigError("config.toml: max_backups_per_file 不得为负（0=不清理）")
    return cfg
