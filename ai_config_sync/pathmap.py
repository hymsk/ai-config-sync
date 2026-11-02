"""主机路径 <-> 仓库 source/ 路径映射（~ 展开与最长前缀匹配）。

默认映射 {"~": "__user_home__"}：本机 home 下的文件归档到
source/__user_home__/ 下。home 语义与登录身份无关：Linux root=/root、
普通用户=/home/<u>、Windows Git Bash=C:/Users/<n>，均映射到同一
source/__user_home__/ 布局（值名不与自然路径撞车，一眼可辨）。

路径统一正斜杠：Windows 原生 Python 的 expanduser/os.walk 会产生反斜杠，
入口处一律 normalize，内部与仓库侧路径全部使用 "/"。
"""

import os

# 配置缺省值：config.toml 未写 [path_map] 节时生效
DEFAULT_PATH_MAP = {"~": "__user_home__"}


class PathMapError(Exception):
    """映射配置非法（engine 侧转为 error-config）。"""
    pass


def normalize(path: str) -> str:
    """统一路径分隔符为 /（Windows 原生 Python 兼容）。"""
    return path.replace("\\", "/")


def home_dir() -> str:
    """本机 home 目录（归一化为正斜杠）。"""
    return normalize(os.path.expanduser("~"))


def expand_tilde(s: str, home=None) -> str:
    """把 "~" / "~/..." 展开为本机 home 绝对路径；其余原样返回。"""
    h = home_dir() if home is None else home
    if s == "~":
        return h
    if s.startswith("~/"):
        return h + s[1:]
    return s


def _is_absolute(path: str) -> bool:
    """POSIX 绝对路径（/...）或 Windows 盘符路径（C:/...）。"""
    if path.startswith("/"):
        return True
    return (len(path) >= 3 and path[0].isalpha()
            and path[1] == ":" and path[2] == "/")


def _under(path: str, prefix: str) -> bool:
    """path 是否位于 prefix 内（含等于）；按路径段边界匹配。"""
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


class PathMap:
    """主机绝对路径 <-> 仓库 source/ 相对路径的双向前缀映射。

    - raw 形如 {"~": "root", "/d/work": "work"}；缺省为 DEFAULT_PATH_MAP；
    - key：`~` / `~/xxx` 展开为本机 home，或任意绝对路径前缀；
    - value：source/ 下相对目录（非空、无 .. 与通配符）；
    - 匹配：最长前缀优先，且按段边界（/root 不匹配 /rootdir）；
    - 未命中任何映射的路径保持恒等：/etc/x <-> source/etc/x；
    - 撞车防护：未命中映射、但恒等结果落在某映射 value 之下 → 拒绝
      （同一仓库路径出现两个本机来源，真镜像语义下会互踩）。
    """

    def __init__(self, raw=None, home=None):
        raw = dict(DEFAULT_PATH_MAP) if raw is None else dict(raw)
        if not raw:
            raise PathMapError("[path_map] 不能为空表（省略该节即用默认映射）")
        self.home = home_dir() if home is None else normalize(home)
        entries = {}   # host 前缀 -> source/ 下相对目录
        values = {}    # 反查：value -> host 前缀（值须唯一）
        for key, value in raw.items():
            if not isinstance(key, str) or not key:
                raise PathMapError("[path_map] 键须为非空字符串")
            if not isinstance(value, str) or not value:
                raise PathMapError("[path_map] 值须为非空字符串")
            hp = expand_tilde(key, self.home)
            if not _is_absolute(hp):
                raise PathMapError(
                    "[path_map] 键须为 ~ 或绝对路径: {!r}".format(key))
            hp = hp.rstrip("/") or "/"
            if hp == "/":
                raise PathMapError("[path_map] 不允许映射整个根 /")
            if value.startswith("/"):
                raise PathMapError(
                    "[path_map] 值须为 source/ 下相对目录: {!r}".format(value))
            for seg in value.split("/"):
                if seg in ("", ".", "..") or any(c in seg for c in "*?"):
                    raise PathMapError(
                        "[path_map] 值含非法路径段: {!r}".format(value))
            if value in values:
                raise PathMapError(
                    "[path_map] 值 {!r} 被多个键映射（{} 与 {}）".format(
                        value, values[value], key))
            entries[hp] = value
            values[value] = key
        # host 侧 / 仓库侧各自按前缀长度降序，实现最长前缀优先
        self._by_host = sorted(entries.items(), key=lambda kv: -len(kv[0]))
        self._by_repo = sorted(entries.items(), key=lambda kv: -len(kv[1]))
        self.entries = entries

    def to_repo(self, host_path: str) -> str:
        """主机绝对路径 -> 仓库相对路径 source/...。"""
        host_path = normalize(host_path)
        for hp, value in self._by_host:
            if _under(host_path, hp):
                rel = host_path[len(hp):].lstrip("/")
                return "source/" + value + ("/" + rel if rel else "")
        if not host_path.startswith("/"):
            # Windows 盘符路径（C:/...）没有 POSIX 恒等形态：要求显式映射
            raise PathMapError(
                "路径 {} 未命中任何映射键，且盘符路径无恒等归档位置；"
                "请在 [path_map] 中为其配置显式映射".format(host_path))
        repo = "source" + host_path
        for _, value in self._by_repo:
            if _under(repo, "source/" + value):
                raise PathMapError(
                    "路径 {} 未命中任何映射键，但归档位置 {} 与映射值 {!r} "
                    "撞车（同一路径会有两个本机来源）；请把清单改写为映射键"
                    "前缀（如 ~/...）或调整 [path_map]".format(
                        host_path, repo, value))
        return repo

    def to_host(self, repo_path: str) -> str:
        """仓库相对路径 source/... -> 主机绝对路径。"""
        if not repo_path.startswith("source/"):
            raise PathMapError("非 source/ 仓库路径: {}".format(repo_path))
        rel = repo_path[len("source/"):]
        for hp, value in self._by_repo:
            if rel == value or rel.startswith(value + "/"):
                sub = rel[len(value):].lstrip("/")
                return hp + ("/" + sub if sub else "")
        return "/" + rel
